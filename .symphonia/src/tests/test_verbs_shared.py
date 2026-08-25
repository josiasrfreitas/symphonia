"""Tests for `verbs/_shared.py` — the body formats the bureaucracy verbs
write and read.

Nothing here needs a tracker: these are pure functions over markdown, so
the file survived the removal of the faked-tracker tests intact. What went
with them is `new` and `ticket` — the two verbs that write cards — whose
behaviour is now covered by nothing.

`TheTwinsAgree` is the one to keep an eye on: it holds `_shared.empty_section`
and `intake.fog_items` to the same answer, and it is what stops the two
readings of the fog from drifting apart again. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_verbs_shared
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import intake as INTAKE
from linear import Child, Item, ItemRef
from verbs import _shared as S




def child(key, *, title="t", state="Todo", state_type="unstarted",
          assignee="", blocked_by=(), priority=""):
    return Child(
        key=key, url=f"https://x/{key}", title=title, state=state,
        state_type=state_type, assignee=assignee,
        blocked_by=tuple(blocked_by), priority=priority,
    )


def item(key="SYM-8", title="Intake v2", body=""):
    return Item(ref=ItemRef(id=f"uuid-{key}", key=key, url=f"https://x/{key}"),
                title=title, body=body)






# --- _shared: the map's body ------------------------------------------------


class Sections(unittest.TestCase):
    BODY = (
        "## Destination\nship intake v2\n\n"
        "## Notes\nbrownfield\n\n"
        "## Decisions so far\n\n"
        "## Not yet specified\nwhich door\n\n"
        "## Out of scope\n- [SYM-40 — old idea](https://x/SYM-40) — dropped\n"
    )

    def test_a_section_is_read_by_its_heading(self):
        self.assertEqual(S.section(self.BODY, S.DESTINATION), "ship intake v2")
        self.assertEqual(S.section(self.BODY, S.FOG), "which door")

    def test_an_absent_section_is_none_not_empty(self):
        self.assertIsNone(S.section("## Notes\nx\n", S.DESTINATION))
        self.assertFalse(S.empty_section("## Notes\nx\n", S.DESTINATION))
        self.assertTrue(S.empty_section(self.BODY, S.DECISIONS))

    def test_a_heading_inside_a_fence_is_quoted_text(self):
        body = "## Destination\nreal\n\n```\n## Notes\nquoted\n```\n"
        self.assertIsNone(S.section(body, S.NOTES))
        self.assertIn("quoted", S.section(body, S.DESTINATION))

    def test_the_fog_heading_is_the_upstream_string(self):
        # The `wayfinder` skill writes this body; SYM-12 declares the twin
        # of this constant with the same literal. If this string drifts,
        # every map written by the skill stops being readable here.
        self.assertEqual(S.FOG, "Not yet specified")

    def test_a_section_holding_only_a_comment_is_empty(self):
        # The `wayfinder` template leaves `## Not yet specified` holding one
        # `<!-- see "Fog of war"... -->` and nothing else. Counting the
        # comment read every map the skill wrote as a map whose fog is
        # still full, so `validate` never declared the end of one.
        body = self.BODY.replace(
            "## Not yet specified\nwhich door",
            '## Not yet specified\n\n<!-- see "Fog of war": ... -->',
        )
        self.assertTrue(S.empty_section(body, S.FOG))

    def test_a_comment_spanning_lines_is_still_not_content(self):
        body = self.BODY.replace(
            "## Not yet specified\nwhich door",
            "## Not yet specified\n\n<!-- one\ntwo\nthree -->",
        )
        self.assertTrue(S.empty_section(body, S.FOG))

    def test_content_beside_a_comment_is_still_content(self):
        body = self.BODY.replace(
            "## Not yet specified\nwhich door",
            "## Not yet specified\n\n<!-- hint -->\nwhich door",
        )
        self.assertFalse(S.empty_section(body, S.FOG))

    def test_a_new_body_has_all_five_sections_three_of_them_empty(self):
        body = S.blank_map_body("ship it", "brownfield")
        for heading in S.SECTIONS:
            self.assertIsNotNone(S.section(body, heading), heading)
        self.assertEqual(S.section(body, S.DESTINATION), "ship it")
        for heading in (S.DECISIONS, S.FOG, S.OUT_OF_SCOPE):
            self.assertTrue(S.empty_section(body, heading), heading)


class TheTwinsAgree(unittest.TestCase):
    """The fog is measured twice — `_shared.empty_section` here and
    `intake.fog_items` in the other vertical — and the two constants are
    still duplicated (debt of the V4, SYM-13). While two readings exist
    they have to give the same answer on the same body, or `validate` and
    `brief --map` disagree about whether a map is finished. They did
    disagree once, on exactly the third case below, and each side was
    green on its own: the twins were never run against one another until
    both were already merged."""

    CASES = (
        ('<!-- see "Fog of war": ... -->', True),
        ("- como paginar\n- como versionar", False),
        ("which door do we take", False),
        ("", True),
        ("<!-- one\ntwo -->", True),
        ('<!-- hint -->\nwhich door', False),
    )

    def test_both_halves_read_the_same_fog_the_same_way(self):
        for content, expected_empty in self.CASES:
            body = (
                f"## Destination\nship it\n\n"
                f"## Not yet specified\n{content}\n\n"
                f"## Out of scope\n"
            )
            with self.subTest(content=content):
                self.assertEqual(S.empty_section(body, S.FOG), expected_empty)
                self.assertEqual(
                    not INTAKE.fog_items(body), expected_empty
                )


class IndexLine(unittest.TestCase):
    def test_a_line_round_trips_through_its_key(self):
        line = S.index_line("SYM-12", "V3", "https://x/SYM-12", "brief reads the map")
        self.assertIn("SYM-12", S.index_keys(line))

    def test_a_hand_typed_line_still_names_its_key(self):
        # `## Out of scope` is written by a human or by the skill, not by
        # this tool — the lint must recognise a key there in any shape.
        self.assertEqual(S.index_keys("- SYM-40: dropped, wrong destination"), {"SYM-40"})

    def test_appending_keeps_what_was_there(self):
        first = S.index_line("SYM-1", "a", "u", "g")
        second = S.index_line("SYM-2", "b", "u", "g")
        self.assertEqual(S.append_index("", first), first)
        self.assertEqual(S.append_index(first, second), f"{first}\n{second}")


class ResolutionComment(unittest.TestCase):
    def test_the_answer_is_the_first_line_after_the_heading(self):
        body = S.resolution_comment("Yes, three doors.\n\nBecause the intake forks.")
        self.assertTrue(body.startswith(S.RESOLUTION_HEADING))
        self.assertEqual(S.resolution_answer(body), "Yes, three doors.")

    def test_a_comment_without_the_heading_is_not_a_resolution(self):
        self.assertIsNone(S.resolution_answer("Yes, three doors."))


class Ordering(unittest.TestCase):
    def test_priority_first_then_creation_order(self):
        children = [
            child("SYM-1"), child("SYM-2", priority="low"),
            child("SYM-3", priority="high"), child("SYM-4"),
            child("SYM-5", priority="high"),
        ]
        self.assertEqual(
            [c.key for c in S.order_frontier(children)],
            ["SYM-3", "SYM-5", "SYM-2", "SYM-1", "SYM-4"],
        )

    def test_a_blocker_outside_the_map_still_blocks(self):
        children = [child("SYM-1", blocked_by=("OTHER-9",))]
        self.assertEqual(S.open_blockers(children[0], children), ["OTHER-9"])
        self.assertEqual(S.external_blockers(children[0], children), ["OTHER-9"])
        self.assertEqual(S.takeable(children), [])
        self.assertEqual(
            S.blocking_phrase(children[0], children),
            "OTHER-9 (external to this map, so its state is unknown here)")
        self.assertIn("external to this map", S.describe(children[0], children))

    def test_the_blocking_phrase_marks_only_the_blockers_it_cannot_see(self):
        # One phrase for `claim`, `resolve` and `describe`. It lived in
        # three copies, and the third had already drifted to a different
        # wording — this asserts the one wording all three now say.
        children = [child("SYM-1", blocked_by=("SYM-2", "OTHER-9")), child("SYM-2")]
        self.assertEqual(
            S.blocking_phrase(children[0], children),
            "SYM-2, OTHER-9 (external to this map, so its state is unknown here)")

    def test_the_blocking_phrase_is_empty_when_nothing_blocks(self):
        children = [child("SYM-1", blocked_by=("SYM-2",)),
                    child("SYM-2", state="Done", state_type="completed")]
        self.assertEqual(S.blocking_phrase(children[0], children), "")

    def test_the_frontier_excludes_claimed_closed_and_blocked(self):
        children = [
            child("SYM-1"),
            child("SYM-2", assignee="Ana"),
            child("SYM-3", state="Done", state_type="completed"),
            child("SYM-4", blocked_by=("SYM-1",)),
            child("SYM-5", blocked_by=("SYM-3",)),
        ]
        self.assertEqual([c.key for c in S.takeable(children)], ["SYM-1", "SYM-5"])
        self.assertEqual(
            [c.key for c in S.held_back(children)], ["SYM-2", "SYM-3", "SYM-4"])


class LowResolution(unittest.TestCase):
    def test_it_carries_the_sections_and_the_frontier_count(self):
        text = S.low_res(item(body=Sections.BODY), [child("SYM-1"), child("SYM-2", assignee="A")])
        self.assertIn("SYM-8", text)
        for heading in S.SECTIONS:
            self.assertIn(f"## {heading}", text)
        self.assertIn("1 takeable now", text)

    def test_a_long_section_is_cut_and_says_so(self):
        body = "## Destination\n" + "\n".join(f"line {i}" for i in range(40)) + "\n"
        text = S.low_res(item(body=body), [])
        self.assertIn("more line(s) in the map itself", text)

    def test_a_missing_section_is_named_not_silent(self):
        text = S.low_res(item(body="## Destination\nx\n"), [])
        self.assertIn("section missing from the map body", text)


class Optionals(unittest.TestCase):
    def test_a_valueless_optional_is_refused_not_read_as_true(self):
        with self.assertRaises(INJECTION.Refused) as caught:
            S.optional({"gist": True}, "gist", example="map resolve --gist x")
        self.assertEqual(caught.exception.refusal.kind, INJECTION.INCOMPLETE)

    def test_an_absent_optional_is_none(self):
        self.assertIsNone(S.optional({}, "gist", example="x"))

    def test_keys_splits_and_drops_the_empties(self):
        self.assertEqual(S.keys("SYM-1, SYM-2 ,"), ["SYM-1", "SYM-2"])


# --- new --------------------------------------------------------------------




# --- ticket -----------------------------------------------------------------








if __name__ == "__main__":
    unittest.main()
