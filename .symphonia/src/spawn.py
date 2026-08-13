#!/usr/bin/env python3
"""The whole spawn interface the Orchestrator is allowed to use.

TLDR: the four role verbs take one argument each — a Ticket Key — plus
`status`, `retire`, `sweep`, `brief`, and the plan gate pair `wait`/
`verdict`. The Orchestrator never picks a model, a permission flag, a
worktree or a launch path; every one of those is decided here. If a command
below does not express what you need, that is a package change, not an
improvisation at the terminal.

    .symphonia/bin/spawn plan             GRE-181
    .symphonia/bin/spawn implement        GRE-181
    .symphonia/bin/spawn review-spec      GRE-181
    .symphonia/bin/spawn review-standards GRE-181
    .symphonia/bin/spawn status          [GRE-181]
    .symphonia/bin/spawn retire           GRE-181 planner
    .symphonia/bin/spawn sweep           [GRE-181]
    .symphonia/bin/spawn brief            GRE-181 --file cut.md
    .symphonia/bin/spawn wait             [--ack <delivery_id>] [--timeout-ms <ms>]
    .symphonia/bin/spawn verdict          GRE-181 approved|revise [--notes <text>|--notes-file <path>]

And two verbs a ROLE runs, inside its own dispatched terminal — the return
half of the same interface. No role ever types `orca orchestration` by hand:

    .symphonia/bin/spawn submit           GRE-181 --file plan.md
    .symphonia/bin/spawn done             GRE-181 --outcome succeeded --file report.md

`plan` derives a worktree from the repo's default base; every later role
reuses that same worktree, so one Ticket Key means one checkout from plan
to PR. `wait` is the one loop that hears back from every role — it turns
mailbox messages into typed events and hands them to `gate.run`, which
executes what comes back (label the ticket, retire a role, flag a
divergence). `verdict` is how the human's decision reaches the planner: it
never comes from an agent typing `APPROVED`/`REVISE` into a reply by hand.

Every verb refuses with `Refusal`, never `SystemExit`: `main()` is the one
place a refusal becomes an exit code, so an importer of these verbs can
catch failures with a plain `except Exception`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import claude as _claude
import gate as _gate
import journal as _journal
import linear as _linear
import orca as _orca
import registry as _registry
import roles as _roles
import setup_worktree as _setup_worktree

# `.symphonia/`, the package root: everything that reads a Resource by path
# — `roles/`, `config.json` — anchors on this.
PACKAGE = Path(__file__).resolve().parents[1]
ROLES_DIR = PACKAGE / "roles"

RoleName = _roles.RoleName
Access = _roles.Access

GATE_ROLE = RoleName.PLANNER
"""Only the planner's completion is a Human Gate today; `wait` only reacts
to gate events keyed to this role's dispatch."""

# The baton between roles is a document dropped at `handoff_dir` — outside
# the repository on purpose: never committed, never travels with the
# branch. Its only job is carrying context from the role that just died to
# the one about to start.
_HANDOFF_DIR_DEFAULT = "~/orca/.context"


class Refusal(Exception):
    """A verb refused to act; the message names why and what to do
    instead. `main()` is where a refusal becomes a nonzero exit — as an
    exception it stays catchable by `except Exception`, which a
    `SystemExit` raised mid-library is not."""


def _handoff_dir() -> str:
    config = json.loads((PACKAGE / "config.json").read_text())
    return config.get("handoff_dir", _HANDOFF_DIR_DEFAULT)


def _policies() -> dict[RoleName, _roles.RolePolicy]:
    # No cache: every verb call re-reads the four small role files, so an
    # edit takes effect on the next spawn without restarting anything.
    return _roles.load_policies(ROLES_DIR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the Execution Brief (every role's input) ------------------------------


def _current_branch(workspace: str) -> str:
    proc = subprocess.run(
        ["git", "-C", workspace, "branch", "--show-current"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() or "(unknown)"


def _head(workspace: str) -> str:
    """The commit a worktree is on, or `""` if git could not answer.
    Recorded at dispatch as `head_at_dispatch`, the baseline `done()`
    compares against to catch an empty `succeeded`."""

    proc = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _handoff_file(ticket: str) -> Path | None:
    """The one handoff a role's Brief may point to:
    `{handoff_dir}/{ticket_lower}.md`, the only file a role's own "How to
    finish" instructs it to write — a role must never have to choose which
    of two documents to believe."""

    path = Path(os.path.expanduser(_handoff_dir())) / f"{ticket.lower()}.md"
    return path if path.exists() else None


def build_brief(role: RoleName, ticket: str, workspace: str, *, tracker=None) -> str:
    """Assembles the Execution Brief injected at dispatch: extracts the
    `io:brief-template` block from the role's own file and fills it from
    the ticket. The role opens with the ticket already in hand — zero tool
    calls needed to fetch it."""

    ticket = ticket.upper()
    policy = _policies()[role]
    role_path = ROLES_DIR / policy.role_file
    template = _gate.extract_block(role_path.read_text(), "md io:brief-template")

    tracker = tracker or _linear.LinearTracker()

    item = tracker.get_item(ticket)
    comments = tracker.list_comments(item.ref.id)
    comment_text = "\n\n".join(
        f"**{c.author_name or 'unknown'} · {c.created_at[:10]}**\n\n{c.body}"
        for c in comments
    ) or "None."

    handoff_file = _handoff_file(ticket)
    handoff_text = (
        f"- {handoff_file}" if handoff_file
        else "None — this is the first role on this ticket."
    )

    values = {
        "ticket_key": ticket,
        "ticket_lower": ticket.lower(),
        "role": role.value,
        "role_file": f".symphonia/roles/{policy.role_file}",
        "workspace": workspace,
        "branch": _current_branch(workspace),
        "title": item.title,
        "url": item.ref.url,
        "description": item.body or "(no description)",
        "comments": comment_text,
        "handoff_files": handoff_text,
        "handoff_dir": _handoff_dir(),
        "handoff_hint": _claude.handoff_hint(),
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise Refusal(
            f"Brief template in {role_path} references unknown placeholder {exc}"
        )


# --- role identity: how a role finds its own record ------------------------


def own_record(ticket: str, data: dict | None = None) -> tuple[str, dict]:
    """The record of the role calling this, from inside its own terminal.

    Identity comes from `ORCA_TERMINAL_HANDLE`, which Orca exports into the
    pane and `spawn` stored as `terminal` — an exact match, never a guess.
    The fallback (the worktree's git toplevel) exists for a pane whose
    environment did not carry the handle; if it matches more than one live
    role, this fails loudly rather than reporting on someone else's
    behalf."""

    ticket = ticket.upper()
    data = _registry.read() if data is None else data
    live = {
        key: rec for key, rec in data.items()
        if rec.get("ticket") == ticket and not rec.get("retired")
    }
    if not live:
        raise Refusal(f"no live spawn recorded for {ticket}; is this the right Ticket Key?")

    handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    if handle:
        for key, rec in live.items():
            if rec.get("terminal") == handle:
                return key, rec
        raise Refusal(
            f"terminal {handle} is not a recorded role of {ticket}; "
            f"this command is run by a role, from inside its own dispatched terminal"
        )

    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    matches = [(key, rec) for key, rec in live.items() if rec.get("worktree") == toplevel]
    if len(matches) == 1:
        return matches[0]
    raise Refusal(
        f"cannot tell which role is calling: ORCA_TERMINAL_HANDLE is unset and "
        f"{len(matches)} live roles of {ticket} share the worktree {toplevel or '(unknown)'}"
    )


# --- the spawn itself ------------------------------------------------------


def spawn(role: RoleName, ticket: str, *, fresh_worktree: bool, tier: str | None = None) -> dict:
    """Create or reuse the ticket's worktree, launch the role at its
    declared tier, inject the Execution Brief as the dispatch, and record
    everything. A failure mid-sequence leaves no orphan: after a fresh
    worktree is created, ANY later failure — badge, launch, brief
    (Linear included), dispatch, a Ctrl-C during the wait for the terminal
    — closes the terminal it may have opened and removes that worktree, so
    a retry of `spawn plan` starts clean instead of refusing on a leftover
    checkout. A reused worktree is never destroyed."""

    ticket = ticket.upper()
    worktree = _orca.find_worktree(ticket)
    fresh = False
    if fresh_worktree:
        if worktree is not None:
            raise Refusal(
                f"{ticket} already has a worktree at {worktree.path}. "
                f"Planning runs once per ticket; use `implement` to continue in it."
            )
        worktree = _orca.create_worktree(ticket, base_branch=_orca.default_base())
        fresh = True
        # The env files are gitignored, so the new checkout does not have
        # them — it looks complete and fails the first time something reads
        # `.env`. Running this twice is harmless.
        print(json.dumps(_setup_worktree.setup(Path(worktree.path))), file=sys.stderr)
    else:
        if worktree is None:
            raise Refusal(
                f"{ticket} has no worktree yet. Run `spawn plan {ticket}` first — "
                f"every role after the planner reuses the checkout planning created."
            )

    # `--tier` is a human override, not a knob for the Orchestrator: the
    # policy is the default precisely so no agent has to choose a model.
    policy = _policies()[role]
    if tier:
        try:
            policy = dataclasses.replace(policy, tier=_roles.CapabilityTier(tier))
        except ValueError:
            raise Refusal(
                f"unknown tier {tier!r}; known: "
                f"{', '.join(t.value for t in _roles.CapabilityTier)}"
            )

    terminal = None
    try:
        _orca.set_phase(worktree, role)
        launch = _claude.prepare(workspace=worktree.path, policy=policy)
        terminal = _orca.create_terminal(worktree, role, launch.command)
        # Every role's input is the Execution Brief, injected: the ticket
        # is read and formatted here, never left for the role to go fetch.
        spec = build_brief(role, ticket, worktree.path)
        dispatched = _orca.dispatch(terminal, spec, ticket=ticket, role=role.value)
    except BaseException:
        # `BaseException`, not a tuple to keep in sync: a KeyboardInterrupt
        # during the 120s terminal wait must roll back like any other
        # failure, or the leftover checkout blocks the retry.
        if fresh:
            # Best-effort, terminal first: `worktree rm` on a checkout
            # whose pane is still open would otherwise be the one failure
            # this rollback cannot survive. The original failure is what
            # gets reported either way.
            if terminal is not None:
                try:
                    _orca.close_terminal(terminal, tab=True)
                except _orca.OrcaCliError:
                    pass
            try:
                _orca.remove_worktree(worktree.path)
            except _orca.OrcaCliError:
                pass
        raise

    record = {
        "ticket": ticket,
        "role": role.value,
        "tier": policy.tier.value,
        "access": policy.access.value,
        "worktree": worktree.path,
        "worktree_id": worktree.id,
        "head_at_dispatch": _head(worktree.path),
        "terminal": terminal,
        "task": dispatched["task"],
        "dispatch": dispatched["dispatch"],
        "capability": dispatched["capability"],
        "gate_state": IDLE,
        "approval_rounds": 0,
        "session_id": launch.session_id,
        "transcript": launch.transcript,
        "command": _orca.shell_join(launch.command),
    }
    with _registry.transaction() as data:
        data[_registry.key(ticket, role.value)] = record
    return record


# --- observation -----------------------------------------------------------


def status(ticket: str | None) -> list[dict] | dict:
    """Deterministic state per spawn: what the dispatch says, and what tier
    evidence the transcript can produce (`claude.observe` reads it — never
    this function guessing).

    With a `ticket`, a flat list. Without one — the whole-registry view —
    that same list under `"spawns"` alongside `"pending_delivery"`: the
    open Delivery id, read from the persisted receipt, so an unacked `wait`
    is visible without recovering an id from old stdout."""

    out = []
    for key, rec in sorted(_registry.read().items()):
        if ticket and not key.startswith(ticket.upper() + "/"):
            continue
        try:
            dispatch_status = _orca.dispatch_status(rec["task"])
        except _orca.OrcaCliError:
            dispatch_status = "unknown"
        if rec.get("transcript"):
            evidence = _claude.observe(rec["transcript"], _roles.CapabilityTier(rec["tier"]))
            evidence_kind, evidence_detail = evidence.kind, evidence.detail
        else:
            evidence_kind = "unverifiable"
            evidence_detail = "no transcript recorded for this spawn"
        out.append(
            {
                "key": key,
                "dispatch_status": dispatch_status,
                "tier": rec.get("tier"),
                "evidence_kind": evidence_kind,
                "evidence_detail": evidence_detail,
                # A role stuck at `verdict-approved` with an old
                # `last_event_at` is one whose worker_done never landed.
                "gate_state": rec.get("gate_state", IDLE),
                "approval_rounds": rec.get("approval_rounds", 0),
                "last_event_at": rec.get("last_event_at"),
                "worktree": rec["worktree"],
                "terminal": rec["terminal"],
            }
        )
    if ticket is None:
        return {"pending_delivery": _journal.read_receipt(_registry.runtime_dir()), "spawns": out}
    return out


def retire(ticket: str, role_value: str) -> dict:
    """Stop one role and close its pane. The worktree survives — the next
    role needs it. The Orchestrator's own verb — not idempotent, unlike
    `teardown`: a human retiring an already-dead role a second time still
    gets the same effects run again, not a silent no-op. Runs inside its
    own registry transaction, so a concurrent `wait` cannot put a stale
    un-retired copy of this record back."""

    key = _registry.key(ticket, role_value)
    with _registry.transaction() as data:
        rec = data.get(key)
        if rec is None:
            raise Refusal(f"no spawn recorded for {key}")

        # A role can name its own terminal here — `retire GRE-188 planner`
        # run from inside the planner's own pane. `stop_worker`/
        # `close_terminal` would kill the process running this very
        # function, so the caller loses its terminal mid-command and never
        # sees the task get settled. Refuse instead: retiring is the
        # Orchestrator's verb.
        own_terminal = os.environ.get("ORCA_TERMINAL_HANDLE", "")
        if own_terminal and own_terminal == rec.get("terminal"):
            raise Refusal(
                f"{key} is the role running this command; retiring it would close "
                f"this terminal mid-command and lose whatever it has not reported "
                f"yet. Report first and let the Orchestrator retire you."
            )

        return _run_teardown(data, ticket, role_value)


def teardown(ticket: str, role_value: str, *, data: dict | None = None) -> dict:
    """Same effects as `retire`, minus the self-guard, plus idempotence: a
    record already marked `retired` returns immediately. Called by
    `gate.run` when a `worker_done` arrives, and by `sweep` for a record
    whose world is already gone — both a replayed Delivery and a repeated
    `sweep` must be a no-op.

    `data` is the registry dict of an already-open transaction (`wait` and
    `sweep` pass theirs — flock does not nest); without it, this opens its
    own."""

    if data is None:
        with _registry.transaction() as opened:
            return teardown(ticket, role_value, data=opened)

    key = _registry.key(ticket, role_value)
    rec = data.get(key)
    if rec is None:
        raise Refusal(f"no spawn recorded for {key}")
    if rec.get("retired"):
        return {"retired": key, "effects": ["already retired"], "worktree_kept": rec["worktree"]}
    return _run_teardown(data, ticket, role_value)


def _run_teardown(data: dict, ticket: str, role_value: str) -> dict:
    """Every effect of ending one role's dispatch, best-effort, in a fixed
    order: `stop_worker` -> close the terminal -> settle the Task -> mark
    `retired`. Nothing here raises on a failed effect: a dead terminal or
    an unreachable dispatch is exactly what this cleans up after, so each
    failure becomes an `effects` entry instead of aborting before `retired`
    is written. Mutates `data` — the caller's open transaction writes it."""

    key = _registry.key(ticket, role_value)
    rec = data.get(key)
    if rec is None:
        raise Refusal(f"no spawn recorded for {key}")

    effects = []
    try:
        _orca.stop_worker(rec["dispatch"])
        effects.append("worker-stop")
    except _orca.OrcaCliError:
        pass  # expected for terminal-created dispatches

    try:
        _orca.close_terminal(rec["terminal"], tab=True)
        effects.append("terminal closed")
    except _orca.OrcaCliError as exc:
        effects.append(f"terminal not closed ({exc})")

    # A killed role leaves its Task in `dispatched` forever, which reads as
    # an attempt still in flight. Settle it — best-effort, so a CLI outage
    # here cannot stop `retired` from being written.
    dispatch_status = ""
    try:
        dispatch_status = _orca.dispatch_status(rec["task"], default="")
    except _orca.OrcaCliError as exc:
        effects.append(f"task NOT settled ({exc})")
    else:
        if dispatch_status not in ("completed", "failed"):
            try:
                _orca.settle_task(rec["task"], f"{role_value} retired by the Orchestrator")
                effects.append("task settled as failed")
            except _orca.OrcaCliError as exc:
                effects.append(f"task NOT settled ({exc})")

    rec["retired"] = True
    return {
        "retired": key,
        "dispatch_was": dispatch_status,
        "effects": effects,
        "worktree_kept": rec["worktree"],
    }


def sweep(ticket: str | None) -> list[dict]:
    """Audit the registry for a record whose world is already gone — its
    terminal not among Orca's live handles, or its worktree missing from
    disk — and tear it down without being told which ticket/role died.
    What `retired` records are for `wait`, this is for everything that
    never reported at all: the app quit, a machine lost power, a worktree
    deleted by hand.

    A live record is reported, not touched. The whole read-decide-teardown
    loop runs inside one registry transaction so a concurrent
    `wait`/`verdict` cannot silently revert a teardown whose effects were
    already irreversible.

    A `list_terminals()` that comes back empty is refused rather than acted
    on: every unretired record would read as "terminal not live" and this
    loop would tear down every live role on the strength of one degraded
    CLI response."""

    live_terminals = _orca.list_terminals()
    if not live_terminals:
        raise Refusal(
            "list_terminals() returned no terminals at all; refusing to treat "
            "every unretired record as an orphan. Retry once the CLI is responding."
        )
    out = []
    with _registry.transaction() as data:
        for key, rec in sorted(data.items()):
            if ticket and not key.startswith(ticket.upper() + "/"):
                continue
            if rec.get("retired"):
                continue
            reasons = []
            if rec["terminal"] not in live_terminals:
                reasons.append("terminal not live")
            if not Path(rec["worktree"]).exists():
                reasons.append("worktree missing")
            if not reasons:
                out.append({"key": key, "live": True})
                continue
            out.append({
                "key": key, "live": False, "reason": reasons,
                "teardown": teardown(rec["ticket"], rec["role"], data=data),
            })
    return out


# --- the plan gate ---------------------------------------------------------

IDLE = _gate.IDLE


def wait(*, ack: str | None, timeout_ms: int) -> dict:
    """The one loop the Orchestrator uses to hear back from every role.
    Wraps the blocking mailbox check, then hands the batch to `gate.run` —
    never the Orchestrator reading a message and deciding by itself.

    Replay-safe: an unacked Delivery replays the same batch, but every
    action is guarded by the recorded `gate_state` (and, for a
    plan-question, the event's own id), so a repeat is a no-op.

    `ack` defaults to the receipt persisted from the previous call: the
    Delivery id no longer has to survive in a terminal's stdout to be
    ackable. An explicit `--ack` still wins — it exists for re-acking a
    specific id by hand.

    Ordering: the journal append happens inside the transaction BEFORE
    `gate.run` (the raw event survives a crash mid-processing), and the
    receipt is written AFTER the transaction commits — the delivery is only
    marked acked once the outcome it acks is durable. A crash before the
    receipt leaves the delivery unacked, Orca redelivers, and the replay is
    a no-op. The failure this ordering prevents is a suppressed delivery; a
    lost receipt only costs a harmless replay."""

    if ack is None:
        ack = _journal.read_receipt(_registry.runtime_dir())

    started = time.monotonic()
    batch = _orca.check_wait(ack=ack, timeout_ms=timeout_ms)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    events = _orca.gate_events(batch.messages)
    raw_by_id = {m.id: m for m in batch.messages}
    # Every mailbox message, not just the typed gate events — an escalation
    # or a non-planner question must stay visible to the Orchestrator.
    message_dicts = [
        {
            "id": m.id, "type": m.type, "subject": m.subject,
            "body": m.body, "payload": m.payload, "sender": m.sender,
        }
        for m in batch.messages
    ]

    # The Linear tracker is built lazily, only when a gate action actually
    # needs it — a `wait` that observes no gate action (the common case)
    # must work without `LINEAR_API_KEY` set.
    with _registry.transaction() as data:
        _journal.append_events(_registry.runtime_dir(), batch.delivery_id, message_dicts)
        actions_taken, unattributed = _gate.run(
            events, raw_by_id, data,
            tracker=lambda: _linear.LinearTracker(),
            teardown=lambda t, r: teardown(t, r, data=data),
            gate_role=GATE_ROLE.value,
        )
    _journal.write_receipt(_registry.runtime_dir(), batch.delivery_id)
    return {
        "delivery_id": batch.delivery_id,
        "acked": ack or None,
        # An empty batch back in a couple of seconds, instead of blocking
        # for the requested timeout, is content-identical to a legitimate
        # empty timeout (measured live) — elapsed time is the only
        # observable difference, so it is surfaced here.
        "elapsed_ms": elapsed_ms,
        "events": message_dicts,
        "actions": actions_taken,
        "unattributed": unattributed,
    }


def verdict(ticket: str, decision: str, notes: str) -> dict:
    """The human's decision, as argv — never typed by an agent. Formats the
    reply, answers the recorded `question_id`, and lifts the `human-gate`
    label. On `approved`, also posts the one and only copy of the plan the
    ticket ever gets — never on submission (that would post an unapproved
    plan) and never on `revise` (that would post once per round). The
    planner is not retired here: that happens in `wait`, when its
    `worker_done` arrives with the approved gate_state recorded — one path
    for both APPROVED and APPROVED-with-caveats, and it never kills a
    planner still blocked inside its `ask`.

    Everything runs inside one registry transaction, with a `commit`
    checkpoint BEFORE the reply goes out: the reply unblocks the planner,
    which may run `spawn done` immediately, and a registry still saying
    `submitted` would refuse the very report the human just authorized.
    `question_id` is only dropped after the reply lands, so a failed reply
    can be retried instead of stranding the planner in its `ask`."""

    ticket = ticket.upper()
    if decision not in ("approved", "revise"):
        raise Refusal(f"unknown decision {decision!r}; use 'approved' or 'revise'")

    key = _registry.key(ticket, GATE_ROLE.value)
    token = "APPROVED" if decision == "approved" else "REVISE"
    note_lines = [line.strip() for line in notes.splitlines() if line.strip()]
    body = _gate.format_approval_reply(token, note_lines)

    with _registry.transaction() as data:
        rec = data.get(key)
        if rec is None or not rec.get("question_id"):
            raise Refusal(
                f"no pending plan submission recorded for {ticket}; "
                f"`spawn wait` must observe the submission before a verdict can be given"
            )
        if decision == "revise" and not notes.strip():
            raise Refusal("REVISE with no --notes/--notes-file says nothing; name the correction")

        rec["gate_state"] = _gate.VERDICT_APPROVED if decision == "approved" else _gate.VERDICT_REVISE
        rec["last_event_at"] = _now()
        _registry.commit(data)

        _orca.reply(rec["question_id"], body)
        rec.pop("question_id", None)

    # The label is cosmetic next to the verdict; losing Linear must not
    # make a delivered verdict look like a failure.
    tracker = None
    label = "cleared"
    try:
        tracker = _linear.LinearTracker()
        tracker.set_gate(ticket, False)
    except Exception as exc:  # noqa: BLE001 - any tracker failure, reported not raised
        label = f"NOT cleared ({exc}); clear the human-gate label by hand"

    result = {"ticket": ticket, "decision": token, "label": label}
    if decision == "approved":
        # `plan_body` is the raw submission `wait` recorded off the genuine
        # plan-question; `body` is the same `## Approval` text just sent.
        plan_body = rec.get("plan_body")
        if plan_body is None:
            result["plan_copy"] = "NOT posted (no plan_body recorded)"
        else:
            comment = f"{plan_body.rstrip()}\n\n## Approval\n\n{body}\n"
            try:
                (tracker or _linear.LinearTracker()).post_comment(ticket, comment)
                result["plan_copy"] = "posted"
            except Exception as exc:  # noqa: BLE001 - any tracker failure, reported not raised
                result["plan_copy"] = f"NOT posted ({exc})"
    return result


def brief(ticket: str, body_path: str) -> dict:
    """Post a wave's coordination note as a ticket comment — the
    Orchestrator's sanctioned way to hand a role a cut of work.
    `build_brief` already composes every ticket comment into the Execution
    Brief the next dispatch opens with, so posting the comment IS the whole
    job; nothing else happens if the post fails, so `LinearError`
    propagates loudly."""

    ticket = ticket.upper()
    path = Path(body_path)
    if not path.is_file():
        raise Refusal(f"{body_path} does not exist; nothing to post")
    body = path.read_text()
    if not body.strip():
        raise Refusal(f"{body_path} is empty; an empty cut of work coordinates nothing")

    comment = _linear.LinearTracker().post_comment(ticket, body)
    return {"ticket": ticket, "posted": True, "comment": comment.id}


# --- the role's own two verbs ----------------------------------------------
#
# Everything above is run by the Orchestrator. The two below are run BY A
# ROLE, inside its own dispatched terminal. Two things measured on Orca
# 1.4.168 are why this cannot be left to a role typing `orca orchestration`
# by hand (a third — payload × structured-flag exclusivity — lives on
# `orca.send_worker_done`, where the argv is composed):
#
#   1. An injected dispatch mints a capability, and a lifecycle message
#      without it is refused with `dispatch_capability_invalid`.
#   2. A dispatch grants exactly one `worker_done`. A second is refused, so
#      the body has to be checked BEFORE the single shot is spent.


def submit(ticket: str, body_path: str, *, max_wait_ms: int) -> dict:
    """Send a Local Technical Plan for a verdict and block until it
    arrives. The verdict is parsed here and returned as a field — the reply
    is written by a script on the coordinator's side (`spawn verdict`) and
    read by a script here, so `APPROVED`/`REVISE` never depends on a model
    reading prose.

    `ask` caps its own wait at 30 minutes and a human verdict routinely
    takes longer, so a timeout is resumed by message id rather than asked
    again — a second `ask` would be a second question, and the gate would
    have two submissions to reconcile."""

    ticket = ticket.upper()
    key, rec = own_record(ticket)
    if rec["role"] != GATE_ROLE.value:
        raise Refusal(f"{key} is not the planner; only the planner submits a plan for a verdict")

    body = Path(body_path).read_text()
    submission = _gate.parse_plan_submission(body)  # loud, before anything is sent
    if submission.ticket.upper() != ticket:
        raise Refusal(
            f"the '## Plan' line says {submission.ticket!r} but you are dispatched on {ticket!r}"
        )

    waited, message_id, answer = 0, None, None
    while answer is None:
        if waited >= max_wait_ms:
            raise Refusal(
                f"no verdict after {max_wait_ms}ms; the question is still pending as "
                f"{message_id} — resume it with a longer --max-wait-ms, never ask again"
            )
        slice_ms = min(_orca.ASK_MAX_MS, max_wait_ms - waited)
        result = _orca.ask(
            rec["terminal"],
            question=None if message_id else body,
            resume=message_id,
            capability=rec.get("capability"),
            timeout_ms=slice_ms,
        )
        waited += int(result.get("timeoutMs") or slice_ms)
        message_id = str(result.get("messageId") or message_id or "")
        answer = result.get("answer")
        if answer is None and not result.get("timedOut"):
            raise Refusal(
                f"ask ended without an answer and without a timeout "
                f"(cancelled={result.get('cancelled')}, "
                f"connectionLost={result.get('connectionLost')}); question {message_id} is pending"
            )

    try:
        parsed = _gate.parse_approval_reply(str(answer))
    except _gate.MalformedReport as exc:
        raise Refusal(
            f"the verdict on question {message_id} does not follow the approval format "
            f"({exc}). It was answered by hand instead of by `spawn verdict`. Do NOT "
            f"submit again — that files a second question and costs another verdict; "
            f"escalate and ask for the verdict to be re-sent. Raw answer:\n{answer}"
        )
    return {
        "ticket": ticket,
        "question_id": message_id,
        "verdict": "approved" if parsed.approved else "revise",
        "notes": list(parsed.notes),
    }


def _worktree_measurement(workspace: str) -> tuple[str, bool] | None:
    """`(HEAD, tree is dirty)`, or `None` if git itself could not answer.
    `None` is not "clean": the caller must never read a failed measurement
    as proof that nothing changed."""

    head_proc = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"], capture_output=True, text=True,
    )
    status_proc = subprocess.run(
        ["git", "-C", workspace, "status", "--porcelain"], capture_output=True, text=True,
    )
    if head_proc.returncode != 0 or status_proc.returncode != 0:
        return None
    return head_proc.stdout.strip(), bool(status_proc.stdout.strip())


def done(ticket: str, body_path: str, *, outcome: str, files_modified: str) -> dict:
    """The single `worker_done` a dispatch allows, built and checked here.

    For the planner it refuses to fire before the gate recorded an approval
    — a `worker_done` without one is flagged and cannot be resent. Two more
    refusals, both local and BEFORE anything is sent: an empty body, for
    any role and either outcome; and, for a write-access non-planner role
    reporting `succeeded`, a worktree that shows no change at all against
    `head_at_dispatch` — an empty success is exactly what this gate exists
    to catch.

    A dirty tree with no new commit is real work, just not persisted:
    accepted and flagged `uncommitted_work` on the record instead, so the
    Orchestrator can check without opening the worktree."""

    ticket = ticket.upper()
    if outcome not in ("succeeded", "failed"):
        raise Refusal(f"unknown outcome {outcome!r}; use 'succeeded' or 'failed'")
    key, rec = own_record(ticket)
    body = Path(body_path).read_text()
    if not body.strip():
        raise Refusal(
            f"{ticket}/{rec['role']}: an empty report says nothing; `done` needs a body "
            f"even for --outcome failed"
        )
    extra: dict = {}

    if rec["role"] == GATE_ROLE.value and outcome == "succeeded":
        state = rec.get("gate_state", IDLE)
        if state != _gate.VERDICT_APPROVED:
            raise Refusal(
                f"the plan for {ticket} is not approved (gate state {state!r}); a planner's "
                f"worker_done is only valid after APPROVED, and you get exactly one"
            )
        _gate.parse_planner_done(body)  # loud, before the shot is spent
        rounds = int(rec.get("approval_rounds", 1)) or 1
        # Only `## Approval` is rewritten, and only to state what the gate
        # counted. Everything else in the body is the planner's.
        body = _gate.set_approval_rounds(body, rounds)
        extra = {"planApproved": True, "approvalRounds": rounds}

    if outcome == "succeeded" and rec.get("access") == "write" and rec["role"] != GATE_ROLE.value:
        measured = _worktree_measurement(rec["worktree"])
        if measured is None:
            print(
                f"note: could not measure {rec['worktree']} (git error); "
                f"skipping the empty-success check", file=sys.stderr,
            )
        else:
            head_now, dirty = measured
            baseline = rec.get("head_at_dispatch")
            if head_now == baseline and not dirty:
                raise Refusal(
                    f"{ticket}/{rec['role']}: outcome=succeeded but the worktree shows no "
                    f"change — HEAD is still {head_now} (same as at dispatch) and `git "
                    f"status --porcelain` is empty. If there genuinely was nothing to do, "
                    f"report `--outcome failed --file <report explaining why>` instead, "
                    f"so the Orchestrator can decide."
                )
            if head_now == baseline and dirty:
                print(
                    f"note: {ticket}/{rec['role']} outcome=succeeded but HEAD is still "
                    f"{head_now} — the tree is dirty but nothing was committed; "
                    f"flagging uncommitted_work on the record", file=sys.stderr,
                )
                with _registry.transaction() as data:
                    if key in data:
                        data[key]["uncommitted_work"] = True

    # The three fields Orca reconciles on, plus whatever the gate needs —
    # all inside the one `--payload` (`orca.send_worker_done` documents the
    # exclusivity rule that forbids the structured flags).
    payload = {
        "taskId": rec["task"],
        "dispatchId": rec["dispatch"],
        "outcome": outcome,
        **extra,
    }
    if files_modified.strip():
        payload["filesModified"] = [f.strip() for f in files_modified.split(",") if f.strip()]

    _orca.send_worker_done(
        rec["terminal"],
        subject=f"{ticket} {rec['role']}: {outcome}",
        body=body,
        payload=payload,
        capability=rec.get("capability"),
    )
    return {"ticket": ticket, "role": rec["role"], "outcome": outcome, "reported": key}


# --- CLI -------------------------------------------------------------------

VERBS = {
    "plan": (RoleName.PLANNER, True),
    "implement": (RoleName.IMPLEMENTER, False),
    "review-spec": (RoleName.SPEC_REVIEWER, False),
    "review-standards": (RoleName.STANDARDS_REVIEWER, False),
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="spawn", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in VERBS:
        p = sub.add_parser(verb)
        p.add_argument("ticket")
        p.add_argument(
            "--tier",
            help="HUMAN OVERRIDE ONLY. Run this role at another Capability Tier "
            "than its role file declares. The Orchestrator never passes this.",
        )
    p = sub.add_parser("status")
    p.add_argument("ticket", nargs="?")
    p = sub.add_parser("retire")
    p.add_argument("ticket")
    p.add_argument("role")
    p = sub.add_parser("sweep")
    p.add_argument("ticket", nargs="?")
    p = sub.add_parser("wait")
    p.add_argument(
        "--ack",
        help="Delivery id to acknowledge before waiting again. Normally automatic "
        "— the previous Delivery's id is read from the persisted receipt. Pass "
        "this only to re-ack a specific id by hand.",
    )
    p.add_argument("--timeout-ms", type=int, default=900000)
    p = sub.add_parser("verdict")
    p.add_argument("ticket")
    p.add_argument("decision", choices=["approved", "revise"])
    p.add_argument("--notes", default="")
    p.add_argument("--notes-file")
    p = sub.add_parser("brief")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the coordination note to post.")
    # The role's own two verbs. Run inside a dispatched terminal, by the
    # role itself — the Orchestrator never calls these.
    p = sub.add_parser("submit")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the plan submission body.")
    p.add_argument("--max-wait-ms", type=int, default=6 * _orca.ASK_MAX_MS)
    p = sub.add_parser("done")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the report body.")
    p.add_argument("--outcome", choices=["succeeded", "failed"], required=True)
    p.add_argument("--files-modified", default="")

    args = parser.parse_args()
    # The one edge where a refusal or a provider failure becomes an exit
    # code. Inside the library they stay ordinary exceptions.
    try:
        if args.verb in VERBS:
            role, fresh = VERBS[args.verb]
            print(json.dumps(
                spawn(role, args.ticket, fresh_worktree=fresh, tier=args.tier), indent=2
            ))
        elif args.verb == "status":
            print(json.dumps(status(args.ticket), indent=2))
        elif args.verb == "retire":
            print(json.dumps(retire(args.ticket, args.role), indent=2))
        elif args.verb == "sweep":
            print(json.dumps(sweep(args.ticket), indent=2))
        elif args.verb == "wait":
            print(json.dumps(wait(ack=args.ack, timeout_ms=args.timeout_ms), indent=2))
        elif args.verb == "verdict":
            notes = args.notes
            if args.notes_file:
                notes = (notes + "\n" + Path(args.notes_file).read_text()).strip()
            print(json.dumps(verdict(args.ticket, args.decision, notes), indent=2))
        elif args.verb == "brief":
            print(json.dumps(brief(args.ticket, args.file), indent=2))
        elif args.verb == "submit":
            print(json.dumps(
                submit(args.ticket, args.file, max_wait_ms=args.max_wait_ms), indent=2
            ))
        elif args.verb == "done":
            print(json.dumps(done(
                args.ticket, args.file,
                outcome=args.outcome, files_modified=args.files_modified,
            ), indent=2))
    except (Refusal, _orca.OrcaCliError, _linear.LinearError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
