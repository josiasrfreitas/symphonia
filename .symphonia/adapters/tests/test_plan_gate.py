"""Tests for the plan gate state machine (GRE-178).

TLDR: the transition table end to end (submit -> label on -> approve ->
label off -> worker_done -> retire), the REVISE round trip, and replay
idempotency — the same event applied twice from the same state must not
double an action. Run either way:

    cd .symphonia && python3 -m unittest adapters.tests.test_plan_gate
    python3 .symphonia/adapters/tests/test_plan_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.plan_gate import (
    FLAG_MALFORMED,
    IDLE,
    LABEL_OFF,
    LABEL_ON,
    RETIRE_PLANNER,
    RETIRED,
    SUBMITTED,
    VERDICT_APPROVED,
    VERDICT_REVISE,
    transition,
)


def question(message_id: str = "q-1"):
    return SimpleNamespace(kind="plan-question", message_id=message_id)


def reply(token: str, notes: str = ""):
    body = token + (f"\n\n- {notes}" if notes else "")
    return SimpleNamespace(kind="approval-reply", body=body)


DONE_BODY = "## Plan\npointer\n\n## Approval\n1 round.\n\n## Deviations\nNone.\n"


def done(outcome: str, *, approved: bool = True, rounds: int = 1):
    payload = {"planApproved": approved, "approvalRounds": rounds}
    return SimpleNamespace(
        kind="worker-done", message_id="d-1", outcome=outcome, summary=DONE_BODY,
    ), payload


class TestHappyPath(unittest.TestCase):
    def test_submit_approve_done_retires(self):
        state = IDLE
        result = transition(state, question("q-1"), last_question_id=None)
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))
        self.assertEqual(result.question_id, "q-1")

        result = transition(result.state, reply("APPROVED"))
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, (LABEL_OFF,)))

        event, payload = done("succeeded")
        result = transition(result.state, event, payload=payload)
        self.assertEqual((result.state, result.actions), (RETIRED, (RETIRE_PLANNER,)))


class TestReviseRoundTrip(unittest.TestCase):
    def test_revise_then_resubmit_relights_the_label(self):
        state = SUBMITTED
        result = transition(state, reply("REVISE", "fix the write scope"))
        self.assertEqual((result.state, result.actions), (VERDICT_REVISE, (LABEL_OFF,)))

        # A genuine resubmission carries a NEW question id — the planner
        # asked again after correcting the plan.
        result = transition(result.state, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))
        self.assertEqual(result.question_id, "q-2")


class TestReplayIsIdempotent(unittest.TestCase):
    def test_repeated_submission_does_not_relight_the_label(self):
        result = transition(SUBMITTED, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))

    def test_repeated_approval_reply_is_a_no_op(self):
        result = transition(VERDICT_APPROVED, reply("APPROVED"))
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, ()))

    def test_repeated_worker_done_after_retirement_does_not_retire_twice(self):
        event, payload = done("succeeded")
        result = transition(RETIRED, event, payload=payload)
        self.assertEqual((result.state, result.actions), (RETIRED, ()))

    def test_replay_of_the_submission_after_approval_does_not_relight_the_label(self):
        """A2: an unacked Delivery can replay the original plan-question
        message after the verdict already moved the state to
        verdict-approved. The replayed event carries the SAME id as the
        submission that produced this state — it must be a no-op, not a
        fresh SUBMITTED transition."""
        result = transition(VERDICT_APPROVED, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, ()))

    def test_replay_of_the_submission_after_revise_does_not_relight_the_label(self):
        result = transition(VERDICT_REVISE, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (VERDICT_REVISE, ()))


class TestDivergenceIsFlagged(unittest.TestCase):
    def test_invalid_approval_token_is_flagged_not_guessed(self):
        result = transition(SUBMITTED, reply("MAYBE"))
        self.assertEqual((result.state, result.actions), (SUBMITTED, (FLAG_MALFORMED,)))

    def test_succeeded_done_without_recorded_approval_is_flagged(self):
        event, payload = done("succeeded")
        result = transition(SUBMITTED, event, payload=payload)
        self.assertEqual((result.state, result.actions), (SUBMITTED, (FLAG_MALFORMED,)))

    def test_failed_done_is_left_for_the_human(self):
        event, payload = done("failed")
        result = transition(SUBMITTED, event, payload=payload)
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))

    def test_approved_done_with_payload_disagreeing_is_flagged_not_retired(self):
        """A1: worker_done now goes through `parse_planner_done`, closing
        the payload side of the divergence check — a `planApproved: false`
        payload after an APPROVED verdict must not retire the planner."""
        event, payload = done("succeeded", approved=False)
        result = transition(VERDICT_APPROVED, event, payload=payload)
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, (FLAG_MALFORMED,)))


class TestRetiredPlannerCannotReopen(unittest.TestCase):
    def test_plan_question_after_retirement_is_ignored(self):
        result = transition(RETIRED, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (RETIRED, ()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
