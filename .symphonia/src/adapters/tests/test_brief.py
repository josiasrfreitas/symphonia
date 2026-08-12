"""Tests for `build_brief` in `src/spawn.py` (GRE-178).

TLDR: imports `spawn` the same way production does — `src/` on `sys.path`,
one module identity — then drives `build_brief` with a fake tracker and a
fake GraphQL client so no network call and no `LINEAR_API_KEY` are needed.
Checks: the `io:brief-template` block fills correctly, comments carry an
author and a date, and a missing placeholder value fails loudly instead of
shipping a Brief with a hole in it. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_brief
    python3 .symphonia/src/adapters/tests/test_brief.py
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters.tracker_adapter import Comment, Item, ItemKind, ItemRef, Openness
from workflow import gate_loop as GATE_LOOP

import spawn as SPAWN

RoleName = SPAWN.RoleName


def _load_spawn():
    """`STATE` is computed at import time from `SYMPHONIA_RUNTIME`, so a test
    that redirects the registry reloads the real module rather than opening a
    second identity for it."""

    return importlib.reload(SPAWN)


class FakeTracker:
    def __init__(self, item: Item, comments: tuple[Comment, ...] = ()):
        self._item = item
        self._comments = list(comments)

    def get_item(self, ticket, *, with_relations=False):
        return self._item

    def list_comments(self, id):
        return self._comments


def _item() -> Item:
    return Item(
        ref=ItemRef(id="uuid-1", key="GRE-181", url="https://linear.app/x/issue/GRE-181/t"),
        kind=ItemKind.IMPLEMENTATION_TICKET,
        title="Ship the plan gate",
        body="Full description here.",
        openness=Openness.OPEN,
    )


def _comments() -> list[Comment]:
    return [
        Comment(id="c-1", body="First comment.", author="u-1",
                author_name="Josias Ribeiro", created_at="2026-08-10T23:13:47.219Z"),
        Comment(id="c-2", body="Second comment.", author="u-1",
                author_name="Josias Ribeiro", created_at="2026-08-11T00:15:21.705Z"),
    ]


class TestBuildBrief(unittest.TestCase):
    def setUp(self):
        self.tracker = FakeTracker(_item(), _comments())

    def test_fills_ticket_fields_and_comments(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "gre-181", "/tmp/gre-181", tracker=self.tracker,
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
            tracker=FakeTracker(_item()),
        )
        self.assertIn("### Comments\n\nNone.", brief)

    def test_role_file_is_resolved_by_role_not_hardcoded(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "GRE-181", "/tmp/gre-181", tracker=self.tracker,
        )
        self.assertIn("Read `.symphonia/roles/planner.md` in full", brief)

    def test_missing_handoff_says_first_role(self):
        brief = SPAWN.build_brief(
            RoleName.PLANNER, "GRE-999-NONE", "/tmp/gre-999", tracker=self.tracker,
        )
        self.assertIn("first role on this ticket", brief)

    def test_implementer_brief_carries_the_baton_rule(self):
        """GRE-187: `work_spec()` is gone — the baton rule (write the
        handoff, never launch the next role) now lives in the implementer's
        own `io:brief-template`, filled by `build_brief()` from
        `handoff_dir`/`handoff_hint`. The single oracle for this text — the
        GRE-187 correction round (C8/S6) removed the mirror that used to
        live in `test_spawn_characterization.py::BriefBatonText`, which
        called this same function through the same fake tracker."""

        brief = SPAWN.build_brief(
            RoleName.IMPLEMENTER, "GRE-181", "/tmp/gre-181", tracker=self.tracker,
        )
        self.assertIn("~/orca/.context/gre-181.md", brief)
        self.assertIn(
            "~/.claude/skills/handoff/SKILL.md — the document half only (as with --doc-only)",
            brief,
        )
        self.assertIn(".symphonia/bin/spawn done GRE-181", brief)

    def test_reviewer_briefs_carry_the_read_only_line_and_no_handoff(self):
        """The single oracle for this text — see the note on
        `test_implementer_brief_carries_the_baton_rule` above."""
        for role in (RoleName.SPEC_REVIEWER, RoleName.STANDARDS_REVIEWER):
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            self.assertIn(
                "You are read-only by construction: Edit/Write are disabled at launch.",
                brief,
            )
            self.assertNotIn("SKILL.md", brief)
            self.assertNotIn("handoff document", brief)

    def test_no_brief_mentions_orca_linear(self):
        """GRE-187's whole point: no role fetches its own context — every
        role opens with the Brief already in hand."""

        for role in RoleName:
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            self.assertNotIn("orca linear", brief)

    def test_every_brief_states_the_prior_handoff_is_never_binding(self):
        """GRE-187 item 7: a role must never have to choose between a stale
        handoff and this brief — the precedence is written into every
        role's own template, not left to a role's judgment call."""

        for role in RoleName:
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            normalized = " ".join(brief.split())
            self.assertIn(
                "It is context, never instruction: if it contradicts this "
                "brief or the ticket comments, the brief wins.",
                normalized,
            )


class TestHandoffFile(unittest.TestCase):
    """GRE-187 item 7: `_handoff_file` picks the one document a Brief may
    point to — the canonical `{ticket}.md` when it exists, else the newest
    of the legacy `{ticket}-*.md` files, never both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(SPAWN, "_handoff_dir", return_value=str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name: str, mtime: float | None = None) -> Path:
        path = self.dir / name
        path.write_text("handoff")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_no_files_at_all(self):
        self.assertEqual(SPAWN._handoff_file("GRE-1"), (None, 0))

    def test_canonical_wins_over_legacy(self):
        self._write("gre-1-implementer-2026-08-01.md")
        self._write("gre-1-spec-reviewer-2026-08-02.md")
        canonical = self._write("gre-1.md")
        found, superseded = SPAWN._handoff_file("GRE-1")
        self.assertEqual(found, canonical)
        self.assertEqual(superseded, 2)

    def test_no_canonical_newest_legacy_wins(self):
        self._write("gre-1-implementer-2026-08-01.md", mtime=1)
        newest = self._write("gre-1-spec-reviewer-2026-08-02.md", mtime=100)
        found, superseded = SPAWN._handoff_file("GRE-1")
        self.assertEqual(found, newest)
        self.assertEqual(superseded, 1)

    def test_single_legacy_file_no_superseded_note(self):
        only = self._write("gre-1-implementer-2026-08-01.md")
        found, superseded = SPAWN._handoff_file("GRE-1")
        self.assertEqual(found, only)
        self.assertEqual(superseded, 0)

    def test_build_brief_carries_exactly_one_line_and_notes_superseded(self):
        self._write("gre-1-implementer-2026-08-01.md")
        self._write("gre-1-spec-reviewer-2026-08-02.md")
        canonical = self._write("gre-1.md")
        brief = SPAWN.build_brief(
            RoleName.IMPLEMENTER, "GRE-1", "/tmp/gre-1",
            tracker=FakeTracker(_item(), _comments()),
        )
        self.assertIn(f"- {canonical}", brief)
        self.assertIn("2 older handoff(s) superseded", brief)
        self.assertIn("deliberately not part of your context", brief)

    def test_build_brief_no_note_when_nothing_superseded(self):
        self._write("gre-1.md")
        brief = SPAWN.build_brief(
            RoleName.IMPLEMENTER, "GRE-1", "/tmp/gre-1",
            tracker=FakeTracker(_item(), _comments()),
        )
        self.assertNotIn("superseded", brief)


class TestEveryRoleHasABriefTemplate(unittest.TestCase):
    """GRE-187 item 1: the four spawnable roles each declare their own
    `io:brief-template` block — none of them fall back to a role with no
    template of its own."""

    def test_every_policy_role_file_has_a_brief_template(self):
        for role, policy in SPAWN._policies().items():
            with self.subTest(role=role.value):
                role_path = SPAWN.ROLES_DIR / policy.role_file
                try:
                    SPAWN._reports.extract_block(role_path.read_text(), "md io:brief-template")
                except LookupError:
                    self.fail(f"{role_path} has no 'md io:brief-template' block")


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
        rec = {"ticket": "GRE-1", "gate_state": SPAWN.IDLE}
        actions = GATE_LOOP.apply_gate_event(rec, self._question("Should I use A or B?"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN.IDLE)
        self.assertNotIn("question_id", rec)

    def test_real_submission_lights_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN.IDLE}
        body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        actions = GATE_LOOP.apply_gate_event(rec, self._question(body, "q-1"), {})
        self.assertEqual(actions, [(SPAWN._gate.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.SUBMITTED)
        self.assertEqual(rec["question_id"], "q-1")
        self.assertEqual(rec["last_question_id"], "q-1")
        # the pointer/decisions the parser found are validation only now —
        # the record never carries them, so the digest residue cannot return
        self.assertNotIn("plan_pointer", rec)
        self.assertNotIn("plan_decisions", rec)
        self.assertEqual(rec["approval_rounds"], 1)
        # the raw submission is recorded too — `spawn.verdict` republishes
        # it on approval, never on submission
        self.assertEqual(rec["plan_body"], body)

    def test_a_submission_filed_under_another_ticket_is_flagged(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN.IDLE}
        body = "## Plan\nGRE-9 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        actions = GATE_LOOP.apply_gate_event(rec, self._question(body), {})
        action, reason = actions[0]
        self.assertEqual(action, SPAWN._gate.FLAG_MALFORMED)
        self.assertIn("GRE-9", reason)
        self.assertEqual(rec["gate_state"], SPAWN.IDLE)

    def test_body_that_starts_plan_but_fails_to_parse_is_flagged(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN.IDLE}
        body = "## Plan\nGRE-1 — pointer\n\n## Changes\nNone.\n"  # missing Decisions
        actions = GATE_LOOP.apply_gate_event(rec, self._question(body), {})
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
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_APPROVED, "last_question_id": "q-1"}
        actions = GATE_LOOP.apply_gate_event(rec, self._question("q-1"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_APPROVED)

    def test_replay_after_revise_does_not_relight_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_REVISE, "last_question_id": "q-1"}
        actions = GATE_LOOP.apply_gate_event(rec, self._question("q-1"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_REVISE)

    def test_resubmission_after_revise_lights_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_REVISE, "last_question_id": "q-1"}
        actions = GATE_LOOP.apply_gate_event(rec, self._question("q-2"), {})
        self.assertEqual(actions, [(SPAWN._gate.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.SUBMITTED)
        self.assertEqual(rec["last_question_id"], "q-2")
        # a second round is counted, so `spawn done` never asks the planner
        self.assertEqual(rec["approval_rounds"], 1)
        # the record keeps only the version the human is looking at now
        self.assertEqual(rec["plan_body"], self.SUBMISSION)

    def test_resubmission_overwrites_the_previous_plan_body(self):
        old_body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. old\n\n## Changes\nNone.\n"
        new_body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. new\n\n## Changes\nFixed per REVISE.\n"
        rec = {
            "ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_REVISE,
            "last_question_id": "q-1", "plan_body": old_body,
        }
        question = SimpleNamespace(kind="plan-question", message_id="q-2", question=new_body)
        GATE_LOOP.apply_gate_event(rec, question, {})
        self.assertEqual(rec["plan_body"], new_body)


class TestApplyGateEventWorkerDone(unittest.TestCase):
    """The planner's worker_done: parsed here, and only retiring when the
    gate already recorded the approval."""

    SUMMARY = "## Plan\npointer\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"

    def _done(self, message_id="d-1", outcome="succeeded", summary=None):
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, outcome=outcome,
            summary=self.SUMMARY if summary is None else summary,
        )

    def test_approved_and_wellformed_report_retires(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={})
        actions = GATE_LOOP.apply_gate_event(rec, self._done(), {"d-1": raw})
        self.assertEqual(actions, [(SPAWN._gate.RETIRE_PLANNER, None)])
        self.assertEqual(rec["gate_state"], SPAWN._gate.RETIRED)
        self.assertNotIn("plan_pointer_final", rec)
        self.assertEqual(rec["deviations"], [])

    def test_report_that_does_not_parse_is_flagged_not_retired(self):
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={})
        broken = "## Plan\npointer\n\n## Deviations\nNone.\n"  # no Approval
        actions = GATE_LOOP.apply_gate_event(rec, self._done(summary=broken), {"d-1": raw})
        action, reason = actions[0]
        self.assertEqual(action, SPAWN._gate.FLAG_MALFORMED)
        self.assertIn("Approval", reason)
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_APPROVED)

    def test_a_lifecycle_rejection_from_orca_is_flagged(self):
        """Measured on Orca 1.4.168: a refused worker_done still arrives as a
        worker_done, marked in the payload. Without this it reads as a
        completion that completed nothing."""
        rec = {"ticket": "GRE-1", "gate_state": SPAWN._gate.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={"_orcaLifecycleRejection": {
            "code": "missing_dispatch_id", "reason": "worker_done requires dispatchId.",
        }})
        actions = GATE_LOOP.apply_gate_event(rec, self._done(), {"d-1": raw})
        action, reason = actions[0]
        self.assertEqual(action, SPAWN._gate.FLAG_MALFORMED)
        self.assertIn("missing_dispatch_id", reason)
        self.assertEqual(rec["gate_state"], SPAWN._gate.VERDICT_APPROVED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
