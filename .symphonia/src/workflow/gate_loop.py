"""The gate loop: turns a batch of typed events into gate actions and
executes them, over the pure `adapters/plan_gate.py` state machine, which
this module builds around and never touches.

TLDR: `apply_gate_event` is `spawn._apply_gate_event`, moved here and made
public — same body, same behavior. `run` is the attribution/execution loop
that used to live inside `wait`: it maps each event to the registry record
it belongs to, applies `apply_gate_event`, and executes what comes back.
Neither function makes an Orca or Linear call directly — the registry, the
tracker and `teardown` all arrive by injection, so a test drives this with a
plain dict and a fake tracker instead of loading the CLI or monkeypatching
`spawn`.

A `worker_done` from any role's own dispatch ends that role: the planner's
goes through `apply_gate_event`/`RETIRE_PLANNER` as it always has, and a
non-planner's (implementer, either reviewer) is handled directly here,
since it carries no gate state of its own to transition.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from adapters import attention as _attention
from adapters import plan_gate as _gate
from adapters import reports as _reports
from adapters.tracker_adapter import TrackerAdapter

IDLE = _gate.IDLE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flag_malformed(tracker, ticket: str, reason: str) -> None:
    tracker.set_attention(
        ticket,
        _attention.Attention(
            needs=True, code=_attention.AttentionCode.MALFORMED_REPORT, reason=reason,
        ),
    )


def apply_gate_event(rec: dict, event, raw_by_id: dict) -> list[tuple[str, str | None]]:
    """The gate's decision for one event, isolated from the tracker/Linear
    calls `wait` makes: mutates `rec`'s `gate_state`/`question_id` in place
    and returns the `(action, reason)` pairs to execute — `reason` is only
    set for `flag_malformed`. No network call happens here, so this is what
    a unit test drives.

    A `plan-question` is only a gate event when its body is a real plan
    submission (`reports.is_plan_submission`) — a clarifying question from
    the planner is not a submission, does not light the label, and is left
    for the caller to treat as an ordinary message instead. A body that
    starts `## Plan` but does not otherwise parse is flagged rather than
    silently accepted or silently ignored.

    What the parsers return is kept, not discarded: the plan's pointer, the
    round count, and the raw submission body (`plan_body`, republished by
    `spawn.verdict` on approval — never here) all come from here, and the
    Ticket Key on the `## Plan` line is checked against the record so a
    report filed under the wrong ticket is caught rather than counted.
    """

    state = rec.get("gate_state", IDLE)
    raw = raw_by_id.get(event.message_id)

    # Orca does not drop a refused lifecycle message: it rewrites the subject
    # and body and marks the payload. Measured on 1.4.168 — a `worker_done`
    # without `dispatchId` comes back as a `worker_done` carrying
    # `_orcaLifecycleRejection`. Without this branch that arrival is a
    # completion that never completed anything.
    rejection = (raw.payload if raw else {}).get("_orcaLifecycleRejection")
    if rejection:
        return [(
            _gate.FLAG_MALFORMED,
            f"Orca refused this {event.kind}: "
            f"{rejection.get('code')}: {rejection.get('reason')}",
        )]

    if event.kind == "plan-question":
        if not _reports.is_plan_submission(event.question):
            return []
        try:
            submission = _reports.parse_plan_submission(event.question)
        except _reports.MalformedReport as exc:
            return [(_gate.FLAG_MALFORMED, str(exc))]
        if submission.ticket.upper() != str(rec.get("ticket", "")).upper():
            return [(
                _gate.FLAG_MALFORMED,
                f"plan submission is filed under {submission.ticket!r} but this "
                f"dispatch is {rec.get('ticket')!r}",
            )]
        result = _gate.transition(state, event, last_question_id=rec.get("last_question_id"))
        if result.question_id:
            rec["question_id"] = result.question_id
            rec["last_question_id"] = result.question_id
            rec["plan_pointer"] = submission.pointer
            rec["plan_decisions"] = list(submission.decisions)
            rec["plan_body"] = event.question
            if result.state == _gate.SUBMITTED and state != _gate.SUBMITTED:
                # A round is a plan put in front of the human, counted here
                # so `spawn done` never asks the planner how many there were.
                # Re-asking the same pending question is not a new round.
                rec["approval_rounds"] = int(rec.get("approval_rounds", 0)) + 1
    elif event.kind == "worker-done":
        report_ok = True
        try:
            report = _reports.parse_planner_done(event.summary)
        except _reports.MalformedReport as exc:
            report_ok, report = False, None
            malformed_reason = str(exc)
        result = _gate.transition(state, event, report_ok=report_ok)
        if report is not None:
            rec["plan_pointer_final"] = report.plan_pointer
            rec["deviations"] = list(report.deviations)
        if not report_ok and _gate.FLAG_MALFORMED in result.actions:
            rec["gate_state"] = result.state
            rec["last_event_at"] = _now()
            return [(_gate.FLAG_MALFORMED, malformed_reason)]
    else:
        result = _gate.transition(state, event)

    rec["gate_state"] = result.state
    rec["last_event_at"] = _now()
    divergence = (
        f"gate event {event.kind!r} did not match the recorded gate_state {state!r}"
    )
    return [
        (action, divergence if action == _gate.FLAG_MALFORMED else None)
        for action in result.actions
    ]


def run(
    events,
    raw_by_id: dict,
    data: dict,
    *,
    tracker: Callable[[], TrackerAdapter],
    teardown: Callable[[str, str], dict],
    gate_role: str,
) -> tuple[list[dict], list[dict]]:
    """Attribute each event to the registry record it belongs to, apply
    `apply_gate_event`, and execute the actions it returns — label the
    ticket, end the role, flag a divergence. Mutates `data` in place and
    returns `(actions_taken, unattributed)`; it never reads or writes the
    registry itself — the caller does both, under the same lock, so this
    only ever touches the copy it was handed.

    `tracker` is a zero-argument factory, not a tracker: a `wait` that
    observes no gate action (the common case for every role but the
    planner) must keep working without `LINEAR_API_KEY` set, so the tracker
    is built at most once, and only if an action actually needs it.
    `teardown` takes `(ticket, role_value)` — this loop supplies the role,
    since it is the one that knows which record an event belongs to.
    `gate_role` is the role value the plan gate itself is scoped to (the
    planner): an event attributed to any other role's dispatch never goes
    through `apply_gate_event` — a `worker_done` from one of those still
    ends the role, just directly, below.
    """

    by_dispatch = {rec["dispatch"]: key for key, rec in data.items()}
    # Attribution falls back to the task id because a REFUSED lifecycle
    # message is exactly the one whose `dispatchId` may be missing — that is
    # what it was refused for. Measured: the refusal keeps the rest of the
    # payload, `taskId` included, so it can still be traced to its role.
    by_task = {rec["task"]: key for key, rec in data.items()}
    actions_taken = []

    _tracker: list = []

    def resolved_tracker() -> TrackerAdapter:
        if not _tracker:
            _tracker.append(tracker())
        return _tracker[0]

    unattributed = []
    for event in events:
        dispatch_id = getattr(event, "dispatch_id", None)
        key = by_dispatch.get(dispatch_id) or by_task.get(getattr(event, "task_id", None))
        if key is None:
            # Neither id maps to a spawn this package started: it completes
            # nothing and would otherwise vanish. Reported, never swallowed.
            unattributed.append({
                "message": event.message_id, "kind": event.kind,
                "dispatch": dispatch_id, "task": getattr(event, "task_id", None),
            })
            continue
        rec = data[key]
        ticket = rec["ticket"]
        if not key.endswith(f"/{gate_role}"):
            # Not the gate's own role — the plan gate has nothing to say
            # about this dispatch. A `worker_done` still ends it, on both
            # outcomes; an already-retired record is left alone.
            if event.kind == "worker-done" and not rec.get("retired"):
                teardown(ticket, rec["role"])
                rec["retired"] = True
                actions_taken.append({"ticket": ticket, "action": _gate.RETIRE_ROLE})
            continue
        for action, reason in apply_gate_event(rec, event, raw_by_id):
            if action == _gate.LABEL_ON:
                resolved_tracker().set_gate(ticket, True)
            elif action == _gate.LABEL_OFF:
                resolved_tracker().set_gate(ticket, False)
            elif action == _gate.RETIRE_PLANNER:
                teardown(ticket, gate_role)
                # `teardown` re-reads and rewrites the registry on its own;
                # without this the caller's `state_write` would put this
                # stale copy back and resurrect the retired role.
                rec["retired"] = True
            elif action == _gate.FLAG_MALFORMED:
                _flag_malformed(resolved_tracker(), ticket, reason or "malformed gate report")
            actions_taken.append({"ticket": ticket, "action": action})
    return actions_taken, unattributed
