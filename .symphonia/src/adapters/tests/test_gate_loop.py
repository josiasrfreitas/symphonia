"""Tests for the Orchestrator's side of the gate: `wait` and `verdict`.

TLDR: `_apply_gate_event` is already covered in `test_brief.py`; what this
file covers is the loop AROUND it — the registry write that follows the loop,
how an event is attributed to a role, and the order in which `verdict`
records and delivers a decision. Every case here is a defect found in review
of the first version, so each one is a regression, not a hypothetical.

No network: `SPAWN.orca` and `SPAWN._linear.LinearTracker` are replaced.
Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_gate_loop
    python3 .symphonia/src/adapters/tests/test_gate_loop.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_brief import _load_spawn

DONE_BODY = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class FakeTracker:
    """Records what the gate asked of Linear; raises on demand."""

    def __init__(self, fail: bool = False):
        self.gate_calls, self.attention = [], []
        self.fail = fail

    def set_gate(self, ticket, on):
        if self.fail:
            raise RuntimeError("linear unreachable")
        self.gate_calls.append((ticket, on))

    def set_attention(self, ticket, attention):
        self.attention.append((ticket, attention))


class GateLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.spawn = _load_spawn()
        self.record = {
            "ticket": "GRE-1",
            "role": "planner",
            "worktree": "/tmp/gre-1",
            "terminal": "term_planner",
            "task": "task_1",
            "dispatch": "ctx_1",
            "capability": "dcap_abc",
            "gate_state": self.spawn.IDLE,
            "approval_rounds": 0,
        }
        self.spawn.state_write({"GRE-1/planner": dict(self.record)})

        self.tracker = FakeTracker()
        self.spawn._linear.LinearTracker = lambda: self.tracker
        self.calls = []
        self.batch = {"deliveryId": "d1", "messages": []}

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append(list(argv))
            if argv[:2] == ("orchestration", "check"):
                return self.batch
            if argv[:2] == ("orchestration", "dispatch-show"):
                return {"dispatch": {"status": "completed"}}
            return {}

        self.spawn.orca = fake_orca

    def message(self, **over):
        base = {
            "id": "m-1", "type": "worker_done", "subject": "s", "body": DONE_BODY,
            "payload": {"taskId": "task_1", "dispatchId": "ctx_1", "outcome": "succeeded"},
        }
        base.update(over)
        return base

    def set_state(self, **over):
        data = self.spawn.state_read()
        data["GRE-1/planner"].update(over)
        self.spawn.state_write(data)


class TestWaitPersistsRetirement(GateLoopCase):
    def test_the_retired_flag_survives_the_loops_own_write(self):
        """`retire()` writes the flag straight to the registry; `wait` then
        writes the copy it read before that. Without carrying the flag over,
        a dead planner stays 'live' and the next role in the same worktree
        cannot identify itself."""

        self.set_state(gate_state=self.spawn._gate.VERDICT_APPROVED)
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertTrue(rec.get("retired"), "wait must not resurrect a retired planner")
        self.assertEqual(rec["gate_state"], self.spawn._gate.RETIRED)


class TestWaitAttribution(GateLoopCase):
    def test_a_rejection_without_a_dispatch_id_is_still_flagged(self):
        """The refusal Orca sends back for a missing `dispatchId` is exactly
        the message that cannot be attributed by dispatch. It keeps `taskId`,
        so it is attributed by that instead of vanishing."""

        self.set_state(gate_state=self.spawn._gate.VERDICT_APPROVED)
        self.batch = {"deliveryId": "d1", "messages": [self.message(payload={
            "taskId": "task_1", "outcome": "succeeded",
            "_orcaLifecycleRejection": {
                "code": "missing_dispatch_id", "reason": "worker_done requires dispatchId.",
            },
        })]}
        out = self.spawn.wait(ack=None, timeout_ms=1)
        self.assertEqual(out["unattributed"], [])
        self.assertEqual(len(self.tracker.attention), 1)
        ticket, attention = self.tracker.attention[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertIn("missing_dispatch_id", attention.reason)

    def test_a_message_from_an_unknown_dispatch_is_reported_not_swallowed(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message(
            payload={"taskId": "task_other", "dispatchId": "ctx_other", "outcome": "succeeded"},
        )]}
        out = self.spawn.wait(ack=None, timeout_ms=1)
        self.assertEqual(len(out["unattributed"]), 1)
        self.assertEqual(out["unattributed"][0]["task"], "task_other")


class TestWaitCountsRounds(GateLoopCase):
    SUBMISSION = "## Plan\nGRE-1 — comment abc\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"

    def question(self, message_id):
        return self.message(id=message_id, type="question", body=self.SUBMISSION,
                            payload={"taskId": "task_1", "dispatchId": "ctx_1"})

    def test_a_second_question_before_a_verdict_moves_the_pending_id(self):
        """If the planner's first `submit` died, it is blocked on a NEW ask.
        The verdict must go to the thread it is actually waiting on — and
        that is not a new round, because no plan was decided on."""

        self.batch = {"deliveryId": "d1", "messages": [self.question("q-1")]}
        self.spawn.wait(ack=None, timeout_ms=1)
        first = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(first["question_id"], "q-1")
        self.assertEqual(first["approval_rounds"], 1)

        self.batch = {"deliveryId": "d2", "messages": [self.question("q-2")]}
        self.spawn.wait(ack=None, timeout_ms=1)
        second = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(second["question_id"], "q-2", "the verdict must follow the planner")
        self.assertEqual(second["approval_rounds"], 1, "re-asking is not a new round")
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)], "label lit once")


class TestVerdictOrdering(GateLoopCase):
    def test_the_approval_is_recorded_before_the_planner_is_told(self):
        """The reply unblocks the planner, which may run `spawn done` at
        once. A registry that still said `submitted` would refuse the report
        the human just authorized."""

        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        seen = {}

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append(list(argv))
            if argv[:2] == ("orchestration", "reply"):
                seen["state_at_reply"] = (
                    self.spawn.state_read()["GRE-1/planner"]["gate_state"]
                )
            return {}

        self.spawn.orca = fake_orca
        self.spawn.verdict("GRE-1", "approved", "")
        self.assertEqual(seen["state_at_reply"], self.spawn._gate.VERDICT_APPROVED)

    def test_a_tracker_failure_does_not_lose_a_delivered_verdict(self):
        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        self.tracker.fail = True
        out = self.spawn.verdict("GRE-1", "approved", "")
        self.assertIn("NOT cleared", out["label"])
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["gate_state"], self.spawn._gate.VERDICT_APPROVED)
        self.assertNotIn("question_id", rec)

    def test_the_pending_question_is_only_cleared_after_the_reply_lands(self):
        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")

        def failing_orca(*argv, expect_lifecycle_ok=False):
            if argv[:2] == ("orchestration", "reply"):
                raise SystemExit("orca orchestration reply failed")
            return {}

        self.spawn.orca = failing_orca
        with self.assertRaises(SystemExit):
            self.spawn.verdict("GRE-1", "approved", "")
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["question_id"], "q-1", "a failed reply must stay retryable")


class TestRegistryLocation(GateLoopCase):
    def test_the_registry_and_its_directory_are_not_world_readable(self):
        """It holds Dispatch capability tokens, and a token is what
        authorizes a worker_done on someone else's dispatch."""

        self.spawn.state_write({"GRE-1/planner": dict(self.record)})
        self.assertEqual(self.spawn.STATE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.spawn.STATE.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.spawn.STATE.parent.glob("*.tmp")), "no temp file left behind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
