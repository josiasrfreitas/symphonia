"""Everything this package asks of the `orca` CLI — one deterministic
invocation per function, failures loud — plus the parsing of what the
orchestration mailbox answers.

Facts measured against Orca 1.4.168 that this module encodes:

- Every `--json` response is wrapped in `{id, ok, result, _meta}`;
  `unwrap_envelope` is the one place that opens it.
- A rejected `worker_done` still answers `ok: true` — the refusal lives in
  `result.lifecycle` and the process exit code (`expect_lifecycle_ok`).
  `lifecycle` is present only on a rejection, so its absence must never be
  treated as failure.
- An injected dispatch mints a capability token that exists only as text in
  the returned preamble (the dispatch row's `capability_hash` is null).
- `worker-start` cannot set a model or permission flag; the only launch
  path that controls the command line is `terminal create --command`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, Union

from roles import RoleName


class OrcaCliError(RuntimeError):
    """One `orca` invocation failed — at the envelope layer (`ok: false`),
    the transport layer (no stdout), or the lifecycle layer (a refused
    `worker_done`)."""


def unwrap_envelope(
    argv: Sequence[str], stdout: str, stderr: str, returncode: int,
    *, expect_lifecycle_ok: bool = False,
) -> dict:
    if not stdout.strip():
        raise OrcaCliError(f"orca {' '.join(argv)} produced no output: {stderr.strip()}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise OrcaCliError(
            f"orca {' '.join(argv)} exited {returncode} with unparseable output: "
            f"{stderr.strip() or stdout.strip()}"
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
        if lifecycle.get("action") == "rejected" or returncode != 0:
            raise OrcaCliError(
                f"orca {' '.join(argv[:2])} rejected: "
                f"{lifecycle.get('code') or f'exit {returncode}'}: "
                f"{lifecycle.get('reason') or stderr.strip()}"
            )
    if data is None:
        return {}
    return data if isinstance(data, dict) else {"items": data}


def orca(*argv: str, expect_lifecycle_ok: bool = False) -> dict:
    """One orca call, envelope unwrapped, failures raised as `OrcaCliError`."""

    proc = subprocess.run(["orca", *argv, "--json"], capture_output=True, text=True)
    return unwrap_envelope(
        argv, proc.stdout, proc.stderr, proc.returncode,
        expect_lifecycle_ok=expect_lifecycle_ok,
    )


def _required(value: str, what: str) -> str:
    """An id the CLI must return. An empty one fails here, loudly, instead
    of surfacing later as a worktree with no path or a task keyed by ""."""

    if not value:
        raise OrcaCliError(f"orca returned no {what}; refusing to continue with an empty id")
    return value


def shell_join(argv: Sequence[str]) -> str:
    """`orca terminal create --command` takes one string, so an argv is
    joined here — quoting anything the shell would otherwise split."""

    return " ".join(
        f"'{part}'" if re.search(r"[\s()*?\"$]", part) else part for part in argv
    )


# --- worktrees -------------------------------------------------------------


@dataclass(frozen=True)
class Worktree:
    ticket: str
    id: str
    path: str
    branch: str


def default_base() -> str:
    """The repo's default base, read from git rather than assumed."""

    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    base = proc.stdout.strip()
    if not base:
        raise OrcaCliError(
            "cannot resolve origin/HEAD; run `git remote set-head origin -a` "
            "so the ticket branch has a known base"
        )
    return base


def _close_orphan_shell(worktree_id: str) -> None:
    """A bare `worktree create` leaves one untitled fallback shell.
    Best-effort: a tidy sidebar is never worth failing a worktree that
    otherwise came up fine."""

    listed = orca("terminal", "list", "--worktree", f"id:{worktree_id}")
    for term in listed.get("terminals", []):
        if term.get("title") or term.get("command"):
            continue
        try:
            orca("terminal", "close", "--terminal", str(term.get("handle")), "--tab")
        except OrcaCliError as exc:
            print(f"note: leftover shell not closed ({exc})", file=sys.stderr)


def create_worktree(ticket: str, *, base_branch: str) -> Worktree:
    """One checkout per Ticket Key, child of the caller's worktree in Orca
    lineage, off `base_branch` in git — never the current branch."""

    name = ticket.strip().lower()
    created = orca(
        "worktree", "create",
        "--name", name,
        "--parent-worktree", "active",
        "--base-branch", base_branch,
        "--setup", "run",
        "--linear-issue", ticket,
        "--comment", f"symphonia ticket {ticket}",
    )
    wt = created.get("worktree", created)
    wt_id = _required(str(wt.get("id", "")), "worktree id")
    path = _required(str(wt.get("path", "")), "worktree path")
    branch = str(wt.get("branch") or name)
    _close_orphan_shell(wt_id)
    return Worktree(ticket=ticket, id=wt_id, path=path, branch=branch)


def find_worktree(ticket: str) -> Worktree | None:
    """This ticket's worktree, or None. Matched on the path basename, not
    the display name — the display name is rewritten with the phase.

    An exact `<ticket>` basename always wins. Without one, Orca dedupes
    worktree names against its own database and hands back `<ticket>-<n>`
    permanently, so the fallback matches that strict suffix pattern. More
    than one suffixed candidate with no exact match raises loud: picking
    one silently risks sending the next role to a stale tree."""

    name = ticket.strip().lower()
    listed = orca("worktree", "list")
    items = listed.get("worktrees", listed.get("items", []))

    def _ref(wt: dict) -> Worktree:
        # `worktree list` answers `branch` as a full ref while `create`
        # answers the bare name; strip so both agree.
        branch = str(wt.get("branch") or name).removeprefix("refs/heads/")
        return Worktree(
            ticket=ticket, id=str(wt.get("id", "")), path=str(wt.get("path", "")),
            branch=branch,
        )

    suffix_re = re.compile(rf"^{re.escape(name)}-\d+$")
    suffixed = []
    for wt in items:
        basename = Path(str(wt.get("path", ""))).name
        if basename == name:
            return _ref(wt)
        if suffix_re.match(basename):
            suffixed.append(wt)

    if not suffixed:
        return None
    if len(suffixed) > 1:
        paths = ", ".join(str(wt.get("path", "")) for wt in suffixed)
        raise OrcaCliError(
            f"{ticket} has {len(suffixed)} live worktrees and none named "
            f"exactly after the ticket: {paths}. Remove the stale ones before "
            "continuing."
        )
    return _ref(suffixed[0])


def remove_worktree(path: str) -> None:
    orca("worktree", "rm", "--worktree", f"path:{path}")


# What the phase looks like in the Orca sidebar. Orca exposes no
# per-worktree colour, so the phase is carried by the three labels it does
# expose: the worktree display name, the terminal title, the board column.
ROLE_BADGE = {
    RoleName.PLANNER: ("🧭", "planning", "in-progress"),
    RoleName.IMPLEMENTER: ("🔨", "implementing", "in-progress"),
    RoleName.SPEC_REVIEWER: ("🔍", "spec review", "in-review"),
    RoleName.STANDARDS_REVIEWER: ("📐", "standards review", "in-review"),
}


def set_phase(worktree: Worktree, role: RoleName) -> None:
    emoji, phase, board = ROLE_BADGE[role]
    orca(
        "worktree", "set",
        "--worktree", f"id:{worktree.id}",
        "--display-name", f"{emoji} {worktree.ticket} · {phase}",
        "--workspace-status", board,
    )


# --- terminals and dispatches ---------------------------------------------


def create_terminal(worktree: Worktree, role: RoleName, command: Sequence[str]) -> str:
    """Start the role's terminal with an already-decided launch command and
    wait for readiness — losing the first prompt costs a whole spawn."""

    emoji, phase, _board = ROLE_BADGE[role]
    created = orca(
        "terminal", "create",
        "--worktree", f"id:{worktree.id}",
        "--title", f"{emoji} {phase} · {worktree.ticket}",
        "--command", shell_join(command),
    )
    term = created.get("terminal", created)
    terminal = _required(str(term.get("handle", term.get("terminal", ""))), "terminal handle")
    orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle", "--timeout-ms", "120000")
    return terminal


_CAPABILITY = re.compile(r"--dispatch-capability\s+(\S+)")


def dispatch(terminal: str, work: str, *, ticket: str, role: str) -> dict:
    """Create the Task and inject the dispatch. Returns `{task, dispatch,
    capability}`. A dispatch that minted no capability is rolled back (task
    failed, terminal closed) and raised: the role could never report."""

    task = orca("orchestration", "task-create", "--spec", work)
    task_id = _required(str(task.get("task", task).get("id", "")), "task id")
    dispatched = orca(
        "orchestration", "dispatch", "--task", task_id,
        "--to", terminal, "--inject", "--return-preamble",
    )
    dispatch_id = _required(str((dispatched.get("dispatch") or {}).get("id", "")), "dispatch id")
    found = _CAPABILITY.search(dispatched.get("preamble") or "")
    if found is None:
        settle_task(task_id, "no dispatch capability in the preamble")
        try:
            close_terminal(terminal, tab=True)
        except OrcaCliError as exc:
            print(f"note: terminal not closed ({exc})", file=sys.stderr)
        raise OrcaCliError(
            f"dispatch for {ticket}/{role} minted no capability, so the role "
            f"could never report; the terminal and task were rolled back. Retry the spawn."
        )
    return {"task": task_id, "dispatch": dispatch_id, "capability": found.group(1)}


def dispatch_status(task_id: str, *, default: str = "?") -> str:
    shown = orca("orchestration", "dispatch-show", "--task", task_id)
    return str((shown.get("dispatch") or {}).get("status", default))


def stop_worker(dispatch_id: str) -> None:
    """Best-effort: `worker-stop` only knows dispatches `worker-start`
    created, and this package launches through `terminal create` — a
    failure here is expected, not exceptional."""

    orca("orchestration", "worker-stop", "--dispatch", dispatch_id)


def close_terminal(handle: str, *, tab: bool) -> None:
    argv = ["terminal", "close", "--terminal", handle]
    if tab:
        argv.append("--tab")
    orca(*argv)


def settle_task(task_id: str, reason: str) -> None:
    """A killed role's Task would otherwise sit in `dispatched` forever,
    which reads as an attempt still in flight."""

    orca(
        "orchestration", "task-update", "--id", task_id, "--status", "failed",
        "--result", json.dumps({"reason": reason}),
    )


def list_terminals() -> set[str]:
    """Every terminal handle currently live in Orca, across every worktree
    — `terminal list` with no selector returns all of them (verified
    against the real CLI, 2026-08-11)."""

    listed = orca("terminal", "list")
    return {str(t.get("handle")) for t in listed.get("terminals", []) if t.get("handle")}


# --- the orchestration mailbox --------------------------------------------


@dataclass(frozen=True)
class OrchestrationMessage:
    """One mailbox message, field-for-field."""

    id: str
    type: str
    subject: str
    body: str
    payload: dict
    thread_id: str = ""
    sender: str = ""


@dataclass(frozen=True)
class CheckBatch:
    delivery_id: str
    messages: tuple[OrchestrationMessage, ...]


def _first(record: dict, *keys: str, default: str = "") -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return default


def _payload_of(record: dict) -> dict:
    raw = _first(record, "payload", default=None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def parse_check_output(text: str) -> CheckBatch:
    """Parse `check --peek/--wait --json` output. The real CLI wraps
    everything in the `{id, ok, result, _meta}` envelope — the batch lives
    under `result` and the envelope's top-level `id` is the request id, NOT
    a delivery id. Also tolerates an unwrapped dict or a bare list."""

    data = json.loads(text) if text.strip() else []
    if isinstance(data, dict) and "ok" in data:
        if not data.get("ok"):
            raise ValueError(f"orca check failed: {json.dumps(data.get('error'))}")
        data = _first(data, "result", default={})
    if isinstance(data, dict):
        delivery = str(_first(data, "deliveryId", "delivery_id"))
        raw_messages = _first(data, "messages", default=[])
    else:
        delivery, raw_messages = "", data
    messages = tuple(
        OrchestrationMessage(
            id=str(_first(m, "id", "messageId", "message_id")),
            type=str(_first(m, "type")),
            subject=str(_first(m, "subject")),
            body=str(_first(m, "body")),
            payload=_payload_of(m),
            thread_id=str(_first(m, "threadId", "thread_id")),
            sender=str(_first(m, "from", "sender")),
        )
        for m in raw_messages
        if isinstance(m, dict)
    )
    return CheckBatch(delivery_id=delivery, messages=messages)


@dataclass(frozen=True)
class PlanQuestion:
    """A worker asked a question (`ask` / `--type question`)."""

    kind: str
    message_id: str
    task_id: str
    dispatch_id: str
    question: str


@dataclass(frozen=True)
class WorkerDone:
    """A worker reported its terminal outcome."""

    kind: str
    message_id: str
    task_id: str
    dispatch_id: str
    outcome: str
    summary: str


GateEvent = Union[PlanQuestion, WorkerDone]


def gate_events(messages: tuple[OrchestrationMessage, ...]) -> tuple[GateEvent, ...]:
    """Map mailbox messages to typed gate events by their declared `type`
    field — never by reading prose. Other types are skipped.

    Only the two events a coordinator can actually observe: the answer to
    an `ask` is delivered to the worker that asked and never reaches the
    coordinator's mailbox, so there is no reply event here — `spawn
    verdict` writes the reply, `spawn submit` parses it."""

    out: list[GateEvent] = []
    for m in messages:
        task_id = str(_first(m.payload, "taskId", "task_id"))
        dispatch_id = str(_first(m.payload, "dispatchId", "dispatch_id"))
        if m.type == "question":
            out.append(PlanQuestion("plan-question", m.id, task_id, dispatch_id, m.body))
        elif m.type == "worker_done":
            outcome = str(_first(m.payload, "outcome"))
            out.append(WorkerDone("worker-done", m.id, task_id, dispatch_id, outcome, m.body))
    return tuple(out)
