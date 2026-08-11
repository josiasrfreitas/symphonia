"""The real Runtime Adapter for Orca.

TLDR: implements the ``RuntimeAdapter`` contract by shelling out to the
``orca`` CLI, one deterministic invocation per contract call. The command
runner is injectable so the conformance suite can drive the exact same
adapter against a scripted CLI. The Capability Tier translation table lives
here — roles declare a tier, only this file knows model names.

``RoleSpec.briefing`` (GRE-175 M3, closed by GRE-184 M3): the brief IS the
body of the first ``dispatch`` — the caller passes it as ``work``, same as
every later correction. There is no second channel to wire.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..attention import Attention, AttentionCode
from ..runtime_adapter import (
    Access,
    AttemptRef,
    AttemptStatus,
    CapabilityTier,
    ContextRef,
    ControlLostEvent,
    EscalationEvent,
    EventBatch,
    EventKind,
    KillReceipt,
    LaunchResult,
    Liveness,
    QuestionEvent,
    ResultEvent,
    RoleName,
    RoleSpec,
    RuntimeCapabilities,
    RuntimeEvent,
    TicketKey,
    TierEvidence,
    WorkspaceRef,
)
from .events import parse_check_output
from .launcher import LaunchPlan, TIER_MODELS, build_launch, observed_models, tier_matches


@dataclass(frozen=True)
class RunResult:
    """One completed ``orca`` invocation, both halves the envelope
    unwrapper needs: a rejected lifecycle message can only be told apart
    from an accepted one by combining ``stdout`` (the envelope, which still
    answers ``ok: true``) with ``returncode`` (GRE-184 M1 — measured in
    ``spawn.orca`` before this adapter carried the same check)."""

    stdout: str
    stderr: str
    returncode: int


Runner = Callable[[Sequence[str]], RunResult]
"""Runs one full argv (``["orca", *args, "--json"]``), returns the
completed result. Production uses ``subprocess_runner``; the conformance
suite and ``spawn``'s characterization tests inject a scripted double."""


class OrcaCliError(RuntimeError):
    """One ``orca`` invocation failed — at the envelope layer (``ok:
    false``), the transport layer (no stdout), or the lifecycle layer (a
    refused ``worker_done``). ``str(exc)`` is the fully-formed message both
    ``OrcaRuntimeAdapter._orca`` (surfaces it as-is) and ``spawn.orca``
    (wraps it in ``SystemExit`` with no changes) show the caller — one
    wording for both sides of the fence."""


def _required(value: str, what: str) -> str:
    """An id the CLI must return. An empty one fails here, loudly, instead
    of surfacing calls later as a workspace with no path or an attempt
    keyed by the empty string."""

    if not value:
        raise RuntimeError(f"orca returned no {what}; refusing to continue with an empty id")
    return value


def subprocess_runner(argv: Sequence[str]) -> RunResult:
    """The production ``Runner``: one subprocess, stdout/stderr/returncode
    all handed back. Never raises on a nonzero exit — deciding what a
    nonzero exit or an ``ok: false`` envelope means is ``unwrap_envelope``'s
    job, not the transport's, so the two error paths cannot disagree."""

    proc = subprocess.run(list(argv), capture_output=True, text=True)
    return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def unwrap_envelope(
    argv: Sequence[str], result: RunResult, *, expect_lifecycle_ok: bool = False
) -> dict:
    """The one envelope unwrapper, shared by ``OrcaRuntimeAdapter._orca``
    and ``spawn.orca`` (GRE-184 M1 — the boundary condition with GRE-185:
    ``spawn.orca``'s message text is exactly what this produces). The CLI
    wraps every ``--json`` response in ``{id, ok, result, _meta}``.

    ``argv`` is the bare call (no ``orca``/``--json``) — only used to name
    the failing call in a message.

    ``expect_lifecycle_ok`` is for the lifecycle messages a role sends
    (``worker_done``). Measured against Orca 1.4.168: a rejected
    ``worker_done`` still answers ``ok: true`` — the refusal lives in
    ``result.lifecycle`` and in the process exit code, not in the envelope.
    Without this flag a rejected completion reads as a success, which is
    exactly how a planner ends up never retiring. Note the asymmetry:
    ``lifecycle`` is present only on a rejection, so its absence must never
    be treated as failure.
    """

    if not result.stdout.strip():
        raise OrcaCliError(f"orca {' '.join(argv)} produced no output: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OrcaCliError(
            f"orca {' '.join(argv)} exited {result.returncode} with unparseable output: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        ) from exc
    if isinstance(data, dict) and "ok" in data:
        if not data.get("ok"):
            err = data.get("error")
            if not isinstance(err, dict):
                err = {"message": str(err)}
            raise OrcaCliError(
                f"orca {' '.join(argv)} failed: {err.get('code', 'unknown')}: {err.get('message', '')}"
            )
        data = data.get("result")
    if expect_lifecycle_ok:
        lifecycle = (data or {}).get("lifecycle") or {}
        if lifecycle.get("action") == "rejected" or result.returncode != 0:
            raise OrcaCliError(
                f"orca {' '.join(argv[:2])} rejected: "
                f"{lifecycle.get('code') or f'exit {result.returncode}'}: "
                f"{lifecycle.get('reason') or result.stderr.strip()}"
            )
    if data is None:
        return {}
    return data if isinstance(data, dict) else {"items": data}


# The (tier -> launch command) table GRE-161 put behind this boundary now
# lives in launcher.py, so the adapter and the `spawn` CLI cannot drift into
# two different command lines for the same tier (GRE-179).

# What the phase looks like in the Orca sidebar (GRE-184 M3, retroported
# from `spawn.ROLE_BADGE` unedited). Orca exposes no per-worktree colour, so
# the phase is carried by the three labels it does expose: the worktree
# display name, the terminal title, and the board column.
ROLE_BADGE = {
    RoleName.PLANNER: ("🧭", "planning", "in-progress"),
    RoleName.IMPLEMENTER: ("🔨", "implementing", "in-progress"),
    RoleName.SPEC_REVIEWER: ("🔍", "spec review", "in-review"),
    RoleName.STANDARDS_REVIEWER: ("📐", "standards review", "in-review"),
}

_CAPABILITY = re.compile(r"--dispatch-capability\s+(\S+)")


def _capability_of(preamble: str) -> str | None:
    """The Dispatch capability token Orca mints for an injected dispatch
    (GRE-184 M3, retroported from `spawn._capability_of` unedited). It
    exists only as text inside the preamble (the dispatch row's
    ``capability_hash`` comes back null), so it is read with a regex — the
    preamble repeats it once per lifecycle command and they are the same
    token."""

    found = _CAPABILITY.search(preamble)
    return found.group(1) if found else None


VALID_PATHS_AFTER_DONE = (
    "orca orchestration dispatch --inject (new task into the terminal)",
    "orca terminal send (direct prompt to the pane)",
)


class MessageWorkerRefused(Exception):
    """Raised instead of sending when the dispatch can no longer wake the
    worker. Carries a structured Attention flag; scripts branch on the code."""

    def __init__(self, dispatch_id: str, status: str):
        self.attention = Attention(
            needs=True,
            code=AttentionCode.TICKET_WITHOUT_WORKER,
            reason=(
                f"dispatch {dispatch_id} is {status}: a send lands in the mailbox "
                f"but never wakes the worker. Valid paths: {'; '.join(VALID_PATHS_AFTER_DONE)}"
            ),
        )
        self.valid_paths = VALID_PATHS_AFTER_DONE
        super().__init__(self.attention.reason)


@dataclass
class _ContextRecord:
    ref: ContextRef
    terminal: str
    access: Access
    requested_tier: CapabilityTier
    plan: LaunchPlan | None = None


@dataclass
class _AttemptRecord:
    ref: AttemptRef
    task_id: str
    capability: str = ""
    open: bool = True
    fenced: bool = False


@dataclass
class OrcaRuntimeAdapter:
    """``RuntimeAdapter`` over the real Orca CLI.

    Orca records only the launch command, never which model answered, so
    ``tier_verification`` is False and evidence is at best ``requested``.
    """

    coordinator: str
    run_id: str
    runner: Runner = subprocess_runner
    bind_control: bool = True
    """Whether `_ensure_control` actually runs `run-current`/`run-use`
    before each call. Defaults True — the coordinator loop and the
    conformance suite construct with this default, so control-lost
    detection is exercised there. GRE-184 M4 composes `spawn.py`'s verbs
    over this adapter with `bind_control=False`: `spawn.py` never emitted
    `run-current` and the characterization tests freeze that argv byte for
    byte, so the coordinator-binding check cannot be introduced on that
    path without breaking M0's frozen argv. Consequence, spelled out:
    with `bind_control=False`, production spawn/status/retire never
    exercises control-lost detection — the conformance suite (which
    always binds) is the only place that check runs."""

    _contexts: dict[str, _ContextRecord] = field(default_factory=dict)
    _attempts: dict[str, _AttemptRecord] = field(default_factory=dict)
    _pending_questions: set[str] = field(default_factory=set)
    _local_events: list[RuntimeEvent] = field(default_factory=list)
    _delivered_locals: list[RuntimeEvent] = field(default_factory=list)
    _seq: int = 0

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(tier_verification=False, cooperative_completion=True)

    def _orca(self, *argv: str, expect_lifecycle_ok: bool = False) -> dict:
        result = self.runner(["orca", *argv, "--json"])
        return unwrap_envelope(argv, result, expect_lifecycle_ok=expect_lifecycle_ok)

    def _last_open_attempt(self) -> AttemptRef | None:
        for rec in reversed(list(self._attempts.values())):
            if rec.open:
                return rec.ref
        return None

    def _ensure_control(self) -> None:
        """Every operation re-establishes the coordinator binding first. A
        reclaim is reported, never silent — attributed to the most recent
        open attempt, the work most at risk while control was lost. A no-op
        when `bind_control` is False (see that field's docstring)."""

        if not self.bind_control:
            return
        current = self._orca("orchestration", "run-current")
        run = current.get("run") if isinstance(current.get("run"), dict) else {}
        holder = str(run.get("terminal", current.get("terminal", current.get("coordinator", ""))))
        if holder == self.coordinator:
            return
        self._orca("orchestration", "run-use", "--run", self.run_id, "--from", self.coordinator)
        attempt = self._last_open_attempt()
        if attempt is not None:
            self._local_events.append(
                ControlLostEvent(kind="control-lost", attempt=attempt, detail=f"observed holder {holder!r}; reclaimed")
            )

    # --- workspaces ---

    def default_base(self) -> str:
        """The repo's default base, read from git rather than assumed
        (GRE-184 M2, retroported from ``spawn.default_base`` unedited). A
        git call, not an orca one — it never goes through ``self.runner``,
        which is scoped to `orca` argv (see ``Runner``'s docstring); like
        ``spawn.py``'s own git calls, it stays a plain subprocess."""

        proc = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True,
        )
        base = proc.stdout.strip()
        if not base:
            raise RuntimeError(
                "cannot resolve origin/HEAD; run `git remote set-head origin -a` "
                "so the ticket branch has a known base"
            )
        return base

    def _close_orphan_shell(self, workspace_id: str) -> None:
        """A bare ``worktree create`` leaves one untitled fallback shell
        (measured on GRE-179, retroported here unedited from
        ``spawn.create_worktree``). Best-effort: cosmetic, and the shell's
        tab can already be gone by the time this asks — a tidy sidebar is
        never worth failing a workspace that otherwise came up fine."""

        listed = self._orca("terminal", "list", "--worktree", f"id:{workspace_id}")
        for term in listed.get("terminals", []):
            if term.get("title") or term.get("command"):
                continue
            try:
                self._orca("terminal", "close", "--terminal", str(term.get("handle")), "--tab")
            except OrcaCliError as exc:
                print(f"note: leftover shell not closed ({exc})", file=sys.stderr)

    def create_workspace(self, ticket_key: TicketKey, *, base_branch: str) -> WorkspaceRef:
        """One checkout per Ticket Key: child of the coordinator in Orca
        lineage, off ``base_branch`` in git — the argv measured from
        ``spawn.create_worktree`` (GRE-184 M2). Worktree setup
        (``setup_worktree.setup``) is a separate subprocess boundary and
        stays the caller's job, same as it always has."""

        self._ensure_control()
        name = ticket_key.strip().lower()
        created = self._orca(
            "worktree", "create",
            "--name", name,
            "--parent-worktree", "active",
            "--base-branch", base_branch,
            "--setup", "run",
            "--linear-issue", ticket_key,
            "--comment", f"symphonia ticket {ticket_key}",
        )
        wt = created.get("worktree", created)
        wt_id = _required(str(wt.get("id", "")), "worktree id")
        path = _required(str(wt.get("path", "")), "worktree path")
        branch = str(wt.get("branch") or name)
        self._close_orphan_shell(wt_id)
        return WorkspaceRef(ticket_key=ticket_key, id=wt_id, path=path, branch=branch)

    def find_workspace(self, ticket_key: TicketKey) -> WorkspaceRef | None:
        """(id, path, branch) of this ticket's worktree, or None —
        retroported from ``spawn.find_worktree``. Matched on the path, not
        the display name: composed verbs rewrite the display name with the
        current phase, so a ``name:`` lookup would stop finding the ticket
        the moment a phase change lands."""

        self._ensure_control()
        name = ticket_key.strip().lower()
        listed = self._orca("worktree", "list")
        items = listed.get("worktrees", listed.get("items", []))
        for wt in items:
            path = str(wt.get("path", ""))
            if Path(path).name == name:
                wt_id = str(wt.get("id", ""))
                branch = str(wt.get("branch") or name)
                return WorkspaceRef(ticket_key=ticket_key, id=wt_id, path=path, branch=branch)
        return None

    def destroy_workspace(self, workspace: WorkspaceRef) -> None:
        self._ensure_control()
        live = [c for c in self._contexts.values() if c.ref.workspace.path == workspace.path]
        if live:
            raise RuntimeError(
                f"refusing to destroy {workspace.ticket_key}: {len(live)} context(s) still live. "
                "An abandoned role may still be writing here."
            )
        self._orca("worktree", "rm", "--worktree", f"path:{workspace.path}")

    # --- role contexts ---

    def set_phase(self, workspace: WorkspaceRef, role: RoleName) -> None:
        """The phase badge in the Orca sidebar (GRE-184 M3, retroported from
        ``spawn.spawn`` unedited). Orca-specific, like ``message_worker`` —
        not part of the neutral Protocol, and not called by ``launch_role``
        itself: the caller sets the badge, then launches, same order the
        measured argv freezes."""

        self._ensure_control()
        emoji, phase, board = ROLE_BADGE[role]
        self._orca(
            "worktree", "set",
            "--worktree", f"id:{workspace.id}",
            "--display-name", f"{emoji} {workspace.ticket_key} · {phase}",
            "--workspace-status", board,
        )

    def launch_role(self, workspace: WorkspaceRef, spec: RoleSpec) -> LaunchResult:
        self._ensure_control()
        if spec.access is Access.WRITE:
            writer = next(
                (
                    c
                    for c in self._contexts.values()
                    if c.ref.workspace.path == workspace.path and c.access is Access.WRITE
                ),
                None,
            )
            if writer is not None:
                raise RuntimeError(
                    f"refusing to launch {spec.role.value} into {workspace.ticket_key}: "
                    f"{writer.ref.role.value} already holds write access. "
                    "The runtime cannot detect the collision."
                )

        self._seq += 1
        context_id = f"ctx-{self._seq}"
        emoji, phase, _board = ROLE_BADGE[spec.role]
        # The argv measured from `spawn.spawn` (GRE-184 M3): title carries
        # phase and ticket, not context identity — Rig._proc_for falls back
        # to a role+liveness match for exactly this reason.
        title = f"{emoji} {phase} · {workspace.ticket_key}"
        plan = build_launch(
            spec.role,
            session_id=str(uuid.uuid4()),
            workspace=workspace.path,
            tier=spec.tier,
            access=spec.access,
        )
        created = self._orca(
            "terminal", "create",
            "--worktree", f"id:{workspace.id}",
            "--title", title,
            "--command", plan.command,
        )
        # Real shape, matching `spawn.spawn`'s `orca(...)["terminal"]["handle"]`
        # (GRE-184 M3) — the old flat `created["terminal"]` this replaced was
        # never exercised against anything but the adapter's own dead-code fake.
        term = created.get("terminal", created)
        terminal = _required(str(term.get("handle", term.get("terminal", ""))), "terminal handle")
        # Losing the first prompt costs a whole spawn, so readiness is
        # waited on, never assumed.
        self._orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle", "--timeout-ms", "120000")
        ref = ContextRef(id=context_id, role=spec.role, workspace=workspace)
        self._contexts[context_id] = _ContextRecord(
            ref=ref, terminal=terminal, access=spec.access, requested_tier=spec.tier, plan=plan
        )
        return LaunchResult(context=ref, tier_evidence=self.verify_tier(ref))

    def verify_tier(self, context: ContextRef) -> TierEvidence:
        """Orca records the launch command only, but the CLI writes a session
        transcript that names the model on every answer. Pinning the session
        id at launch makes that file addressable, so a tier is `observed`
        once the context has answered at least once (GRE-179)."""

        record = self._contexts.get(context.id)
        if record is None:
            return TierEvidence(kind="unverifiable", detail="no such context")
        transcript = record.plan.transcript if record.plan else None
        if transcript is None:
            return TierEvidence(
                kind="requested",
                tier=record.requested_tier,
                detail=f"provider {record.plan.provider if record.plan else '?'} exposes no transcript",
            )
        models = observed_models(transcript)
        if not models:
            # No answer yet is not a wrong tier: the session may still be
            # starting. Downgrade to what was asked for, never guess.
            return TierEvidence(
                kind="requested",
                tier=record.requested_tier,
                detail="session has not answered yet; transcript is empty",
            )
        if tier_matches(record.requested_tier, models):
            return TierEvidence(
                kind="observed",
                tier=record.requested_tier,
                detail=f"transcript reports {', '.join(models)}",
            )
        return TierEvidence(
            kind="observed",
            tier=None,
            detail=(
                f"tier mismatch: requested {record.requested_tier.value} "
                f"({TIER_MODELS[record.requested_tier]}), transcript reports {', '.join(models)}"
            ),
        )

    def close_role(self, context: ContextRef) -> None:
        self._ensure_control()
        record = self._contexts.pop(context.id, None)
        if record is None:
            return
        self._orca("terminal", "close", "--terminal", record.terminal)

    # --- attempts ---

    def _roll_back_uncapabilitied_dispatch(self, task_id: str, terminal: str) -> None:
        """A dispatch that minted no capability, undone (GRE-184 M3,
        retroported from ``spawn._require_capability`` unedited): the task
        and terminal are rolled back so a retry starts clean, rather than
        leaving a role launched but never able to report."""

        self._orca(
            "orchestration", "task-update", "--id", task_id, "--status", "failed",
            "--result", json.dumps({"reason": "no dispatch capability in the preamble"}),
        )
        try:
            self._orca("terminal", "close", "--terminal", terminal, "--tab")
        except OrcaCliError as exc:
            print(f"note: terminal not closed ({exc})", file=sys.stderr)

    def dispatch(self, context: ContextRef, work: str) -> AttemptRef:
        self._ensure_control()
        record = self._contexts.get(context.id)
        if record is None:
            raise RuntimeError(f"context {context.id} is gone; dispatch has nowhere to land")
        ticket = context.workspace.ticket_key
        task = self._orca("orchestration", "task-create", "--spec", work)
        # Real shape, matching `spawn.spawn`'s `orca(...)["task"]["id"]`
        # (GRE-184 M3) — the old flat `taskId`/`task_id` this replaced was
        # never exercised against anything but the adapter's own dead-code fake.
        task_row = task.get("task", task)
        task_id = _required(str(task_row.get("id", "")), "task id")
        dispatched = self._orca(
            "orchestration", "dispatch", "--task", task_id,
            "--to", record.terminal, "--inject", "--return-preamble",
        )
        dispatch_row = dispatched.get("dispatch") or {}
        attempt_id = _required(str(dispatch_row.get("id", "")), "dispatch id")
        capability = _capability_of(dispatched.get("preamble") or "")
        if capability is None:
            self._roll_back_uncapabilitied_dispatch(task_id, record.terminal)
            raise OrcaCliError(
                f"dispatch for {ticket}/{context.role.value} minted no capability, so the role "
                f"could never report; the terminal and task were rolled back. Retry the spawn."
            )
        ref = AttemptRef(attempt_id=attempt_id, ticket_key=ticket, context=context)
        self._attempts[attempt_id] = _AttemptRecord(ref=ref, task_id=task_id, capability=capability)
        return ref

    def snapshot(self, attempt: AttemptRef) -> dict:
        """Everything a caller needs to persist about one dispatch, read
        back from the adapter's own records (GRE-184 M3) rather than
        reconstructed by hand: terminal, task, dispatch, capability, tier,
        access, session id, transcript, launch command."""

        attempt_record = self._attempts[attempt.attempt_id]
        context_record = self._contexts[attempt.context.id]
        plan = context_record.plan
        return {
            "terminal": context_record.terminal,
            "task": attempt_record.task_id,
            "dispatch": attempt.attempt_id,
            "capability": attempt_record.capability,
            "tier": context_record.requested_tier.value,
            "access": context_record.access.value,
            "session_id": plan.session_id if plan else None,
            "transcript": str(plan.transcript) if plan and plan.transcript else None,
            "command": plan.command if plan else None,
        }

    def kill(self, attempt: AttemptRef) -> KillReceipt:
        self._ensure_control()
        stopped = self._orca("orchestration", "worker-stop", "--dispatch", attempt.attempt_id)
        record = self._attempts.get(attempt.attempt_id)
        if record is not None:
            record.open, record.fenced = False, True
            self._contexts.pop(record.ref.context.id, None)
        return KillReceipt(attempt=attempt, confirmed_gone=bool(stopped.get("stopped", True)))

    # --- cross-process control (GRE-184 M4b) ---
    #
    # `status`/`retire` run in a fresh process, after `_contexts`/`_attempts`
    # are gone with the process that populated them — the in-memory
    # `kill`/`sweep` above have nowhere to look. These four concrete methods
    # (outside the Protocol, like `message_worker`) take the ids the
    # registry already has on disk and act on them directly, one call each,
    # matching `spawn.status`/`spawn.retire`'s measured argv.

    def dispatch_status(self, task_id: str) -> str:
        shown = self._orca("orchestration", "dispatch-show", "--task", task_id)
        return str((shown.get("dispatch") or {}).get("status", "?"))

    def stop_worker(self, dispatch_id: str) -> None:
        """Best-effort: `worker-stop` only knows dispatches `worker-start`
        created, and this package launches through `terminal create` so it
        can set a model and permissions (GRE-179) — a failure here is
        expected, not exceptional. The caller decides what to do with it."""

        self._orca("orchestration", "worker-stop", "--dispatch", dispatch_id)

    def close_terminal(self, handle: str, *, tab: bool) -> None:
        argv = ["terminal", "close", "--terminal", handle]
        if tab:
            argv.append("--tab")
        self._orca(*argv)

    def settle_task(self, task_id: str, reason: str) -> None:
        """A killed role's Task would otherwise sit in `dispatched` forever,
        which Reconciliation reads as an attempt still in flight."""

        self._orca(
            "orchestration", "task-update", "--id", task_id, "--status", "failed",
            "--result", json.dumps({"reason": reason}),
        )

    # --- events ---

    def _to_event(self, message, kinds: list[EventKind]) -> RuntimeEvent | None:
        record = self._attempts.get(str(message.payload.get("dispatchId", "")))
        # An event for a fenced or unknown attempt never reaches the core:
        # a zombie cannot complete work that was taken from it.
        if record is None or record.fenced:
            return None
        attempt = record.ref
        if message.type == "worker_done" and "result" in kinds:
            outcome = "succeeded" if message.payload.get("outcome") == "succeeded" else "failed"
            return ResultEvent(kind="result", attempt=attempt, outcome=outcome, summary=message.body)
        if message.type == "question" and "question" in kinds:
            return QuestionEvent(kind="question", attempt=attempt, token=message.id, question=message.body)
        if message.type == "escalation" and "escalation" in kinds:
            return EscalationEvent(kind="escalation", attempt=attempt, reason=message.body)
        return None

    def drain(self, kinds: list[EventKind]) -> EventBatch:
        self._ensure_control()
        # `_orca` already unwraps the envelope (a failed `check` now raises
        # `OrcaCliError` instead of going through here as "no events" —
        # GRE-184 review round 1, finding 2); `parse_check_output` expects
        # the raw JSON text of `check --peek --json`, so the unwrapped
        # result is fed back in — the same trick `spawn.wait` uses to reuse
        # this one parser for both call sites.
        result = self._orca("orchestration", "check", "--terminal", self.coordinator, "--peek")
        batch = parse_check_output(json.dumps(result))
        # Only local events that made it into this batch may be consumed by
        # the matching ack; a control-lost filtered out by `kinds` must
        # survive until a drain actually delivers it (review M1).
        self._delivered_locals = [e for e in self._local_events if e.kind in kinds]
        events: list[RuntimeEvent] = list(self._delivered_locals)
        for message in batch.messages:
            event = self._to_event(message, kinds)
            if event is None:
                continue
            events.append(event)
            if event.kind == "result":
                self._attempts[event.attempt.attempt_id].open = False
            if event.kind == "question":
                self._pending_questions.add(event.token)
        return EventBatch(events=tuple(events), receipt=batch.delivery_id or f"local-{self._seq}")

    def ack(self, receipt: str) -> None:
        delivered = self._delivered_locals
        self._local_events = [e for e in self._local_events if not any(e is d for d in delivered)]
        self._delivered_locals = []
        if receipt.startswith("local-"):
            return
        self._orca("orchestration", "check", "--terminal", self.coordinator, "--ack", receipt)

    def respond(self, token: str, body: str) -> None:
        if token not in self._pending_questions:
            raise RuntimeError(f"question token {token} is spent or unknown; it cannot be answered twice")
        self._orca("orchestration", "reply", "--id", token, "--body", body, "--from", self.coordinator)
        self._pending_questions.discard(token)

    # --- observation ---

    def sweep(self) -> list[AttemptStatus]:
        self._ensure_control()
        out: list[AttemptStatus] = []
        for record in self._attempts.values():
            if not record.open:
                continue
            shown = self._orca("orchestration", "worker-show", "--dispatch", record.ref.attempt_id)
            alive = bool(shown.get("alive", False))
            quiet = shown.get("quietMs")
            if not alive:
                liveness, quiet = Liveness.GONE, None
            elif quiet:
                liveness = Liveness.IDLE
            else:
                liveness = Liveness.RUNNING
            out.append(
                AttemptStatus(
                    attempt_id=record.ref.attempt_id,
                    ticket_key=record.ref.ticket_key,
                    role=record.ref.context.role,
                    liveness=liveness,
                    quiet_for_ms=quiet,
                )
            )
        return out

    # --- the one sanctioned path for messaging a worker ---

    def message_worker(self, dispatch_id: str, msg: str) -> None:
        """Validates the dispatch state before sending. The Orchestrator
        never calls ``orca orchestration send`` raw: a send after
        ``worker_done`` sits in the mailbox and never wakes the worker."""

        record = self._attempts.get(dispatch_id)
        if record is None:
            raise MessageWorkerRefused(dispatch_id, "unknown")
        shown = self._orca("orchestration", "dispatch-show", "--task", record.task_id)
        # dispatch-show nests the record under result.dispatch. A missing
        # status is a loud failure, never a pass-through: reading "" here
        # once let every send through to dead workers (ad hoc test).
        dispatch = shown.get("dispatch")
        if not isinstance(dispatch, dict) or not dispatch.get("status"):
            raise RuntimeError(
                f"dispatch-show for task {record.task_id} returned no dispatch status; refusing to send blind"
            )
        status = str(dispatch["status"])
        # "stopped" is a killed worker (review M2): just as dead as
        # completed/failed — a send would sit in the mailbox forever.
        if status in ("completed", "failed", "stopped"):
            raise MessageWorkerRefused(dispatch_id, status)
        self._orca(
            "orchestration", "send", "--from", self.coordinator,
            "--to", f"dispatch:{dispatch_id}", "--subject", "coordinator message", "--body", msg,
        )
