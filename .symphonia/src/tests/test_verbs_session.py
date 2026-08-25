"""Tests for the three verbs of a working session — `frontier`, `claim`,
`resolve`.

Offline, tracker injected, refusals asserted on `kind` and on a field of
`injection.as_dict`. The fakes and helpers come from `test_verbs_shared`,
which is where they were first needed.

    cd .symphonia/src && python3 -m unittest tests.test_verbs_session
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import map as MAP
from tests.test_verbs_shared import FakeTracker, call, child, item, refusal_of
from verbs import _shared as S
from verbs import claim as CLAIM
from verbs import frontier as FRONTIER
from verbs import resolve as RESOLVE

MAP_BODY = (
    "## Destination\nship intake v2\n\n"
    "## Notes\nbrownfield\n\n"
    "## Decisions so far\n\n"
    "## Not yet specified\nwhich door\n\n"
    "## Out of scope\n- [SYM-40 — old idea](https://x/SYM-40) — dropped\n"
)


def session(children, body=MAP_BODY, ticket_body="## Question\nwhich headings?"):
    """A tracker canned for a whole session: the map, its children, and a
    ticket body. `get_item` answers for whichever key it is asked."""

    fake = FakeTracker(children=children)
    bodies = {"SYM-8": item("SYM-8", "Intake v2", body)}

    def get_item(id):
        fake.calls.append(("get_item", id))
        return bodies.get(id, item(id, f"title of {id}", ticket_body))

    fake.get_item = get_item
    return fake


# --- frontier ---------------------------------------------------------------


class Frontier(unittest.TestCase):
    def test_priority_orders_it_and_creation_order_breaks_the_tie(self):
        children = [
            child("SYM-1", title="first"),
            child("SYM-2", title="low", priority="low"),
            child("SYM-3", title="high", priority="high"),
            child("SYM-4", title="second"),
        ]
        result = call(FRONTIER, ["frontier", "--map", "SYM-8"], FakeTracker(children=children))
        self.assertEqual(result.code, 0)
        listed = [line for line in result.out.splitlines() if line.startswith("- SYM-")]
        self.assertEqual(
            [line.split(" ")[1] for line in listed],
            ["SYM-3", "SYM-2", "SYM-1", "SYM-4"],
        )

    def test_it_only_reads(self):
        result = call(FRONTIER, ["frontier", "--map", "SYM-8"],
                      FakeTracker(children=[child("SYM-1")]))
        self.assertEqual(result.tracker.calls, [("list_children", "SYM-8")])

    def test_an_empty_frontier_is_said_not_left_silent(self):
        children = [child("SYM-1", assignee="Ana")]
        result = call(FRONTIER, ["frontier", "--map", "SYM-8"], FakeTracker(children=children))
        self.assertIn("Takeable now: none", result.out)
        self.assertIn("claimed by Ana", result.out)

    def test_a_map_with_no_tickets_says_that_instead(self):
        result = call(FRONTIER, ["frontier", "--map", "SYM-8"], FakeTracker(children=[]))
        self.assertIn("no tickets yet", result.out)

    def test_held_back_lines_say_what_holds_them(self):
        children = [
            child("SYM-1", state="Done", state_type="completed"),
            child("SYM-2", blocked_by=("SYM-3",)),
            child("SYM-3"),
        ]
        result = call(FRONTIER, ["frontier", "--map", "SYM-8"], FakeTracker(children=children))
        self.assertIn("closed (Done)", result.out)
        self.assertIn("blocked by SYM-3", result.out)


# --- claim ------------------------------------------------------------------


class Claim(unittest.TestCase):
    def test_one_call_returns_the_ticket_and_the_map(self):
        # The success criterion: the body of the ticket *and* the map in low
        # resolution, out of a single `map claim`.
        fake = session([child("SYM-12", title="Read the skill")])
        result = call(CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12",
                              "--assignee", "ana@example.com"], fake)
        self.assertEqual(result.code, 0)
        self.assertIn("which headings?", result.out)          # the ticket's body
        self.assertIn("## The map, in low resolution", result.out)
        self.assertIn("ship intake v2", result.out)           # the map's destination
        self.assertIn("which door", result.out)               # the map's fog
        self.assertIn("takeable now", result.out.lower())

    def test_it_assigns_before_it_reads(self):
        fake = session([child("SYM-12")])
        call(CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12",
                     "--assignee", "ana"], fake)
        kinds = [c[0] for c in fake.calls]
        self.assertEqual(kinds[0], "list_children")
        self.assertEqual(kinds[1], "assign")
        self.assertEqual(kinds.count("get_item"), 2)

    def test_a_ticket_outside_the_map_is_refused(self):
        refusal, fake = refusal_of(CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-77",
                                           "--assignee", "ana"],
                                   session([child("SYM-12")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("SYM-77", refusal["blocked"])
        self.assertNotIn("assign", [c[0] for c in fake.calls])

    def test_a_closed_ticket_is_refused(self):
        refusal, _ = refusal_of(
            CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12", "--assignee", "ana"],
            session([child("SYM-12", state="Done", state_type="completed")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("Done", refusal["blocked"])

    def test_a_held_ticket_names_who_holds_it(self):
        refusal, _ = refusal_of(
            CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12", "--assignee", "ana"],
            session([child("SYM-12", assignee="Bruno")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("Bruno", refusal["blocked"])

    def test_a_blocked_ticket_is_refused_and_names_the_blockers(self):
        refusal, _ = refusal_of(
            CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12", "--assignee", "ana"],
            session([child("SYM-12", blocked_by=("SYM-11",)), child("SYM-11")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("SYM-11", refusal["blocked"])

    def test_a_blocker_outside_the_map_blocks_the_claim(self):
        # Refusing too much is recoverable; claiming work that is not ready
        # is not. That the refusal calls the blocker external is a property
        # of `_shared.blocking_phrase`, tested there — here the assertion is
        # that an unseeable blocker refuses at all, and names its key.
        target = child("SYM-12", blocked_by=("OTHER-9",))
        refusal, fake = refusal_of(
            CLAIM, ["claim", "--map", "SYM-8", "--ticket", "SYM-12", "--assignee", "ana"],
            session([target]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("OTHER-9", refusal["blocked"])
        self.assertEqual([c[0] for c in fake.calls], ["list_children"])


# --- resolve ----------------------------------------------------------------


class ResolveRefuses(unittest.TestCase):
    ARGV = ["resolve", "--map", "SYM-8", "--ticket", "SYM-12", "--answer", "Three doors."]

    def test_an_unowned_ticket_is_refused(self):
        refusal, fake = refusal_of(RESOLVE, self.ARGV, session([child("SYM-12")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("SYM-12", refusal["blocked"])
        self.assertIn("map claim", refusal["example"])
        self.assertEqual([c[0] for c in fake.calls], ["list_children"])

    def test_a_closed_ticket_is_refused(self):
        refusal, fake = refusal_of(
            RESOLVE, self.ARGV,
            session([child("SYM-12", assignee="Ana", state="Done", state_type="completed")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertNotIn("post_comment", [c[0] for c in fake.calls])

    def test_a_blocked_ticket_is_refused(self):
        refusal, _ = refusal_of(
            RESOLVE, self.ARGV,
            session([child("SYM-12", assignee="Ana", blocked_by=("SYM-11",)),
                     child("SYM-11")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("SYM-11", refusal["blocked"])

    def test_a_ticket_outside_the_map_is_refused(self):
        refusal, _ = refusal_of(
            RESOLVE, ["resolve", "--map", "SYM-8", "--ticket", "SYM-77", "--answer", "x"],
            session([child("SYM-12", assignee="Ana")]))
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("SYM-77", refusal["blocked"])

    def test_an_empty_answer_is_refused_by_the_dispatcher(self):
        result = call(RESOLVE, ["resolve", "--map", "SYM-8", "--ticket", "SYM-12",
                                "--answer", "   "],
                      session([child("SYM-12", assignee="Ana")]))
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertIn("Kind: incomplete", result.err)
        self.assertEqual(result.tracker.calls, [])


class ResolveSucceeds(unittest.TestCase):
    def run_it(self, children, body=MAP_BODY, extra=()):
        fake = session(children, body)
        argv = ["resolve", "--map", "SYM-8", "--ticket", "SYM-12",
                "--answer", "Three doors, one route.\n\nBecause intake forks."]
        return call(RESOLVE, argv + list(extra), fake)

    def test_it_comments_closes_indexes_and_re_reads_in_that_order(self):
        result = self.run_it([child("SYM-12", title="Read the skill", assignee="Ana")])
        self.assertEqual(result.code, 0)
        kinds = [c[0] for c in result.tracker.calls]
        self.assertEqual(
            kinds,
            ["list_children", "post_comment", "close_item", "get_item",
             "patch_body_section", "list_children"],
        )

    def test_the_comment_carries_the_direct_answer_on_its_first_line(self):
        result = self.run_it([child("SYM-12", assignee="Ana")])
        posted = next(c for c in result.tracker.calls if c[0] == "post_comment")[2]
        self.assertEqual(S.resolution_answer(posted), "Three doors, one route.")

    def test_the_index_line_lands_in_the_decisions_section(self):
        result = self.run_it([child("SYM-12", title="Read the skill", assignee="Ana")])
        _, target, heading, content = next(
            c for c in result.tracker.calls if c[0] == "patch_body_section")
        self.assertEqual((target, heading), ("SYM-8", S.DECISIONS))
        self.assertIn("SYM-12", S.index_keys(content))
        self.assertIn("Three doors, one route.", content)

    def test_gist_replaces_the_first_line_of_the_answer_in_the_index(self):
        result = self.run_it([child("SYM-12", assignee="Ana")],
                             extra=("--gist", "three doors"))
        content = next(c for c in result.tracker.calls if c[0] == "patch_body_section")[3]
        self.assertIn("three doors", content)

    def test_an_already_indexed_ticket_is_not_written_twice(self):
        body = MAP_BODY.replace(
            "## Decisions so far\n",
            "## Decisions so far\n- [SYM-12 — Read the skill](https://x/SYM-12) — done\n")
        result = self.run_it([child("SYM-12", assignee="Ana")], body=body)
        self.assertNotIn("patch_body_section", [c[0] for c in result.tracker.calls])
        self.assertIn("Already accounted for", result.out)

    def test_a_ticket_ruled_out_of_scope_counts_as_indexed(self):
        # Leaving scope is an act of scope, not a step on the route: its
        # line lives under `## Out of scope` and must not be copied into
        # the decisions index.
        body = MAP_BODY.replace("SYM-40", "SYM-12")
        result = self.run_it([child("SYM-12", assignee="Ana")], body=body)
        self.assertNotIn("patch_body_section", [c[0] for c in result.tracker.calls])

    def test_it_hands_back_the_frontier_as_it_now_stands(self):
        result = self.run_it([child("SYM-12", assignee="Ana"), child("SYM-13")])
        self.assertIn("Frontier of SYM-8, as it now stands", result.out)
        self.assertIn("SYM-13", result.out)


if __name__ == "__main__":
    unittest.main()
