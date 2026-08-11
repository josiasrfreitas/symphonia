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


def question():
    return SimpleNamespace(kind="plan-question")


def reply(token: str, notes: str = ""):
    body = token + (f"\n\n- {notes}" if notes else "")
    return SimpleNamespace(kind="approval-reply", body=body)


def done(outcome: str):
    return SimpleNamespace(kind="worker-done", outcome=outcome)


class TestHappyPath(unittest.TestCase):
    def test_submit_approve_done_retires(self):
        state = IDLE
        result = transition(state, question())
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))

        result = transition(result.state, reply("APPROVED"))
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, (LABEL_OFF,)))

        result = transition(result.state, done("succeeded"))
        self.assertEqual((result.state, result.actions), (RETIRED, (RETIRE_PLANNER,)))


class TestReviseRoundTrip(unittest.TestCase):
    def test_revise_then_resubmit_relights_the_label(self):
        state = SUBMITTED
        result = transition(state, reply("REVISE", "fix the write scope"))
        self.assertEqual((result.state, result.actions), (VERDICT_REVISE, (LABEL_OFF,)))

        result = transition(result.state, question())
        self.assertEqual((result.state, result.actions), (SUBMITTED, (LABEL_ON,)))


class TestReplayIsIdempotent(unittest.TestCase):
    def test_repeated_submission_does_not_relight_the_label(self):
        result = transition(SUBMITTED, question())
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))

    def test_repeated_approval_reply_is_a_no_op(self):
        result = transition(VERDICT_APPROVED, reply("APPROVED"))
        self.assertEqual((result.state, result.actions), (VERDICT_APPROVED, ()))

    def test_repeated_worker_done_after_retirement_does_not_retire_twice(self):
        result = transition(RETIRED, done("succeeded"))
        self.assertEqual((result.state, result.actions), (RETIRED, ()))


class TestDivergenceIsFlagged(unittest.TestCase):
    def test_invalid_approval_token_is_flagged_not_guessed(self):
        result = transition(SUBMITTED, reply("MAYBE"))
        self.assertEqual((result.state, result.actions), (SUBMITTED, (FLAG_MALFORMED,)))

    def test_succeeded_done_without_recorded_approval_is_flagged(self):
        result = transition(SUBMITTED, done("succeeded"))
        self.assertEqual((result.state, result.actions), (SUBMITTED, (FLAG_MALFORMED,)))

    def test_failed_done_is_left_for_the_human(self):
        result = transition(SUBMITTED, done("failed"))
        self.assertEqual((result.state, result.actions), (SUBMITTED, ()))


class TestRetiredPlannerCannotReopen(unittest.TestCase):
    def test_plan_question_after_retirement_is_ignored(self):
        result = transition(RETIRED, question())
        self.assertEqual((result.state, result.actions), (RETIRED, ()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
