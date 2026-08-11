"""The gate's decision for one event, isolated from the tracker/Linear calls
`wait` makes.

TLDR: `apply_gate_event` is `spawn._apply_gate_event`, moved here and made
public — same body, same behavior. It knows nothing about Orca or Linear:
no network call happens here, so this is what a unit test drives, over the
pure `adapters/plan_gate.py` state machine, which this module builds around
and never touches.
"""
from __future__ import annotations

from datetime import datetime, timezone

from adapters import attention as _attention
from adapters import plan_gate as _gate
from adapters import reports as _reports

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

    What the parsers return is kept, not discarded: the plan's pointer (the
    comment the next role has to read) and the round count both come from
    here, and the Ticket Key on the `## Plan` line is checked against the
    record so a report filed under the wrong ticket is caught rather than
    counted.
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
