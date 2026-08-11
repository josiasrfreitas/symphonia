"""The plan gate as a pure state machine.

TLDR: ``transition(state, event)`` is the whole mechanics of the gate
between the planner and the human — submission turns the label on, a
verdict turns it off, and an approved ``worker_done`` retires the planner.
It knows nothing about Orca or Linear: ``event`` is duck-typed on the
``kind`` field the gate events in ``adapters/orca/events.py`` already carry
(``"plan-question"``, ``"approval-reply"``, ``"worker-done"``), so this file
has no import of that module and stays reusable by any runtime adapter.

Replay-safe by construction: a Delivery that is not acked replays the same
batch, so every transition is keyed off the *current state*, not off having
"seen this event before". Applying the same event twice from the same state
produces the same next state and an empty (or already-applied) action list —
label repeats are a no-op, and ``retire_planner`` only fires once, from
``VERDICT_APPROVED``, never again once the state is ``RETIRED``.
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


def _approval_token(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def transition(state: str, event: Any) -> Transition:
    """One event, one state in, one state out plus the actions to run."""

    kind = getattr(event, "kind", None)

    if kind == "plan-question":
        if state in (SUBMITTED, RETIRED):
            # SUBMITTED: a replayed, not-yet-acked Delivery — label already
            # on. RETIRED: a dead planner cannot reopen the gate.
            return Transition(state, ())
        return Transition(SUBMITTED, (LABEL_ON,))

    if kind == "approval-reply":
        token = _approval_token(event.body)
        if token not in ("APPROVED", "REVISE"):
            return Transition(state, (FLAG_MALFORMED,))
        if state != SUBMITTED:
            # Out of order or replayed after the state already moved on.
            return Transition(state, ())
        next_state = VERDICT_APPROVED if token == "APPROVED" else VERDICT_REVISE
        return Transition(next_state, (LABEL_OFF,))

    if kind == "worker-done":
        if state == RETIRED:
            return Transition(state, ())  # guarded: retire fires once
        if event.outcome != "succeeded":
            return Transition(state, ())  # a failed planner is for the human
        if state == VERDICT_APPROVED:
            return Transition(RETIRED, (RETIRE_PLANNER,))
        # succeeded without a recorded approval: payload and gate state
        # disagree — never guess which one is right.
        return Transition(state, (FLAG_MALFORMED,))

    return Transition(state, ())
