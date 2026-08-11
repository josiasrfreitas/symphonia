"""The plan gate as a pure state machine.

TLDR: ``transition(state, event)`` is the whole mechanics of the gate
between the planner and the human — submission turns the label on, a
verdict turns it off, and an approved ``worker_done`` retires the planner.
It knows nothing about Orca or Linear: ``event`` is duck-typed on the
``kind`` field the gate events in ``adapters/orca/events.py`` already carry,
so this file has no import at all and stays reusable by any runtime adapter.

It handles the two events a COORDINATOR can actually observe:
``"plan-question"`` and ``"worker-done"``. The verdict reply is not among
them — measured against Orca 1.4.168, the answer to an ``ask`` is printed
back to the worker that asked and never reaches the coordinator's mailbox
(``wait`` does not even subscribe to ``reply``). The state moves to
``verdict-approved``/``verdict-revise`` when ``spawn verdict`` writes the
reply, and the planner's side of that same exchange is parsed by
``spawn submit``. Parsing bodies is the caller's job on both events.

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
    report_ok: bool = True,
) -> Transition:
    """One event, one state in, one state out plus the actions to run.

    ``last_question_id`` is the id of the plan-question that produced the
    current ``state`` (when that state came from a submission) — needed to
    tell a replayed submission from a genuine resubmission after REVISE.
    ``report_ok`` is the caller's verdict on whether a ``worker-done`` body
    parsed: parsing lives with the caller (as it already did for
    plan-question), so this module imports nothing and stays a pure state
    machine.
    """

    kind = getattr(event, "kind", None)

    if kind == "plan-question":
        if event.message_id == last_question_id:
            # The same submission, replayed by an unacked Delivery.
            return Transition(state, ())
        if state == RETIRED:
            return Transition(state, ())  # a dead planner cannot reopen the gate
        if state == SUBMITTED:
            # A second, distinct question before any verdict: the planner
            # abandoned the first `ask` (its `submit` died, or timed out past
            # `--max-wait-ms`) and is now blocked on this one. The label is
            # already on and no new round has been decided, so nothing to do
            # — but the pending question id must follow the planner, or the
            # verdict is replied into a thread nobody is listening to.
            return Transition(state, (), question_id=event.message_id)
        return Transition(SUBMITTED, (LABEL_ON,), question_id=event.message_id)

    if kind == "worker-done":
        if state == RETIRED:
            return Transition(state, ())  # guarded: retire fires once
        if event.outcome != "succeeded":
            return Transition(state, ())  # a failed planner is for the human
        if state == VERDICT_APPROVED:
            if not report_ok:
                # The report does not follow its contract — never guess what
                # was meant.
                return Transition(state, (FLAG_MALFORMED,))
            return Transition(RETIRED, (RETIRE_PLANNER,))
        # succeeded without a recorded approval: the report and the gate
        # state disagree — never guess which one is right.
        return Transition(state, (FLAG_MALFORMED,))

    return Transition(state, ())
