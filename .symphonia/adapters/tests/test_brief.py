"""Tests for `build_brief` in `bin/spawn` (GRE-178).

TLDR: loads `bin/spawn` the same way it loads its own dependencies —
`importlib` by file path, since the package lives in a dot-directory no
import statement can name — then drives `build_brief` with a fake tracker
and a fake GraphQL client so no network call and no `LINEAR_API_KEY` are
needed. Checks: the `io:brief-template` block fills correctly, comments
carry an author and a date, and a missing placeholder value fails loudly
instead of shipping a Brief with a hole in it. Run either way:

    cd .symphonia && python3 -m unittest adapters.tests.test_brief
    python3 .symphonia/adapters/tests/test_brief.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE))

from adapters.tracker_adapter import Item, ItemKind, ItemRef, Openness


def _load_spawn():
    """Mirrors `bin/spawn`'s own `_load`: the module has no `.py` suffix and
    lives in a dot-directory, so it is loaded by file path, not import."""

    path = PACKAGE / "bin" / "spawn"
    loader = importlib.machinery.SourceFileLoader("spawn_under_test", str(path))
    spec = importlib.util.spec_from_loader("spawn_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


SPAWN = _load_spawn()
# `spawn` loads its own copy of `runtime_adapter.py` under the
# `symphonia_pkg` name (it cannot `import adapters...` — the package lives
# in a dot-directory). `RoleName` must come from that same copy: an enum
# member from the `adapters.*` import above is a different class and would
# fail every dict lookup keyed by `SPAWN`'s `RoleName`.
RoleName = SPAWN.RoleName


class FakeTracker:
    def __init__(self, item: Item):
        self._item = item

    def get_item(self, ticket, *, with_relations=False):
        return self._item


class FakeClient:
    def __init__(self, comments: list[dict]):
        self._comments = comments

    def query(self, gql, variables=None):
        assert "comments(first: 100)" in gql
        return {"issue": {"comments": {"nodes": self._comments}}}


def _item() -> Item:
    return Item(
        ref=ItemRef(id="uuid-1", key="GRE-181", url="https://linear.app/x/issue/GRE-181/t"),
        kind=ItemKind.IMPLEMENTATION_TICKET,
        title="Ship the plan gate",
        body="Full description here.",
        openness=Openness.OPEN,
    )


class TestBuildBrief(unittest.TestCase):
    def setUp(self):
        self.tracker = FakeTracker(_item())
        self.client = FakeClient([
            {"body": "First comment.", "createdAt": "2026-08-10T23:13:47.219Z",
             "user": {"name": "Josias Ribeiro"}},
            {"body": "Second comment.", "createdAt": "2026-08-11T00:15:21.705Z",
             "user": {"name": "Josias Ribeiro"}},
        ])

    def test_fills_ticket_fields_and_comments(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "gre-181", "/tmp/gre-181",
            tracker=self.tracker, client=self.client,
        )
        self.assertIn("GRE-181", brief)
        self.assertIn("Ship the plan gate", brief)
        self.assertIn("https://linear.app/x/issue/GRE-181/t", brief)
        self.assertIn("Full description here.", brief)
        self.assertIn("Josias Ribeiro · 2026-08-10", brief)
        self.assertIn("First comment.", brief)
        self.assertIn("Josias Ribeiro · 2026-08-11", brief)
        self.assertIn("Second comment.", brief)

    def test_no_comments_says_none(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "GRE-181", "/tmp/gre-181",
            tracker=self.tracker, client=FakeClient([]),
        )
        self.assertIn("### Comments\n\nNone.", brief)

    def test_role_file_is_resolved_by_role_not_hardcoded(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "GRE-181", "/tmp/gre-181",
            tracker=self.tracker, client=self.client,
        )
        self.assertIn("Read `.symphonia/roles/planner.md` in full", brief)

    def test_missing_handoff_says_first_role(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "GRE-999-NONE", "/tmp/gre-999",
            tracker=self.tracker, client=self.client,
        )
        self.assertIn("first role on this ticket", brief)


class TestExtractBlockFailureIsLoud(unittest.TestCase):
    def test_role_file_with_no_brief_template_raises(self):
        with self.assertRaises(LookupError):
            SPAWN._reports.extract_block("# No I/O section here.", "md io:brief-template")


class TestApplyGateEventQuestionFiltering(unittest.TestCase):
    """A1: `wait`'s gate must only react to a real plan submission — a
    clarifying question from the planner must not light the label."""

    def _question(self, body, message_id="q-1"):
        return SimpleNamespace(kind="plan-question", message_id=message_id, question=body)

    def test_non_submission_question_is_not_a_gate_event(self):
        rec = {"gate_state": SPAWN.IDLE}
        actions = SPAWN._apply_gate_event(rec, self._question("Should I use A or B?"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN.IDLE)
        self.assertNotIn("question_id", rec)

    def test_real_submission_lights_the_label(self):
        rec = {"gate_state": SPAWN.IDLE}
        body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        actions = SPAWN._apply_gate_event(rec, self._question(body, "q-1"), {})
        self.assertEqual(actions, [(SPAWN._gate.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.SUBMITTED)
        self.assertEqual(rec["question_id"], "q-1")
        self.assertEqual(rec["last_question_id"], "q-1")

    def test_body_that_starts_plan_but_fails_to_parse_is_flagged(self):
        rec = {"gate_state": SPAWN.IDLE}
        body = "## Plan\nGRE-1 — pointer\n\n## Changes\nNone.\n"  # missing Decisions
        actions = SPAWN._apply_gate_event(rec, self._question(body), {})
        self.assertEqual(len(actions), 1)
        action, reason = actions[0]
        self.assertEqual(action, SPAWN._gate.FLAG_MALFORMED)
        self.assertIn("Decisions", reason)
        self.assertEqual(rec["gate_state"], SPAWN.IDLE)  # never toggled


class TestApplyGateEventReplayVsResubmission(unittest.TestCase):
    """A2: replay of an unacked Delivery must not relight the label or
    revert an already-decided verdict; a genuine resubmission after REVISE
    must."""

    SUBMISSION = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"

    def _question(self, message_id):
        return SimpleNamespace(kind="plan-question", message_id=message_id, question=self.SUBMISSION)

    def test_replay_after_approval_does_not_relight_the_label(self):
        rec = {"gate_state": SPAWN._gate.VERDICT_APPROVED, "last_question_id": "q-1"}
        actions = SPAWN._apply_gate_event(rec, self._question("q-1"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_APPROVED)

    def test_replay_after_revise_does_not_relight_the_label(self):
        rec = {"gate_state": SPAWN._gate.VERDICT_REVISE, "last_question_id": "q-1"}
        actions = SPAWN._apply_gate_event(rec, self._question("q-1"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_REVISE)

    def test_resubmission_after_revise_lights_the_label(self):
        rec = {"gate_state": SPAWN._gate.VERDICT_REVISE, "last_question_id": "q-1"}
        actions = SPAWN._apply_gate_event(rec, self._question("q-2"), {})
        self.assertEqual(actions, [(SPAWN._gate.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.SUBMITTED)
        self.assertEqual(rec["last_question_id"], "q-2")


class TestApplyGateEventWorkerDoneUsesRawPayload(unittest.TestCase):
    """A1: the planner's worker_done must be parsed through
    `reports.parse_planner_done`, not accepted on gate_state alone."""

    def _done(self, message_id="d-1", outcome="succeeded"):
        summary = "## Plan\npointer\n\n## Approval\n1 round.\n\n## Deviations\nNone.\n"
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, outcome=outcome, summary=summary,
        )

    def test_approved_and_matching_payload_retires(self):
        rec = {"gate_state": SPAWN._gate.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={"planApproved": True, "approvalRounds": 1})
        actions = SPAWN._apply_gate_event(rec, self._done(), {"d-1": raw})
        self.assertEqual(actions, [(SPAWN._gate.RETIRE_PLANNER, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.RETIRED)

    def test_approved_state_with_planApproved_false_payload_is_flagged_not_retired(self):
        rec = {"gate_state": SPAWN._gate.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={"planApproved": False, "approvalRounds": 1})
        actions = SPAWN._apply_gate_event(rec, self._done(), {"d-1": raw})
        self.assertEqual(len(actions), 1)
        action, _ = actions[0]
        self.assertEqual(action, SPAWN._gate.FLAG_MALFORMED)
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_APPROVED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
