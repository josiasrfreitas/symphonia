"""The prototype harness fake, promoted to a reusable test double.

TLDR: three pieces, all in-memory and deterministic:

- ``FakeRuntime`` — the hostile runtime from the GRE-150 harness: no
  graceful stop, no reaper, a control binding anyone can seize, a capped
  mailbox, stray shells on workspace creation, and a provider that can
  silently serve a tier nobody asked for.
- ``FakeRuntimeAdapter`` — the contract implemented directly over that
  runtime. This is the double other suites reuse.
- ``ScriptedOrcaCli`` — the same runtime dressed as the ``orca`` CLI, so
  the real ``OrcaRuntimeAdapter`` runs the conformance suite without a
  live Orca.

``Rig`` is the staging surface the conformance suite uses to make workers
answer, go quiet, deliver, or steal control — identical for both adapters.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

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
from .adapter import ROLE_BADGE, RunResult
from .launcher import TIER_MODELS


@dataclass
class FakeProcess:
    id: str
    workspace_id: str
    command: str
    display_name: str
    alive: bool = True
    quiet_ms: int = 0
    # What the provider recorded once the process answered; None on a
    # provider that records only requests — the entire point of GRE-163.
    recorded_answered_tier: CapabilityTier | None = None
    recorded_requested_tier: CapabilityTier = CapabilityTier.FAST
    # Truth, visible to the rig and to nobody else.
    actually_serving: CapabilityTier = CapabilityTier.FAST


@dataclass
class FakeWorkspace:
    id: str
    ticket_key: TicketKey
    setup_done: bool = False
    display_name: str = ""
    workspace_status: str = ""


@dataclass
class _MailEntry:
    message: dict
    acked: bool = False


class FakeRuntime:
    """State only; every hostile behavior the adapters must survive."""

    def __init__(self) -> None:
        self.seq = 0
        self.workspaces: dict[str, FakeWorkspace] = {}
        self.processes: dict[str, FakeProcess] = {}
        self.mailbox: list[_MailEntry] = []
        self.deliveries: dict[str, tuple[_MailEntry, ...]] = {}
        self.control_holder: str | None = None
        self.batch_cap = 3
        self.tasks: dict[str, str] = {}
        self.dispatches: dict[str, dict] = {}
        self.sent: list[dict] = []
        # Tiers the provider will silently serve, keyed by role name string.
        self.staging: dict[str, CapabilityTier] = {}

    def _next(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq}"

    def create_workspace(self, ticket_key: TicketKey) -> str:
        ws_id = self._next("ws")
        self.workspaces[ws_id] = FakeWorkspace(id=ws_id, ticket_key=ticket_key)
        # Creating a workspace opens a stray fallback shell. Every time.
        shell_id = self._next("shell")
        self.processes[shell_id] = FakeProcess(
            id=shell_id, workspace_id=ws_id, command="/bin/zsh", display_name="fallback shell"
        )
        return ws_id

    def run_setup(self, ws_id: str) -> None:
        self.workspaces[ws_id].setup_done = True

    def spawn(
        self,
        ws_id: str,
        command: str,
        display_name: str,
        requested: CapabilityTier,
        serving: CapabilityTier,
    ) -> str:
        pid = self._next("proc")
        self.processes[pid] = FakeProcess(
            id=pid,
            workspace_id=ws_id,
            command=command,
            display_name=display_name,
            recorded_requested_tier=requested,
            actually_serving=serving,
        )
        return pid

    def answer(self, pid: str, records_answers: bool) -> None:
        proc = self.processes[pid]
        proc.quiet_ms = 0
        proc.recorded_answered_tier = proc.actually_serving if records_answers else None

    def go_quiet(self, pid: str, ms: int) -> None:
        self.processes[pid].quiet_ms = ms

    def kill_process(self, pid: str) -> None:
        """The only way to stop a process. There is no signal to ask nicely."""
        self.processes[pid].alive = False

    def live_all(self) -> list[FakeProcess]:
        return [p for p in self.processes.values() if p.alive]

    def live_in(self, ws_id: str) -> list[FakeProcess]:
        return [p for p in self.live_all() if p.workspace_id == ws_id]

    def post(self, msg_type: str, body: str, payload: dict) -> str:
        msg_id = self._next("msg")
        self.mailbox.append(_MailEntry({"id": msg_id, "type": msg_type, "subject": msg_type, "body": body, "payload": payload}))
        return msg_id

    def check_peek(self) -> tuple[str, list[dict]]:
        """The oldest unacked FIFO batch, capped. Replayed until acked."""

        pending = [m for m in self.mailbox if not m.acked][: self.batch_cap]
        if not pending:
            return "", []
        delivery_id = self._next("del")
        self.deliveries[delivery_id] = tuple(pending)
        return delivery_id, [m.message for m in pending]

    def ack(self, delivery_id: str) -> None:
        for entry in self.deliveries.pop(delivery_id, ()):
            entry.acked = True

    def create_task(self, spec: str) -> str:
        task_id = self._next("task")
        self.tasks[task_id] = spec
        return task_id

    def create_dispatch(self, task_id: str, terminal: str) -> str:
        dispatch_id = self._next("disp")
        self.dispatches[dispatch_id] = {"task_id": task_id, "terminal": terminal, "status": "running"}
        return dispatch_id


# --- the reusable double -----------------------------------------------------

# The fake provider's own launch table — a provider that is not Orca.
FAKE_COMMANDS: dict[CapabilityTier, str] = {
    CapabilityTier.HIGH: "agent-a --model opus --effort high",
    CapabilityTier.STANDARD: "agent-a --model sonnet --effort medium",
    CapabilityTier.FAST: "agent-a --model haiku --effort low",
}


@dataclass
class _FakeContext:
    ref: ContextRef
    pid: str
    access: Access
    requested_tier: CapabilityTier


@dataclass
class _FakeAttempt:
    ref: AttemptRef
    pid: str
    open: bool = True
    fenced: bool = False


class FakeRuntimeAdapter:
    """``RuntimeAdapter`` implemented directly over ``FakeRuntime``.

    ``records_answers=True`` models the provider that records the tier that
    actually answered (``tier_verification`` on); False models the one that
    records only what was requested — the accepted GRE-163 debt.
    """

    def __init__(self, rt: FakeRuntime, coordinator: str, records_answers: bool = True):
        self.rt = rt
        self.coordinator = coordinator
        self.records_answers = records_answers
        self._contexts: dict[str, _FakeContext] = {}
        self._attempts: dict[str, _FakeAttempt] = {}
        self._ws_ids: dict[str, str] = {}
        self._ws_by_ticket: dict[TicketKey, WorkspaceRef] = {}
        self._pending_questions: set[str] = set()
        self._local_events: list[RuntimeEvent] = []
        self._delivered_locals: list[RuntimeEvent] = []
        if rt.control_holder is None:
            rt.control_holder = coordinator

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            tier_verification=self.records_answers, cooperative_completion=True
        )

    def _last_open_attempt(self) -> AttemptRef | None:
        for rec in reversed(list(self._attempts.values())):
            if rec.open:
                return rec.ref
        return None

    def _ensure_control(self) -> None:
        if self.rt.control_holder == self.coordinator:
            return
        observed = self.rt.control_holder
        self.rt.control_holder = self.coordinator
        attempt = self._last_open_attempt()
        if attempt is not None:
            self._local_events.append(
                ControlLostEvent(kind="control-lost", attempt=attempt, detail=f"observed holder {observed!r}; reclaimed")
            )

    # --- workspaces ---

    def create_workspace(self, ticket_key: TicketKey, *, base_branch: str) -> WorkspaceRef:
        self._ensure_control()
        ws_id = self.rt.create_workspace(ticket_key)
        self.rt.run_setup(ws_id)  # setup finishes before this call resolves
        for proc in self.rt.live_in(ws_id):  # and the stray shell is closed
            self.rt.kill_process(proc.id)
        name = ticket_key.lower()
        path = f"/workspaces/{name}"
        self._ws_ids[path] = ws_id
        ref = WorkspaceRef(ticket_key=ticket_key, id=ws_id, path=path, branch=name)
        self._ws_by_ticket[ticket_key] = ref
        return ref

    def find_workspace(self, ticket_key: TicketKey) -> WorkspaceRef | None:
        self._ensure_control()
        return self._ws_by_ticket.get(ticket_key)

    def destroy_workspace(self, workspace: WorkspaceRef) -> None:
        self._ensure_control()
        live = [c for c in self._contexts.values() if c.ref.workspace.path == workspace.path]
        if live:
            raise RuntimeError(
                f"refusing to destroy {workspace.ticket_key}: {len(live)} context(s) still live. "
                "An abandoned role may still be writing here."
            )
        self.rt.workspaces.pop(self._ws_ids[workspace.path], None)
        self._ws_by_ticket.pop(workspace.ticket_key, None)

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
        context_id = self.rt._next("fctx")
        serving = self.rt.staging.get(spec.role.value, spec.tier)
        pid = self.rt.spawn(
            self._ws_ids[workspace.path],
            FAKE_COMMANDS[spec.tier],
            f"{workspace.ticket_key}/{spec.role.value}/{context_id}",
            requested=spec.tier,
            serving=serving,
        )
        ref = ContextRef(id=context_id, role=spec.role, workspace=workspace)
        self._contexts[context_id] = _FakeContext(ref=ref, pid=pid, access=spec.access, requested_tier=spec.tier)
        return LaunchResult(context=ref, tier_evidence=self.verify_tier(ref))

    def verify_tier(self, context: ContextRef) -> TierEvidence:
        record = self._contexts.get(context.id)
        if record is None:
            return TierEvidence(kind="unverifiable", detail="no such context")
        proc = self.rt.processes[record.pid]
        # "observed" only from a field written after the process answered.
        if self.records_answers and proc.recorded_answered_tier is not None:
            return TierEvidence(kind="observed", tier=proc.recorded_answered_tier)
        return TierEvidence(kind="requested", tier=proc.recorded_requested_tier)

    def close_role(self, context: ContextRef) -> None:
        self._ensure_control()
        record = self._contexts.pop(context.id, None)
        if record is not None:
            self.rt.kill_process(record.pid)

    # --- attempts ---

    def dispatch(self, context: ContextRef, work: str) -> AttemptRef:
        self._ensure_control()
        record = self._contexts.get(context.id)
        if record is None:
            raise RuntimeError(f"context {context.id} is gone; dispatch has nowhere to land")
        attempt_id = self.rt._next("att")
        ref = AttemptRef(attempt_id=attempt_id, ticket_key=context.workspace.ticket_key, context=context)
        self._attempts[attempt_id] = _FakeAttempt(ref=ref, pid=record.pid)
        self.rt.go_quiet(record.pid, 0)
        return ref

    def kill(self, attempt: AttemptRef) -> KillReceipt:
        self._ensure_control()
        record = self._attempts.get(attempt.attempt_id)
        if record is not None:
            self.rt.kill_process(record.pid)
            record.open, record.fenced = False, True
            self._contexts.pop(record.ref.context.id, None)
        return KillReceipt(attempt=attempt, confirmed_gone=True)

    # --- events ---

    def drain(self, kinds: list[EventKind]) -> EventBatch:
        self._ensure_control()
        # Only local events delivered in this batch may be consumed by the
        # matching ack; one filtered out by `kinds` survives (review M1).
        self._delivered_locals = [e for e in self._local_events if e.kind in kinds]
        events: list[RuntimeEvent] = list(self._delivered_locals)
        delivery_id, messages = self.rt.check_peek()
        for message in messages:
            record = self._attempts.get(str(message["payload"].get("dispatchId", "")))
            # A fenced or unknown attempt's event never reaches the core.
            if record is None or record.fenced:
                continue
            attempt = record.ref
            if message["type"] == "worker_done" and "result" in kinds:
                outcome = "succeeded" if message["payload"].get("outcome") == "succeeded" else "failed"
                events.append(ResultEvent(kind="result", attempt=attempt, outcome=outcome, summary=message["body"]))
                record.open = False
            elif message["type"] == "question" and "question" in kinds:
                events.append(QuestionEvent(kind="question", attempt=attempt, token=message["id"], question=message["body"]))
                self._pending_questions.add(message["id"])
            elif message["type"] == "escalation" and "escalation" in kinds:
                events.append(EscalationEvent(kind="escalation", attempt=attempt, reason=message["body"]))
        return EventBatch(events=tuple(events), receipt=delivery_id or f"local-{self.rt.seq}")

    def ack(self, receipt: str) -> None:
        delivered = self._delivered_locals
        self._local_events = [e for e in self._local_events if not any(e is d for d in delivered)]
        self._delivered_locals = []
        if not receipt.startswith("local-"):
            self.rt.ack(receipt)

    def respond(self, token: str, body: str) -> None:
        if token not in self._pending_questions:
            raise RuntimeError(f"question token {token} is spent or unknown; it cannot be answered twice")
        self._pending_questions.discard(token)

    # --- observation ---

    def sweep(self) -> list[AttemptStatus]:
        self._ensure_control()
        out: list[AttemptStatus] = []
        for record in self._attempts.values():
            if not record.open:
                continue
            proc = self.rt.processes.get(record.pid)
            if proc is None or not proc.alive:
                liveness, quiet = Liveness.GONE, None
            elif proc.quiet_ms == 0:
                liveness, quiet = Liveness.RUNNING, 0
            else:
                liveness, quiet = Liveness.IDLE, proc.quiet_ms
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


# --- the scripted orca CLI ---------------------------------------------------

_MODEL_TIERS = {model: tier for tier, model in TIER_MODELS.items()}


def _requested_tier(command: str):
    """Which tier a launch command asks for, read out of its `--model`.

    Matched on the model rather than the whole string because the command
    also carries effort, permission and session flags (GRE-179) — a fake
    that compared the entire line would break every time the launcher
    gained a flag, which is exactly the drift it exists to catch.
    """

    parts = command.split()
    if "--model" not in parts:
        raise AssertionError(f"launch command names no model: {command!r}")
    model = parts[parts.index("--model") + 1]
    if model not in _MODEL_TIERS:
        raise AssertionError(f"launch command names unknown model {model!r}")
    return _MODEL_TIERS[model]


# Which role a title names, read out of its badge prefix (GRE-184 M3): the
# real-CLI-shaped title (`f"{emoji} {phase} · {ticket}"`) carries no context
# id, only the badge — the same table `adapter.launch_role` built it from.
_BADGE_PREFIX_TO_ROLE = {f"{emoji} {phase}": role for role, (emoji, phase, _) in ROLE_BADGE.items()}


def _role_of(title: str) -> RoleName | None:
    return next((role for prefix, role in _BADGE_PREFIX_TO_ROLE.items() if title.startswith(prefix)), None)


class ScriptedOrcaCli:
    """A callable with the ``Runner`` signature: the subset of the ``orca``
    CLI the adapter uses, answered from a ``FakeRuntime``. Deterministic —
    every stdout is a pure function of runtime state.

    Faithful to the real CLI's response contract: every response is wrapped
    in the ``{id, ok, result, _meta}`` envelope, and ``dispatch-show`` nests
    its record under ``result.dispatch``. The suite failed to catch the
    envelope bug precisely because an earlier version of this class returned
    bare payloads — keep this in sync with observed real output."""

    def __init__(self, rt: FakeRuntime):
        self.rt = rt
        self._ws_by_path: dict[str, str] = {}

    def __call__(self, argv: Sequence[str]) -> RunResult:
        argv = list(argv)

        def flag(name: str) -> str:
            return argv[argv.index(name) + 1] if name in argv else ""

        command = tuple(a for a in argv[1:] if not a.startswith("--"))[:2]
        handlers = {
            ("worktree", "create"): lambda: self._worktree_create(flag("--name"), flag("--linear-issue")),
            ("worktree", "list"): lambda: self._worktree_list(),
            ("worktree", "set"): lambda: self._worktree_set(
                flag("--worktree"), flag("--display-name"), flag("--workspace-status")
            ),
            ("worktree", "rm"): lambda: self._worktree_rm(flag("--worktree")),
            ("terminal", "create"): lambda: self._terminal_create(flag("--worktree"), flag("--title"), flag("--command")),
            ("terminal", "list"): lambda: self._terminal_list(flag("--worktree")),
            ("terminal", "wait"): lambda: {},
            ("terminal", "close"): lambda: self._terminal_close(flag("--terminal")),
            ("orchestration", "run-current"): lambda: {
                "run": {"terminal": self.rt.control_holder} if self.rt.control_holder else None
            },
            ("orchestration", "run-use"): lambda: self._run_use(flag("--from")),
            ("orchestration", "task-create"): lambda: {"task": {"id": self.rt.create_task(flag("--spec"))}},
            ("orchestration", "dispatch"): lambda: self._dispatch(flag("--task"), flag("--to")),
            ("orchestration", "task-update"): lambda: {},
            ("orchestration", "worker-stop"): lambda: self._worker_stop(flag("--dispatch")),
            ("orchestration", "worker-show"): lambda: self._worker_show(flag("--dispatch")),
            ("orchestration", "check"): lambda: self._check(flag("--ack"), "--peek" in argv),
            ("orchestration", "reply"): lambda: {},
            ("orchestration", "send"): lambda: self._send(flag("--to"), flag("--body")),
            ("orchestration", "dispatch-show"): lambda: self._dispatch_show(flag("--task")),
        }
        if command not in handlers:
            raise RuntimeError(f"scripted CLI does not know: {' '.join(argv)}")
        stdout = json.dumps(
            {
                "id": self.rt._next("req"),
                "ok": True,
                "result": handlers[command](),
                "_meta": {"runtimeId": "scripted"},
            }
        )
        return RunResult(stdout=stdout, stderr="", returncode=0)

    def _worktree_create(self, name: str, ticket_key: str) -> dict:
        # Faithful to the real CLI (GRE-184 M2): the stray fallback shell is
        # left behind here, not pre-cleaned — the caller is the one that
        # lists and closes it, same as `spawn.create_worktree` always did.
        ws_id = self.rt.create_workspace(ticket_key)
        self.rt.run_setup(ws_id)
        path = f"/workspaces/{name}"
        self._ws_by_path[path] = ws_id
        return {"worktree": {"id": ws_id, "path": path, "branch": name}}

    def _worktree_list(self) -> dict:
        items = []
        for path, ws_id in self._ws_by_path.items():
            ws = self.rt.workspaces.get(ws_id)
            if ws is None:
                continue
            # `branch` comes back as a full ref, not a bare name — measured
            # against the real CLI on 2026-08-11 (sample in
            # `conformance.RealCliContract.REAL_WORKTREE_LIST`). Emitting it
            # here is what makes `find_workspace`'s ref-stripping reachable
            # from a test; without it the fallback was the only branch value
            # any test ever produced, and the two sides disagreed silently.
            items.append(
                {"id": ws_id, "path": path, "branch": f"refs/heads/{Path(path).name}"}
            )
        return {"worktrees": items}

    def _worktree_set(self, selector: str, display_name: str, status: str) -> dict:
        ws_id = selector.removeprefix("id:")
        ws = self.rt.workspaces.get(ws_id)
        if ws is not None:
            ws.display_name = display_name
            ws.workspace_status = status
        return {}

    def _worktree_rm(self, selector: str) -> dict:
        path = selector.removeprefix("path:")
        ws_id = self._ws_by_path.pop(path, "")
        self.rt.workspaces.pop(ws_id, None)
        return {}

    def _terminal_create(self, selector: str, title: str, command: str) -> dict:
        ws_id = selector.removeprefix("id:")
        requested = _requested_tier(command)
        role = _role_of(title)
        serving = self.rt.staging.get(role.value, requested) if role else requested
        pid = self.rt.spawn(ws_id, command, title, requested=requested, serving=serving)
        return {"terminal": {"handle": pid}}

    def _terminal_list(self, selector: str) -> dict:
        # No `--worktree` is `sweep`'s call (`adapter.list_terminals`): the
        # real CLI answers with every live terminal, not just one
        # workspace's — verified 2026-08-11, see `adapter.list_terminals`.
        procs = self.rt.live_all() if not selector else self.rt.live_in(selector.removeprefix("id:"))
        terms = []
        for proc in procs:
            if proc.display_name == "fallback shell":
                # Blank title/command is what marks a bare fallback shell as
                # orphaned in the real CLI's response, not this fake's
                # internal command string.
                terms.append({"handle": proc.id, "title": "", "command": ""})
            else:
                terms.append({"handle": proc.id, "title": proc.display_name, "command": proc.command})
        return {"terminals": terms}

    def _terminal_close(self, terminal: str) -> dict:
        self.rt.kill_process(terminal)
        return {}

    def _run_use(self, sender: str) -> dict:
        self.rt.control_holder = sender
        return {}

    def _dispatch(self, task_id: str, terminal: str) -> dict:
        self.rt.go_quiet(terminal, 0)
        dispatch_id = self.rt.create_dispatch(task_id, terminal)
        # Real shape (GRE-184 M3): the id nests under "dispatch", and
        # `--return-preamble` is what actually carries the capability token
        # — there is no separate field for it on the envelope.
        return {
            "dispatch": {"id": dispatch_id},
            "preamble": f"orca orchestration send --dispatch-capability dcap_{dispatch_id} --type worker_done",
        }

    def _worker_stop(self, dispatch_id: str) -> dict:
        record = self.rt.dispatches[dispatch_id]
        self.rt.kill_process(record["terminal"])
        record["status"] = "stopped"
        return {"stopped": True}

    def _worker_show(self, dispatch_id: str) -> dict:
        proc = self.rt.processes[self.rt.dispatches[dispatch_id]["terminal"]]
        return {"alive": proc.alive, "quietMs": proc.quiet_ms}

    def _check(self, ack_id: str, peek: bool) -> dict:
        if ack_id:
            self.rt.ack(ack_id)
            return {}
        delivery_id, messages = self.rt.check_peek()
        return {"deliveryId": delivery_id, "messages": messages}

    def _send(self, to: str, body: str) -> dict:
        self.rt.sent.append({"to": to, "body": body})
        return {}

    def _dispatch_show(self, task_id: str) -> dict:
        # Like the real CLI: the record is nested under result.dispatch.
        for dispatch_id, record in self.rt.dispatches.items():
            if record["task_id"] == task_id:
                return {"dispatch": {"id": dispatch_id, "task_id": task_id, "status": record["status"]}}
        return {"dispatch": None}


# --- the staging rig ---------------------------------------------------------


class Rig:
    """What the conformance suite may do to the world that the adapter
    cannot: make processes answer or go quiet, deliver worker messages,
    stage a silent tier downgrade, steal the control binding."""

    def __init__(self, rt: FakeRuntime, records_answers: bool):
        self.rt = rt
        self.records_answers = records_answers

    def _proc_for(self, context: ContextRef) -> FakeProcess:
        marker = f"/{context.id}"
        for p in self.rt.processes.values():
            if p.display_name.endswith(marker):
                return p
        # Real-CLI-shaped titles (GRE-184 M3, OrcaRuntimeAdapter.launch_role)
        # carry no context id, only the role's badge — good enough because
        # the walkthrough never keeps two live contexts of the same role
        # alive at once.
        emoji, phase, _board = ROLE_BADGE[context.role]
        prefix = f"{emoji} {phase}"
        return next(p for p in self.rt.processes.values() if p.alive and p.display_name.startswith(prefix))

    def answer(self, context: ContextRef) -> None:
        self.rt.answer(self._proc_for(context).id, self.records_answers)

    def go_quiet(self, context: ContextRef, ms: int) -> None:
        self.rt.go_quiet(self._proc_for(context).id, ms)

    def serving_truth(self, context: ContextRef) -> CapabilityTier:
        return self._proc_for(context).actually_serving

    def stage(self, role: RoleName, tier: CapabilityTier) -> None:
        self.rt.staging[role.value] = tier

    def post_result(self, attempt: AttemptRef, outcome: str, summary: str) -> None:
        self.rt.post("worker_done", summary, {"dispatchId": attempt.attempt_id, "outcome": outcome})

    def post_question(self, attempt: AttemptRef, question: str) -> str:
        return self.rt.post("question", question, {"dispatchId": attempt.attempt_id})

    def post_escalation(self, attempt: AttemptRef, reason: str) -> None:
        self.rt.post("escalation", reason, {"dispatchId": attempt.attempt_id})

    def steal_control(self, holder: str) -> None:
        self.rt.control_holder = holder

    def control_holder(self) -> str | None:
        return self.rt.control_holder

    def workspace_state(self, ticket_key: TicketKey) -> tuple[bool, int]:
        ws = next(w for w in self.rt.workspaces.values() if w.ticket_key == ticket_key)
        return ws.setup_done, len(self.rt.live_in(ws.id))

    def workspace_gone(self, ticket_key: TicketKey) -> bool:
        return all(w.ticket_key != ticket_key for w in self.rt.workspaces.values())

    def set_dispatch_status(self, attempt: AttemptRef, status: str) -> None:
        self.rt.dispatches[attempt.attempt_id]["status"] = status
