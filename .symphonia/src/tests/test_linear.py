"""Tests for `linear` — the pure body-patcher, and nothing else.

The twelve tracker operations were driven through a fake `LinearClient`;
those tests are gone, and with them the only offline check on the GraphQL
this module sends. `patch_section` needs no client and stays. Run either
way:

    cd .symphonia/src && python3 -m unittest tests.test_linear
    python3 .symphonia/src/tests/test_linear.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import linear as LINEAR

CONFIG = {"attention_label": "needs-attention", "gate_label": "human-gate"}

TEAM_UUID = "11111111-2222-3333-4444-555555555555"
"""A team id shaped the way Linear really returns one. The tests below
that pass a team by hand use this and not a readable stand-in: `--team`
now tells a UUID from a team key by its shape, so a fixture like
`"team-1"` would exercise the key path while claiming to be an id."""

CREATED = {"issueCreate": {"issue": {
    "id": "uuid-2", "identifier": "SYM-9", "url": "https://linear.app/x/SYM-9"}}}

ISSUE = {
    "id": "uuid-1", "identifier": "SYM-8", "url": "https://linear.app/x/SYM-8",
    "title": "the map", "description": "## Index\nold\n", "team": {"id": TEAM_UUID},
}






# --- the pure part ----------------------------------------------------------


class PatchSection(unittest.TestCase):
    BODY = "intro\n\n## Index\nold line\n\n## Fog\nkeep me\n"

    def test_replaces_only_the_named_section(self):
        out = LINEAR.patch_section(self.BODY, "Index", "new line")
        self.assertIn("new line", out)
        self.assertNotIn("old line", out)
        self.assertIn("intro", out)
        self.assertIn("## Fog\nkeep me", out)

    def test_replaces_the_last_section_without_eating_the_body(self):
        out = LINEAR.patch_section(self.BODY, "Fog", "none left")
        self.assertIn("## Index\nold line", out)
        self.assertTrue(out.rstrip().endswith("none left"))

    def test_appends_when_the_section_is_absent(self):
        out = LINEAR.patch_section(self.BODY, "Excluded", "nothing yet")
        self.assertIn("## Excluded\nnothing yet", out)
        self.assertIn("## Index\nold line", out)

    def test_appends_into_an_empty_body(self):
        self.assertEqual(LINEAR.patch_section("", "Index", "first"), "## Index\nfirst\n")

    def test_multi_line_content_survives_whole(self):
        out = LINEAR.patch_section(self.BODY, "Index", "a\nb\nc")
        self.assertIn("## Index\na\nb\nc", out)
        self.assertIn("## Fog", out)

    # A `## ` inside a fenced code block is a template, not a heading. The
    # body of SYM-8 carries exactly this, so the section this tool patches
    # in a real card depends on the distinction.
    FENCED = "## Plan\nx\n\n```md\n## Fake\ntemplate\n```\n\n## End\nz\n"

    def test_a_fenced_heading_does_not_end_the_section_early(self):
        out = LINEAR.patch_section(self.FENCED, "Plan", "y")
        self.assertEqual(out, "## Plan\ny\n\n## End\nz\n")

    def test_a_fenced_heading_is_not_a_section_to_patch(self):
        out = LINEAR.patch_section(self.FENCED, "Fake", "replaced")
        self.assertIn("```md\n## Fake\ntemplate\n```", out)
        self.assertTrue(out.endswith("## Fake\nreplaced\n"))

    def test_a_fenced_heading_survives_a_patch_of_a_later_section(self):
        out = LINEAR.patch_section(self.FENCED, "End", "w")
        self.assertIn("## Plan\nx\n\n```md\n## Fake\ntemplate\n```", out)
        self.assertTrue(out.endswith("## End\nw\n"))

    def test_a_tilde_fence_hides_a_heading_the_same_way(self):
        body = "## Plan\nx\n\n~~~\n## Fake\n~~~\n\n## End\nz\n"
        self.assertEqual(LINEAR.patch_section(body, "Plan", "y"), "## Plan\ny\n\n## End\nz\n")

    def test_the_trailing_newline_is_not_eaten(self):
        self.assertTrue(LINEAR.patch_section(self.BODY, "Fog", "none left").endswith("\n"))
        self.assertTrue(LINEAR.patch_section(self.BODY, "Index", "new").endswith("\n"))

    def test_a_body_without_a_trailing_newline_does_not_grow_one(self):
        self.assertEqual(LINEAR.patch_section("## A\nold", "A", "new"), "## A\nnew")


# --- creating ---------------------------------------------------------------








# --- assigning and closing --------------------------------------------------








# --- listing ----------------------------------------------------------------






# --- priority ---------------------------------------------------------------




if __name__ == "__main__":
    unittest.main()
