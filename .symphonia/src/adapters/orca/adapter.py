"""The real Runtime Adapter for Orca.

TLDR: implements the ``RuntimeAdapter`` contract by shelling out to the
``orca`` CLI, one deterministic invocation per contract call. The command
runner is injectable so the conformance suite can drive the exact same
adapter against a scripted CLI. The Capability Tier translation table lives
here — roles declare a tier, only this file knows model names.

Known limitation (GRE-175 review, M3): ``RoleSpec.briefing`` is not yet
delivered to the agent — ``launch_role`` starts the bare model command and
``dispatch`` sends only the work body, so the briefing is silently
discarded today. How the briefing reaches the agent (launch prompt vs
first dispatch) is a user decision deferred to the merge gate; do not wire
it here without that decision.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
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
    and ``spawn.orca`` (GRE-184 M1 — the fronteira condition with GRE-185:
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
    data = json.loads(result.stdout)
    if isinstance(data, dict) and "ok" in data:
        if not data.get("ok"):
            err = data.get("error") or {}
            raise OrcaCliError(f"orca {' '.join(argv)} failed: {err.get('code')}: {err.get('message')}")
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
    repo: str = ""

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
        open attempt, the work most at risk while control was lost."""

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

    def create_workspace(self, ticket_key: TicketKey, *, branch: str) -> WorkspaceRef:
        self._ensure_control()
        argv = ["worktree", "create", "--name", branch, "--comment", ticket_key]
        if self.repo:
            argv += ["--repo", self.repo]
        created = self._orca(*argv)
        path = _required(str(created.get("path", created.get("worktreePath", ""))), "worktree path")
        return WorkspaceRef(ticket_key=ticket_key, path=path, branch=branch)

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
        # Ticket Key and context identity travel in the title: no process id
        # is exposed to correlate on.
        title = f"{workspace.ticket_key}/{spec.role.value}/{context_id}"
        plan = build_launch(
            spec.role,
            session_id=str(uuid.uuid4()),
            workspace=workspace.path,
            tier=spec.tier,
            access=spec.access,
        )
        created = self._orca(
            "terminal",
            "create",
            "--worktree",
            f"path:{workspace.path}",
            "--title",
            title,
            "--command",
            plan.command,
        )
        terminal = _required(str(created.get("terminal", created.get("handle", ""))), "terminal handle")
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

    def dispatch(self, context: ContextRef, work: str) -> AttemptRef:
        self._ensure_control()
        record = self._contexts.get(context.id)
        if record is None:
            raise RuntimeError(f"context {context.id} is gone; dispatch has nowhere to land")
        ticket = context.workspace.ticket_key
        task = self._orca(
            "orchestration", "task-create", "--spec", work,
            "--task-title", f"{ticket}/{context.role.value}", "--from", self.coordinator,
        )
        task_id = _required(str(task.get("taskId", task.get("task_id", ""))), "task id")
        dispatched = self._orca(
            "orchestration", "dispatch", "--task", task_id,
            "--to", record.terminal, "--from", self.coordinator,
        )
        attempt_id = _required(
            str(dispatched.get("dispatchId", dispatched.get("dispatch_id", ""))), "dispatch id"
        )
        ref = AttemptRef(attempt_id=attempt_id, ticket_key=ticket, context=context)
        self._attempts[attempt_id] = _AttemptRecord(ref=ref, task_id=task_id)
        return ref

    def kill(self, attempt: AttemptRef) -> KillReceipt:
        self._ensure_control()
        stopped = self._orca("orchestration", "worker-stop", "--dispatch", attempt.attempt_id)
        record = self._attempts.get(attempt.attempt_id)
        if record is not None:
            record.open, record.fenced = False, True
            self._contexts.pop(record.ref.context.id, None)
        return KillReceipt(attempt=attempt, confirmed_gone=bool(stopped.get("stopped", True)))

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
        batch = parse_check_output(
            self.runner(["orca", "orchestration", "check", "--terminal", self.coordinator, "--peek", "--json"]).stdout
        )
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
