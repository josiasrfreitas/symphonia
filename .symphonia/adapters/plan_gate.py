"""The plan gate as a pure state machine.

TLDR: ``transition(state, event)`` is the whole mechanics of the gate
between the planner and the human — submission turns the label on, a
verdict turns it off, and an approved ``worker_done`` retires the planner.
It knows nothing about Orca or Linear: ``event`` is duck-typed on the
``kind`` field the gate events in ``adapters/orca/events.py`` already carry
(``"plan-question"``, ``"approval-reply"``, ``"worker-done"``), so this file
has no import of that module and stays reusable by any runtime adapter. It
does import ``reports.py`` — that module is equally neutral (no runtime, no
Orca, no Linear), so borrowing its ``APPROVED``/``REVISE`` parser and its
``worker_done`` parser is legitimate: one house for that contract instead of
one copy per file.

Replay-safe by construction: a Delivery that is not acked replays the same
batch, so most transitions are keyed off the *current state*, not off having
"seen this event before". A ``plan-question`` is the one exception — the
same state (``verdict-approved`` after ``APPROVED``, or ``verdict-revise``
after ``REVISE``) is reached both by a replayed submission and by a genuine
resubmission, and those two only differ by the *identity* of the event.
Callers pass the id of the last plan-question that produced a submission as
``last_question_id``; a replay carries the same id, a resubmission a new
one. Applying the same event twice from the same state (and, for
plan-question, the same id) produces the same next state and an empty (or
already-applied) action list — label repeats are a no-op, and
``retire_planner`` only fires once, from ``VERDICT_APPROVED``, never again
once the state is ``RETIRED``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .reports import MalformedReport, parse_approval_reply, parse_planner_done

# --- states -----------------------------------------------------------------

IDLE = "idle"
SUBMITTED = "submitted"
VERDICT_APPROVED = "verdict-approved"
VERDICT_REVISE = "verdict-revise"
RETIRED = "retired"

# --- actions ------------------------------------------------------------------
# Values, not calls: the caller executes them against the tracker/runtime.

LABEL_ON = "label_on"
LABEL_OFF = "label_off"
RETIRE_PLANNER = "retire_planner"
FLAG_MALFORMED = "flag_malformed"


@dataclass(frozen=True)
class Transition:
    state: str
    actions: tuple[str, ...]
    # Set only on a fresh (non-replay) plan-question submission — the id the
    # caller should remember and pass back as `last_question_id` next time.
    question_id: str | None = None


def transition(
    state: str,
    event: Any,
    *,
    last_question_id: str | None = None,
    payload: dict | None = None,
) -> Transition:
    """One event, one state in, one state out plus the actions to run.

    ``last_question_id`` is the id of the plan-question that produced the
    current ``state`` (when that state came from a submission) — needed to
    tell a replayed submission from a genuine resubmission after REVISE.
    ``payload`` is the raw payload of a ``worker-done`` event — the gate
    itself does not receive typed events carrying it, so the caller supplies
    it for the divergence check below.
    """

    kind = getattr(event, "kind", None)

    if kind == "plan-question":
        if event.message_id == last_question_id:
            # The same submission, replayed by an unacked Delivery.
            return Transition(state, ())
        if state in (SUBMITTED, RETIRED):
            # SUBMITTED: a second, distinct question before any verdict —
            # ignored, the planner is still waiting on the first one.
            # RETIRED: a dead planner cannot reopen the gate.
            return Transition(state, ())
        return Transition(SUBMITTED, (LABEL_ON,), question_id=event.message_id)

    if kind == "approval-reply":
        try:
            verdict = parse_approval_reply(event.body)
        except MalformedReport:
            return Transition(state, (FLAG_MALFORMED,))
        if state != SUBMITTED:
            # Out of order or replayed after the state already moved on.
            return Transition(state, ())
        next_state = VERDICT_APPROVED if verdict.approved else VERDICT_REVISE
        return Transition(next_state, (LABEL_OFF,))

    if kind == "worker-done":
        if state == RETIRED:
            return Transition(state, ())  # guarded: retire fires once
        if event.outcome != "succeeded":
            return Transition(state, ())  # a failed planner is for the human
        if state == VERDICT_APPROVED:
            try:
                parse_planner_done(event.summary, payload or {})
            except MalformedReport:
                # Payload and body disagree with the recorded approval —
                # never guess which one is right.
                return Transition(state, (FLAG_MALFORMED,))
            return Transition(RETIRED, (RETIRE_PLANNER,))
        # succeeded without a recorded approval: payload and gate state
        # disagree — never guess which one is right.
        return Transition(state, (FLAG_MALFORMED,))

    return Transition(state, ())
