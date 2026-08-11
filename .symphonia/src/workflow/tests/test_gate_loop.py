"""Tests for `workflow.gate_loop.run` — the attribution/execution loop.

TLDR: `apply_gate_event` (the per-event decision) is covered in
`adapters.tests.test_brief`; this file covers the loop AROUND it, driven
directly with a plain registry dict, a fake tracker and a fake `retire` —
no CLI, no `importlib.reload`, no monkeypatching a module. Run either way:

    cd .symphonia/src && python3 -m unittest workflow.tests.test_gate_loop
    python3 .symphonia/src/workflow/tests/test_gate_loop.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from workflow import gate_loop as GATE_LOOP

SUBMISSION = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
DONE_BODY = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class FakeTracker:
    """Records what the loop asked of it; never touches a network."""

    def __init__(self):
        self.gate_calls, self.attention = [], []

    def set_gate(self, ticket, on):
        self.gate_calls.append((ticket, on))

    def set_attention(self, ticket, attention):
        self.attention.append((ticket, attention))


class GateLoopRunCase(unittest.TestCase):
    def setUp(self):
        self.record = {
            "ticket": "GRE-1", "role": "planner",
            "dispatch": "ctx_1", "task": "task_1",
            "gate_state": GATE_LOOP.IDLE, "approval_rounds": 0,
        }
        self.data = {"GRE-1/planner": dict(self.record)}
        self.tracker = FakeTracker()
        self.retired = []

    def _run(self, events, raw_by_id=None, gate_role="planner"):
        return GATE_LOOP.run(
            events, raw_by_id or {}, self.data,
            tracker=lambda: self.tracker,
            retire=lambda ticket: self.retired.append(ticket),
            gate_role=gate_role,
        )

    def question(self, message_id="q-1", dispatch_id="ctx_1", task_id="task_1"):
        return SimpleNamespace(
            kind="plan-question", message_id=message_id, question=SUBMISSION,
            dispatch_id=dispatch_id, task_id=task_id,
        )

    def done(self, message_id="d-1", dispatch_id="ctx_1", task_id="task_1", body=DONE_BODY,
             outcome="succeeded"):
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, summary=body, outcome=outcome,
            dispatch_id=dispatch_id, task_id=task_id,
        )


class TestAttribution(GateLoopRunCase):
    def test_attributed_by_dispatch_id(self):
        actions, unattributed = self._run([self.question()])
        self.assertEqual(unattributed, [])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.LABEL_ON}])
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)])

    def test_falls_back_to_task_id_when_dispatch_id_is_absent(self):
        event = self.question(dispatch_id=None)
        actions, unattributed = self._run([event])
        self.assertEqual(unattributed, [])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.LABEL_ON}])

    def test_a_message_from_an_unknown_dispatch_is_reported_not_swallowed(self):
        event = self.question(dispatch_id="ctx_other", task_id="task_other")
        actions, unattributed = self._run([event])
        self.assertEqual(actions, [])
        self.assertEqual(len(unattributed), 1)
        self.assertEqual(unattributed[0]["task"], "task_other")

    def test_an_event_for_another_role_is_left_alone(self):
        actions, unattributed = self._run([self.question()], gate_role="implementer")
        self.assertEqual(actions, [])
        self.assertEqual(unattributed, [])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE_LOOP.IDLE)


class TestRetireCarryOver(GateLoopRunCase):
    def test_the_retired_flag_survives_alongside_a_separate_retire_write(self):
        """`retire()` re-reads and rewrites the registry on its own, outside
        this `data` copy; `run` still has to carry the flag over so the
        caller's `state_write` does not resurrect the retired role."""

        self.data["GRE-1/planner"]["gate_state"] = GATE_LOOP._gate.VERDICT_APPROVED
        actions, _ = self._run([self.done()])
        self.assertEqual(self.retired, ["GRE-1"])
        self.assertTrue(self.data["GRE-1/planner"]["retired"])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE_LOOP._gate.RETIRED)
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.RETIRE_PLANNER}])


class TestApprovalRounds(GateLoopRunCase):
    def test_a_replayed_question_does_not_recount_the_round(self):
        self._run([self.question("q-1")])
        self.assertEqual(self.data["GRE-1/planner"]["approval_rounds"], 1)

        self._run([self.question("q-1")])
        self.assertEqual(self.data["GRE-1/planner"]["approval_rounds"], 1, "a replay is not a new round")
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)], "label lit once")


class TestFlagMalformed(GateLoopRunCase):
    def test_a_malformed_done_report_is_flagged_with_a_reason(self):
        self.data["GRE-1/planner"]["gate_state"] = GATE_LOOP._gate.VERDICT_APPROVED
        actions, _ = self._run([self.done(body="not a done report")])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.FLAG_MALFORMED}])
        self.assertEqual(len(self.tracker.attention), 1)
        ticket, attention = self.tracker.attention[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertTrue(attention.reason)
        self.assertEqual(self.retired, [], "a malformed report must not retire the role")


if __name__ == "__main__":
    unittest.main(verbosity=2)
