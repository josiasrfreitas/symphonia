"""Tests for the plan gate state machine (GRE-178).

TLDR: the transition table end to end (submit -> label on -> verdict
recorded by `spawn verdict` -> worker_done -> retire), the REVISE round
trip, and replay idempotency — the same event applied twice from the same
state must not double an action.

The verdict is NOT an event here: measured on Orca 1.4.168, the answer to an
`ask` goes back to the worker that asked and never reaches the coordinator's
mailbox, so `spawn verdict` writes the state directly and the planner's side
of that exchange is parsed by `spawn submit`. Run either way:

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


DONE_BODY = "## Plan\npointer\n\n## Approval\n1 round.\n\n## Deviations\nNone.\n"


def done(outcome: str):
    return SimpleNamespace(
        kind="worker-done", message_id="d-1", outcome=outcome, summary=DONE_BODY,
    )


class TestHappyPath(unittest.TestCase):
    def test_submit_approve_done_retires(self):
        state = IDLE
        result = transition(state, question("q-1"), last_question_id=None)
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))
        self.assertEqual(result.question_id, "q-1")

        # `spawn verdict` records the verdict; no event carries it.
        result = transition(VERDICT_APPROVED, done("succeeded"), report_ok=True)
        self.assertEqual((result.state, result.actions), (RETIRED, (RETIRE_PLANNER,)))


class TestReviseRoundTrip(unittest.TestCase):
    def test_revise_then_resubmit_relights_the_label(self):
        # A genuine resubmission carries a NEW question id — the planner
        # asked again after correcting the plan.
        result = transition(VERDICT_REVISE, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))
        self.assertEqual(result.question_id, "q-2")


class TestReplayIsIdempotent(unittest.TestCase):
    def test_repeated_submission_does_not_relight_the_label(self):
        result = transition(SUBMITTED, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))

    def test_an_approval_reply_event_is_inert(self):
        """The coordinator never sees one; if a runtime ever delivered one it
        must change nothing, since `spawn verdict` already recorded the
        state."""
        inert = SimpleNamespace(kind="approval-reply", body="APPROVED")
        result = transition(VERDICT_APPROVED, inert)
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, ()))

    def test_repeated_worker_done_after_retirement_does_not_retire_twice(self):
        result = transition(RETIRED, done("succeeded"), report_ok=True)
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
    def test_succeeded_done_without_recorded_approval_is_flagged(self):
        result = transition(SUBMITTED, done("succeeded"), report_ok=True)
        self.assertEqual((result.state, result.actions), (SUBMITTED, (FLAG_MALFORMED,)))

    def test_failed_done_is_left_for_the_human(self):
        result = transition(SUBMITTED, done("failed"))
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))

    def test_approved_done_whose_body_did_not_parse_is_flagged_not_retired(self):
        """The caller parses the body; a report that does not follow its
        contract must not retire the planner, and cannot be resent — a
        dispatch grants exactly one worker_done."""
        result = transition(VERDICT_APPROVED, done("succeeded"), report_ok=False)
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, (FLAG_MALFORMED,)))


class TestRetiredPlannerCannotReopen(unittest.TestCase):
    def test_plan_question_after_retirement_is_ignored(self):
        result = transition(RETIRED, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (RETIRED, ()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
