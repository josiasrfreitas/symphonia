"""Tests for `verbs/_shared.py` and for the two verbs that write cards,
`new` and `ticket`.

Offline, like every test in this package (ADR-0002): the tracker is a fake
injected through `map.main(..., tracker=...)`, so nothing here needs
`LINEAR_API_KEY` and nothing here reaches Linear. Each refusal is asserted
on its `kind` and on a field of `injection.as_dict` — never on a substring
of prose, which is the part a rewrite is allowed to change.

    cd .symphonia/src && python3 -m unittest tests.test_verbs_shared
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import map as MAP
from linear import Child, Item, ItemRef
from verbs import _shared as S
from verbs import new as NEW
from verbs import ticket as TICKET


class FakeTracker:
    """Records every call and answers with what the test canned."""

    def __init__(self, *, item=None, children=(), comments=()):
        self.calls: list[tuple] = []
        self.item = item
        self.children = list(children)
        self.comments = list(comments)
        self.created = ItemRef(id="uuid-new", key="SYM-99", url="https://x/SYM-99")

    def create_item(self, title, body, *, parent=None, labels=(), team=None):
        self.calls.append(("create_item", title, body, parent, tuple(labels), team))
        return self.created

    def add_blocker(self, id, blocked_by):
        self.calls.append(("add_blocker", id, blocked_by))

    def set_priority(self, id, level, *, user_requested):
        self.calls.append(("set_priority", id, level, user_requested))

    def get_item(self, id):
        self.calls.append(("get_item", id))
        return self.item

    def list_children(self, id):
        self.calls.append(("list_children", id))
        return self.children

    def list_comments(self, id):
        self.calls.append(("list_comments", id))
        return self.comments

    def assign(self, id, assignee):
        self.calls.append(("assign", id, assignee))

    def close_item(self, id):
        self.calls.append(("close_item", id))

    def post_comment(self, id, body):
        self.calls.append(("post_comment", id, body))
        return SimpleNamespace(id="c1", body=body, author_name="", created_at="")

    def patch_body_section(self, id, heading, content):
        self.calls.append(("patch_body_section", id, heading, content))


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


def call(module, argv, tracker=None):
    """Run one verb through the real dispatcher, capturing what a caller
    would see. Going through `map.main` is the point: `required` is checked
    by `map.check`, so a test that called `run` directly would be testing a
    path no user takes."""

    fake = tracker or FakeTracker()
    registry = {module.SPEC["name"]: module}
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = MAP.main(argv, registry=registry, tracker=lambda: fake)
    return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue(), tracker=fake)


def refusal_of(module, argv, tracker=None):
    """The `Refusal` a call raises, as the dict `injection.as_dict` makes —
    so a test asserts on `kind` and on a named field, not on prose."""

    fake = tracker or FakeTracker()
    verb, params = MAP.parse(argv)
    MAP.check(module.SPEC, params)
    try:
        module.run(params, tracker=lambda: fake)
    except INJECTION.Refused as refused:
        return INJECTION.as_dict(refused.refusal), fake
    raise AssertionError(f"{argv} was not refused")


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

    def test_a_new_body_has_all_five_sections_three_of_them_empty(self):
        body = S.blank_map_body("ship it", "brownfield")
        for heading in S.SECTIONS:
            self.assertIsNotNone(S.section(body, heading), heading)
        self.assertEqual(S.section(body, S.DESTINATION), "ship it")
        for heading in (S.DECISIONS, S.FOG, S.OUT_OF_SCOPE):
            self.assertTrue(S.empty_section(body, heading), heading)


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
        self.assertIn("external to this map", S.describe(children[0], children))

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


class New(unittest.TestCase):
    def test_it_creates_a_labelled_map_with_the_five_sections(self):
        result = call(NEW, ["new", "--title", "Intake v2",
                            "--destination", "three doors", "--team", "SYM"])
        self.assertEqual(result.code, 0)
        (_, title, body, parent, labels, team), = result.tracker.calls
        self.assertEqual((title, parent, labels, team),
                         ("Intake v2", None, (S.MAP_LABEL,), "SYM"))
        self.assertEqual(S.section(body, S.DESTINATION), "three doors")
        self.assertIn("SYM-99", result.out)

    def test_notes_land_in_their_own_section(self):
        result = call(NEW, ["new", "--title", "t", "--destination", "d",
                            "--team", "SYM", "--notes", "brownfield"])
        body = result.tracker.calls[0][2]
        self.assertEqual(S.section(body, S.NOTES), "brownfield")

    def test_the_team_is_required_by_the_dispatcher(self):
        result = call(NEW, ["new", "--title", "t", "--destination", "d"])
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertIn("Kind: incomplete", result.err)


# --- ticket -----------------------------------------------------------------


class TicketCreate(unittest.TestCase):
    def test_it_creates_under_the_map_with_the_type_label(self):
        result = call(TICKET, ["ticket", "--map", "SYM-8", "--title", "Read the skill",
                               "--question", "which headings?", "--type", "research"])
        self.assertEqual(result.code, 0)
        kind, title, body, parent, labels, team = result.tracker.calls[0]
        self.assertEqual((kind, title, parent, labels),
                         ("create_item", "Read the skill", "SYM-8", ("wayfinder:research",)))
        self.assertIn("which headings?", body)

    def test_blockers_are_linked_one_call_each(self):
        result = call(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                               "--question", "q", "--type", "task",
                               "--blocked-by", "SYM-1,SYM-2"])
        self.assertEqual(
            [c for c in result.tracker.calls if c[0] == "add_blocker"],
            [("add_blocker", "SYM-99", "SYM-1"), ("add_blocker", "SYM-99", "SYM-2")],
        )

    def test_an_unknown_type_is_refused(self):
        refusal, fake = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                                            "--question", "q", "--type", "vibes"])
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("vibes", refusal["blocked"])
        self.assertEqual(fake.calls, [])

    def test_neither_form_complete_is_incomplete(self):
        refusal, _ = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--title", "t"])
        self.assertEqual(refusal["kind"], INJECTION.INCOMPLETE)
        self.assertIn("--question", refusal["blocked"])
        self.assertIn("--key", refusal["blocked"])


class TicketPriority(unittest.TestCase):
    """The success criterion: priority without `--user-requested` is
    refused, in the one format, before the tracker is touched."""

    def test_priority_without_user_requested_is_refused(self):
        refusal, fake = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                                            "--question", "q", "--type", "task",
                                            "--priority", "high"])
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("--user-requested", refusal["accepted"])
        self.assertIn("--user-requested", refusal["example"])

    def test_nothing_is_created_by_a_refused_call(self):
        _, fake = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                                      "--question", "q", "--type", "task",
                                      "--priority", "high"])
        self.assertEqual(fake.calls, [])

    def test_the_refusal_renders_in_the_one_format(self):
        result = call(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                               "--question", "q", "--type", "task", "--priority", "high"])
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertEqual(
            [line.split(":")[0] for line in result.err.strip().splitlines()
             if line.split(":")[0] in ("Blocked", "Accepted", "Example", "Kind")],
            ["Blocked", "Accepted", "Example", "Kind"],
        )

    def test_with_user_requested_it_reaches_the_tracker(self):
        result = call(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                               "--question", "q", "--type", "task",
                               "--priority", "high", "--user-requested"])
        self.assertEqual(result.code, 0)
        self.assertIn(("set_priority", "SYM-99", "high", True), result.tracker.calls)

    def test_an_unknown_level_is_refused_before_the_tracker(self):
        refusal, fake = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--title", "t",
                                            "--question", "q", "--type", "task",
                                            "--priority", "urgent", "--user-requested"])
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertEqual(fake.calls, [])


class TicketRewire(unittest.TestCase):
    def test_it_links_without_creating(self):
        result = call(TICKET, ["ticket", "--map", "SYM-8", "--key", "SYM-12",
                               "--blocked-by", "SYM-11"])
        self.assertEqual(result.code, 0)
        self.assertEqual(result.tracker.calls, [("add_blocker", "SYM-12", "SYM-11")])

    def test_both_forms_at_once_is_refused(self):
        refusal, fake = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--key", "SYM-12",
                                            "--title", "t"])
        self.assertEqual(refusal["kind"], INJECTION.REFUSED)
        self.assertIn("--title", refusal["blocked"])
        self.assertEqual(fake.calls, [])

    def test_a_key_with_nothing_to_change_is_incomplete(self):
        refusal, _ = refusal_of(TICKET, ["ticket", "--map", "SYM-8", "--key", "SYM-12"])
        self.assertEqual(refusal["kind"], INJECTION.INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
