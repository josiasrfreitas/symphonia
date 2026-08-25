"""Tests for the two verbs that only read — `validate` and `graph`.

Offline, tracker injected. `validate` never refuses: a malformed map is
still a map, so what it produces is a list, and the tests assert on the
list. The fakes come from `test_verbs_shared`.

    cd .symphonia/src && python3 -m unittest tests.test_verbs_readonly
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from tests.test_verbs_shared import FakeTracker, call, child, item
from verbs import _shared as S
from verbs import graph as GRAPH
from verbs import validate as VALIDATE


def body(*, decisions="", fog="", out_of_scope="", drop=()):
    filled = {
        S.DESTINATION: "ship intake v2",
        S.NOTES: "brownfield",
        S.DECISIONS: decisions,
        S.FOG: fog,
        S.OUT_OF_SCOPE: out_of_scope,
    }
    return "\n\n".join(
        f"## {heading}\n{filled[heading]}".rstrip()
        for heading in S.SECTIONS if heading not in drop
    ) + "\n"


def resolved(answer="Three doors."):
    return [SimpleNamespace(id="c", body=S.resolution_comment(answer),
                            author_name="", created_at="")]


def reading(children, map_body, comments=None):
    """A tracker that answers `get_item` for the map and `list_comments`
    per ticket key."""

    fake = FakeTracker(children=children)
    per_key = comments or {}

    def get_item(id):
        fake.calls.append(("get_item", id))
        return item("SYM-8", "Intake v2", map_body)

    def list_comments(id):
        fake.calls.append(("list_comments", id))
        return per_key.get(id, [])

    fake.get_item = get_item
    fake.list_comments = list_comments
    return fake


DONE = dict(state="Done", state_type="completed")


# --- validate: the verdict --------------------------------------------------


class Verdict(unittest.TestCase):
    def test_empty_frontier_and_empty_fog_is_the_end_of_the_map(self):
        children = [child("SYM-12", **DONE)]
        fake = reading(children, body(
            decisions=S.index_line("SYM-12", "t", "https://x/SYM-12", "done")),
            {"SYM-12": resolved()})
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertEqual(result.code, 0)
        self.assertIn("At the destination", result.out)
        self.assertIn("Format: nothing pending.", result.out)

    def test_open_work_off_the_frontier_is_named_not_swallowed(self):
        # `takeable()` excludes the claimed and the blocked, so a map whose
        # every open child has an owner has an empty frontier. The verdict
        # stays the same — the rule is "empty frontier + empty fog", and
        # `brief --map` checks the same one — but the text may not say the
        # route is walked while somebody is still walking it.
        children = [child("SYM-12", assignee="Ana"),
                    child("SYM-13", blocked_by=("OTHER-9",))]
        fake = reading(children, body())
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertIn("At the destination", result.out)
        self.assertNotIn("Every step has been walked", result.out)
        self.assertIn("Still open, off the frontier: 2 ticket(s)", result.out)
        self.assertIn("SYM-12", result.out)
        self.assertIn("SYM-13", result.out)

    def test_a_truly_finished_map_still_says_every_step_was_walked(self):
        fake = reading([child("SYM-12", **DONE)], body(
            decisions=S.index_line("SYM-12", "t", "https://x/SYM-12", "done")),
            {"SYM-12": resolved()})
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertIn("Every step has been walked", result.out)
        self.assertNotIn("Still open, off the frontier", result.out)

    def test_fog_left_is_not_the_end_even_with_an_empty_frontier(self):
        fake = reading([], body(fog="which door"))
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertIn("Not at the destination", result.out)
        self.assertIn(S.FOG, result.out)

    def test_a_live_frontier_is_not_the_end_even_with_empty_fog(self):
        fake = reading([child("SYM-12")], body())
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertIn("Not at the destination", result.out)
        self.assertIn("SYM-12", result.out)

    def test_a_missing_fog_section_is_named_as_unreadable(self):
        fake = reading([], body(drop=(S.FOG,)))
        result = call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertIn("Not at the destination", result.out)
        self.assertIn("cannot be read", result.out)

    def test_it_only_reads(self):
        fake = reading([child("SYM-12", **DONE)], body(), {"SYM-12": resolved()})
        call(VALIDATE, ["validate", "--map", "SYM-8"], fake)
        self.assertEqual({c[0] for c in fake.calls},
                         {"get_item", "list_children", "list_comments"})


# --- validate: the lint -----------------------------------------------------


class Lint(unittest.TestCase):
    def pending(self, children, map_body, comments=None):
        """Only the lint's own list — the lines after the Format heading.
        The verdict above it is bulleted too, and folding the two together
        would let a verdict line pass for a format finding."""

        result = call(VALIDATE, ["validate", "--map", "SYM-8"],
                      reading(children, map_body, comments))
        lines = result.out.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("Format"))
        return [line[2:] for line in lines[start + 1:] if line.startswith("- ")], result.out

    def test_a_closed_ticket_with_no_index_line_is_pending(self):
        pending, _ = self.pending([child("SYM-12", **DONE)], body(),
                                  {"SYM-12": resolved()})
        self.assertTrue(any("SYM-12 is closed but has no line" in p for p in pending))

    def test_a_closed_ticket_ruled_out_of_scope_is_not_pending(self):
        # Leaving scope is an act of scope, not a step on the route: its
        # line lives under `## Out of scope` and marking it pending would
        # be a false finding.
        pending, _ = self.pending(
            [child("SYM-12", **DONE)],
            body(out_of_scope="- SYM-12: dropped, wrong destination"),
            {"SYM-12": resolved()})
        self.assertFalse(any("has no line" in p for p in pending), pending)

    def test_a_ticket_ruled_out_of_scope_owes_no_resolution_comment(self):
        # The same exemption, on the other closed-ticket check. Leaving
        # scope produces no resolution to comment, so demanding one is the
        # same false pending as demanding the index line.
        pending, _ = self.pending(
            [child("SYM-12", **DONE)],
            body(out_of_scope="- SYM-12: dropped, wrong destination"),
            {"SYM-12": []})
        self.assertEqual(pending, [])

    def test_an_index_line_for_a_stranger_is_pending(self):
        pending, _ = self.pending(
            [], body(decisions=S.index_line("SYM-77", "t", "https://x/SYM-77", "g")))
        self.assertTrue(any("SYM-77" in p and "not a ticket of this map" in p
                            for p in pending))

    def test_a_closed_ticket_with_no_resolution_comment_is_pending(self):
        pending, _ = self.pending(
            [child("SYM-12", **DONE)],
            body(decisions=S.index_line("SYM-12", "t", "https://x/SYM-12", "g")),
            {"SYM-12": []})
        self.assertTrue(any("heading" in p for p in pending), pending)

    def test_a_comment_with_the_wrong_heading_does_not_count(self):
        wrong = [SimpleNamespace(id="c", body="## Resolução\n\nTrês portas.",
                                 author_name="", created_at="")]
        pending, _ = self.pending(
            [child("SYM-12", **DONE)],
            body(decisions=S.index_line("SYM-12", "t", "https://x/SYM-12", "g")),
            {"SYM-12": wrong})
        self.assertTrue(any(S.RESOLUTION_HEADING in p for p in pending), pending)

    def test_a_missing_section_is_pending(self):
        pending, _ = self.pending([], body(drop=(S.NOTES,)))
        self.assertTrue(any(S.NOTES in p and "no" in p for p in pending))

    def test_an_open_ticket_is_never_linted_for_a_resolution(self):
        # An unfinished map is not a malformed one: an open ticket owes
        # neither an index line nor a resolution comment.
        pending, out = self.pending([child("SYM-12")], body(fog="x"))
        self.assertEqual(pending, [])
        self.assertIn("Not at the destination", out)

    def test_a_malformed_map_is_described_not_refused(self):
        result = call(VALIDATE, ["validate", "--map", "SYM-8"],
                      reading([child("SYM-12", **DONE)], "no sections at all\n",
                              {"SYM-12": []}))
        self.assertEqual(result.code, 0)
        self.assertIn("Format (", result.out)


# --- graph ------------------------------------------------------------------


class Graph(unittest.TestCase):
    def test_it_emits_a_mermaid_flowchart_rooted_at_the_map(self):
        result = call(GRAPH, ["graph", "--map", "SYM-8"],
                      FakeTracker(children=[child("SYM-12", title="Read the skill")]))
        self.assertEqual(result.code, 0)
        self.assertIn("```mermaid", result.out)
        self.assertIn("flowchart TD", result.out)
        self.assertIn("SYM_8 --- SYM_12", result.out)
        self.assertIn("Read the skill", result.out)

    def test_a_blocking_edge_points_from_the_blocker(self):
        children = [child("SYM-11"), child("SYM-12", blocked_by=("SYM-11",))]
        result = call(GRAPH, ["graph", "--map", "SYM-8"], FakeTracker(children=children))
        self.assertIn("SYM_11 --> SYM_12", result.out)

    def test_a_class_per_state(self):
        children = [
            child("SYM-1", **DONE),
            child("SYM-2"),
            child("SYM-3", assignee="Ana"),
            child("SYM-4", blocked_by=("SYM-2",)),
        ]
        result = call(GRAPH, ["graph", "--map", "SYM-8"], FakeTracker(children=children))
        for key, name in (("SYM_1", "closed"), ("SYM_2", "ready"),
                          ("SYM_3", "claimed"), ("SYM_4", "blocked")):
            self.assertIn(f"class {key} {name}", result.out)

    def test_the_root_is_not_painted_as_a_claimed_ticket(self):
        # The map is not a ticket, so it wears neither `claimed` nor any
        # other ticket state.
        result = call(GRAPH, ["graph", "--map", "SYM-8"],
                      FakeTracker(children=[child("SYM-12")]))
        self.assertIn("class SYM_8 root", result.out)

    def test_an_external_blocker_gets_its_own_marked_node(self):
        children = [child("SYM-12", blocked_by=("OTHER-9",))]
        result = call(GRAPH, ["graph", "--map", "SYM-8"], FakeTracker(children=children))
        self.assertIn("external to this map", result.out)
        self.assertIn("class OTHER_9 external", result.out)
        self.assertIn("OTHER_9 --> SYM_12", result.out)

    def test_a_quote_in_a_title_does_not_break_the_label(self):
        result = call(GRAPH, ["graph", "--map", "SYM-8"],
                      FakeTracker(children=[child("SYM-12", title='the "map" tool')]))
        label = next(line for line in result.out.splitlines() if "SYM_12[" in line)
        self.assertEqual(label.count('"'), 2)

    def test_an_empty_map_says_so_below_the_diagram(self):
        result = call(GRAPH, ["graph", "--map", "SYM-8"], FakeTracker(children=[]))
        self.assertIn("no tickets yet", result.out)

    def test_it_only_reads(self):
        result = call(GRAPH, ["graph", "--map", "SYM-8"],
                      FakeTracker(children=[child("SYM-12")]))
        self.assertEqual(result.tracker.calls, [("list_children", "SYM-8")])


if __name__ == "__main__":
    unittest.main()
