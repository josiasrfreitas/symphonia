"""Golden tests for the report parser (GRE-178).

TLDR: the filled examples in ``roles/planner.md`` (the ``io:example-*``
fenced blocks) are the golden fixtures — they parse back into the fields a
human typed, and a body that drifts from the contract raises
``MalformedReport`` naming what is wrong. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_reports
    python3 .symphonia/src/adapters/tests/test_reports.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.reports import (
    MalformedReport,
    extract_block,
    format_approval_reply,
    is_plan_submission,
    parse_approval_reply,
    parse_plan_submission,
    set_approval_rounds,
    parse_planner_done,
)

PLANNER_MD = Path(__file__).resolve().parents[3] / "roles" / "planner.md"


def _example(tag: str) -> str:
    return extract_block(PLANNER_MD.read_text(), tag)


class TestExtractBlock(unittest.TestCase):
    def test_extracts_the_tagged_fence_only(self):
        text = "before\n```md io:a\nA\n```\nmiddle\n```md io:b\nB\n```\nafter"
        self.assertEqual(extract_block(text, "md io:a"), "A")
        self.assertEqual(extract_block(text, "md io:b"), "B")

    def test_missing_tag_raises(self):
        with self.assertRaises(LookupError):
            extract_block("no fences here", "md io:missing")


class TestPlanSubmissionGolden(unittest.TestCase):
    def setUp(self):
        self.body = _example("md io:example-submission")

    def test_is_recognized_as_a_submission(self):
        self.assertTrue(is_plan_submission(self.body))

    def test_parses_ticket_pointer_decisions_and_changes(self):
        report = parse_plan_submission(self.body)
        self.assertEqual(report.ticket, "GRE-181")
        self.assertIn("786809ca-8db0-4ba0-8a2b-d18ae1d070f3", report.pointer)
        self.assertEqual(len(report.decisions), 1)
        self.assertTrue(report.decisions[0].startswith("1. Where the retry counter lives"))
        self.assertEqual(report.changes, "None.")


class TestApprovalReplyGolden(unittest.TestCase):
    def test_parses_approved_with_notes(self):
        verdict = parse_approval_reply(_example("md io:example-approval"))
        self.assertTrue(verdict.approved)
        self.assertEqual(
            verdict.notes,
            ("Ship it, but note the retry counter in the PR description too.",),
        )


class TestPlannerDoneGolden(unittest.TestCase):
    def test_parses_plan_pointer_and_deviations_from_the_body(self):
        report = parse_planner_done(_example("md io:example-done"))
        self.assertIn("85dfe356-d077-436c-895c-ffc8f4bf1264", report.plan_pointer)
        self.assertEqual(report.deviations, ())

    def test_the_body_carries_no_approval_facts(self):
        """`planApproved` and `approvalRounds` are decided by the gate and
        written into the payload by `spawn done`; the parser must not offer
        them, so nothing downstream can read a role's claim by mistake."""
        report = parse_planner_done(_example("md io:example-done"))
        self.assertFalse(hasattr(report, "plan_approved"))
        self.assertFalse(hasattr(report, "approval_rounds"))

    def test_the_round_count_is_rewritten_in_place(self):
        written = set_approval_rounds(_example("md io:example-done"), 2)
        self.assertIn("2 rodadas.", written)
        report = parse_planner_done(written)
        self.assertIn("85dfe356-d077-436c-895c-ffc8f4bf1264", report.plan_pointer)

    def test_one_round_is_singular(self):
        self.assertIn("1 rodada.", set_approval_rounds(_example("md io:example-done"), 1))

    def test_sections_the_package_does_not_know_survive(self):
        """A dispatch grants one worker_done, so anything dropped here can
        never be sent again — only `## Approval` may be touched."""

        body = (
            "## Plan\nGRE-1 — p\n\n## Approval\n9 rodadas.\n\n"
            "## Deviations\nNone.\n\n## Risks\n- o retry pode duplicar\n"
        )
        written = set_approval_rounds(body, 2)
        self.assertIn("## Risks\n- o retry pode duplicar", written)
        self.assertIn("2 rodadas.", written)
        self.assertNotIn("9 rodadas.", written)

    def test_a_body_without_the_section_is_refused(self):
        with self.assertRaises(MalformedReport):
            set_approval_rounds("## Plan\np\n\n## Deviations\nNone.\n", 1)


class TestIsPlanSubmission(unittest.TestCase):
    def test_false_when_first_line_is_not_plan(self):
        self.assertFalse(is_plan_submission("Hello\n\n## Plan\nfoo"))

    def test_false_on_empty_body(self):
        self.assertFalse(is_plan_submission(""))


class TestMalformedSubmission(unittest.TestCase):
    def test_missing_decisions_section_names_it(self):
        body = "## Plan\nGRE-1 — pointer\n\n## Changes\nNone.\n"
        with self.assertRaises(MalformedReport) as ctx:
            parse_plan_submission(body)
        self.assertIn("Decisions", str(ctx.exception))

    def test_missing_changes_section_names_it(self):
        body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n"
        with self.assertRaises(MalformedReport) as ctx:
            parse_plan_submission(body)
        self.assertIn("Changes", str(ctx.exception))

    def test_first_line_not_exactly_plan_raises(self):
        body = "## Plans\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        with self.assertRaises(MalformedReport):
            parse_plan_submission(body)


class TestFormatApprovalReplyRoundTrips(unittest.TestCase):
    """A3: `spawn verdict` formats through this instead of hand-assembling
    the token/notes shape — round-trips through `parse_approval_reply`."""

    def test_approved_with_notes_round_trips(self):
        body = format_approval_reply("APPROVED", ["Ship it, but note the retry counter too."])
        verdict = parse_approval_reply(body)
        self.assertTrue(verdict.approved)
        self.assertEqual(verdict.notes, ("Ship it, but note the retry counter too.",))

    def test_revise_with_no_notes_round_trips(self):
        body = format_approval_reply("REVISE", [])
        verdict = parse_approval_reply(body)
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.notes, ())

    def test_unknown_token_rejected(self):
        with self.assertRaises(ValueError):
            format_approval_reply("MAYBE", [])


class TestMalformedApproval(unittest.TestCase):
    def test_invalid_token_raises(self):
        with self.assertRaises(MalformedReport) as ctx:
            parse_approval_reply("MAYBE\n\n- a note\n")
        self.assertIn("APPROVED", str(ctx.exception))
        self.assertIn("REVISE", str(ctx.exception))

    def test_empty_body_raises(self):
        with self.assertRaises(MalformedReport):
            parse_approval_reply("")


class TestMalformedPlannerDone(unittest.TestCase):
    def test_missing_section_names_it(self):
        body = "## Plan\npointer\n\n## Deviations\nNone.\n"
        with self.assertRaises(MalformedReport) as ctx:
            parse_planner_done(body)
        self.assertIn("Approval", str(ctx.exception))

    def test_empty_body_names_the_first_missing_section(self):
        with self.assertRaises(MalformedReport) as ctx:
            parse_planner_done("")
        self.assertIn("Plan", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
