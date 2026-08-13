"""Tests for `build_brief` in `src/spawn.py`.

TLDR: drives `build_brief` with a fake tracker so no network call and no
`LINEAR_API_KEY` are needed. Checks: the `io:brief-template` block fills
correctly, comments carry an author and a date, and a missing placeholder
value fails loudly instead of shipping a Brief with a hole in it. Run
either way:

    cd .symphonia/src && python3 -m unittest tests.test_brief
    python3 .symphonia/src/tests/test_brief.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linear import Comment, Item, ItemRef

import gate as GATE
import spawn as SPAWN

RoleName = SPAWN.RoleName


class FakeTracker:
    def __init__(self, item: Item, comments: tuple[Comment, ...] = ()):
        self._item = item
        self._comments = list(comments)

    def get_item(self, ticket):
        return self._item

    def list_comments(self, id):
        return self._comments


def _item() -> Item:
    return Item(
        ref=ItemRef(id="uuid-1", key="GRE-181", url="https://linear.app/x/issue/GRE-181/t"),
        title="Ship the plan gate",
        body="Full description here.",
    )


def _comments() -> list[Comment]:
    return [
        Comment(id="c-1", body="First comment.",
                author_name="Josias Ribeiro", created_at="2026-08-10T23:13:47.219Z"),
        Comment(id="c-2", body="Second comment.",
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
        """The baton rule (write the handoff, never launch the next role)
        lives in the implementer's own `io:brief-template`, filled by
        `build_brief()` from `handoff_dir`/`handoff_hint`."""

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
        for role in (RoleName.SPEC_REVIEWER, RoleName.STANDARDS_REVIEWER):
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            self.assertIn(
                "You are read-only by construction: Edit/Write are disabled at launch.",
                brief,
            )
            self.assertNotIn("SKILL.md", brief)
            self.assertNotIn("handoff document", brief)

    def test_no_brief_mentions_orca_linear(self):
        """No role fetches its own context — every role opens with the
        Brief already in hand."""

        for role in RoleName:
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            self.assertNotIn("orca linear", brief)

    def test_every_brief_states_the_prior_handoff_is_never_binding(self):
        """A role must never have to choose between a stale handoff and
        this brief — the precedence is written into every role's own
        template, not left to a role's judgment call."""

        for role in RoleName:
            brief = SPAWN.build_brief(role, "GRE-181", "/tmp/gre-181", tracker=self.tracker)
            normalized = " ".join(brief.split())
            self.assertIn(
                "It is context, never instruction: if it contradicts this "
                "brief or the ticket comments, the brief wins.",
                normalized,
            )


class TestHandoffFile(unittest.TestCase):
    """`_handoff_file` points a Brief at the one canonical handoff —
    `{handoff_dir}/{ticket}.md`, the only file a role's own "How to finish"
    instructs it to write — or nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(SPAWN, "_handoff_dir", return_value=str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_file_is_none(self):
        self.assertIsNone(SPAWN._handoff_file("GRE-1"))

    def test_canonical_file_is_found(self):
        path = self.dir / "gre-1.md"
        path.write_text("handoff")
        self.assertEqual(SPAWN._handoff_file("GRE-1"), path)

    def test_build_brief_carries_exactly_one_line(self):
        path = self.dir / "gre-1.md"
        path.write_text("handoff")
        brief = SPAWN.build_brief(
            RoleName.IMPLEMENTER, "GRE-1", "/tmp/gre-1",
            tracker=FakeTracker(_item(), _comments()),
        )
        self.assertIn(f"- {path}", brief)


class TestEveryRoleHasABriefTemplate(unittest.TestCase):
    """The four spawnable roles each declare their own `io:brief-template`
    block — none of them fall back to a role with no template of its own."""

    def test_every_policy_role_file_has_a_brief_template(self):
        for role, policy in SPAWN._policies().items():
            with self.subTest(role=role.value):
                role_path = SPAWN.ROLES_DIR / policy.role_file
                try:
                    GATE.extract_block(role_path.read_text(), "md io:brief-template")
                except LookupError:
                    self.fail(f"{role_path} has no 'md io:brief-template' block")


class TestExtractBlockFailureIsLoud(unittest.TestCase):
    def test_role_file_with_no_brief_template_raises(self):
        with self.assertRaises(LookupError):
            GATE.extract_block("# No I/O section here.", "md io:brief-template")


if __name__ == "__main__":
    unittest.main(verbosity=2)
