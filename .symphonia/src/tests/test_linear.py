"""Tests for `linear` — the pure body-patcher and the twelve tracker
operations, driven through a fake `LinearClient` that returns canned
answers and records every call. No network, no `LINEAR_API_KEY`. Run
either way:

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

ISSUE = {
    "id": "uuid-1", "identifier": "SYM-8", "url": "https://linear.app/x/SYM-8",
    "title": "the map", "description": "## Index\nold\n", "team": {"id": "team-1"},
}


class FakeClient:
    """A queue of canned `data` objects, and the log of what was asked.

    Each entry is either a dict (returned as-is) or a callable taking
    `(gql, variables)` — used where one operation issues several queries
    and the answer depends on which."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def query(self, gql: str, variables: dict | None = None) -> dict:
        self.calls.append((gql, variables or {}))
        if not self.answers:
            raise AssertionError(f"no canned answer left for query: {gql.strip()[:80]}")
        answer = self.answers.pop(0)
        return answer(gql, variables or {}) if callable(answer) else answer

    def mutations(self) -> list[tuple[str, dict]]:
        return [call for call in self.calls if call[0].lstrip().startswith("mutation")]


def tracker(*answers) -> tuple[LINEAR.LinearTracker, FakeClient]:
    client = FakeClient(*answers)
    return LINEAR.LinearTracker(client=client, config=dict(CONFIG)), client


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


# --- creating ---------------------------------------------------------------


class CreateItem(unittest.TestCase):
    CREATED = {"issueCreate": {"issue": {
        "id": "uuid-2", "identifier": "SYM-9", "url": "https://linear.app/x/SYM-9"}}}

    def test_inherits_the_team_from_the_parent(self):
        tr, client = tracker({"issue": ISSUE}, self.CREATED)
        ref = tr.create_item("child", "body", parent="SYM-8")
        self.assertEqual((ref.key, ref.url), ("SYM-9", "https://linear.app/x/SYM-9"))
        payload = client.mutations()[0][1]["input"]
        self.assertEqual(payload["teamId"], "team-1")
        self.assertEqual(payload["parentId"], "uuid-1")

    def test_refuses_without_parent_or_team(self):
        tr, _ = tracker()
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.create_item("orphan", "body")
        self.assertIn("team", str(caught.exception))

    def test_labels_become_label_ids(self):
        tr, client = tracker(
            {"issueLabels": {"nodes": [{"id": "label-1"}]}}, self.CREATED,
        )
        tr.create_item("mapa", "body", labels=("wayfinder:map",), team="team-1")
        self.assertEqual(client.mutations()[0][1]["input"]["labelIds"], ["label-1"])


class AddBlocker(unittest.TestCase):
    def test_the_blocker_is_the_issue_and_the_blocked_is_the_related(self):
        blocked = dict(ISSUE, id="uuid-blocked", identifier="SYM-20")
        blocker = dict(ISSUE, id="uuid-blocker", identifier="SYM-21")
        tr, client = tracker(
            {"issue": blocked}, {"issue": blocker},
            {"issueRelationCreate": {"success": True}},
        )
        tr.add_blocker("SYM-20", "SYM-21")
        payload = client.mutations()[0][1]["input"]
        self.assertEqual(payload["type"], "blocks")
        self.assertEqual(payload["issueId"], "uuid-blocker")
        self.assertEqual(payload["relatedIssueId"], "uuid-blocked")


# --- assigning and closing --------------------------------------------------


class Assign(unittest.TestCase):
    def test_assigns_the_single_match(self):
        tr, client = tracker(
            {"issue": ISSUE},
            {"users": {"nodes": [{"id": "user-1", "name": "Ana", "email": "a@x"}]}},
            {"issueUpdate": {"success": True}},
        )
        tr.assign("SYM-8", "Ana")
        self.assertEqual(client.mutations()[0][1]["input"], {"assigneeId": "user-1"})

    def test_refuses_an_unknown_user(self):
        tr, _ = tracker({"issue": ISSUE}, {"users": {"nodes": []}})
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.assign("SYM-8", "ghost")
        self.assertIn("ghost", str(caught.exception))

    def test_refuses_an_ambiguous_user_instead_of_picking(self):
        tr, client = tracker(
            {"issue": ISSUE},
            {"users": {"nodes": [
                {"id": "user-1", "name": "Ana", "email": "ana@x"},
                {"id": "user-2", "name": "Ana", "email": "ana2@x"},
            ]}},
        )
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.assign("SYM-8", "Ana")
        self.assertIn("more than one user", str(caught.exception))
        self.assertEqual(client.mutations(), [])


class CloseItem(unittest.TestCase):
    def test_uses_the_first_completed_state_by_position(self):
        tr, client = tracker(
            {"issue": ISSUE},
            {"workflowStates": {"nodes": [
                {"id": "state-shipped", "name": "Shipped", "position": 2},
                {"id": "state-done", "name": "Done", "position": 1},
            ]}},
            {"issueUpdate": {"success": True}},
        )
        tr.close_item("SYM-8")
        self.assertEqual(client.mutations()[0][1]["input"], {"stateId": "state-done"})

    def test_refuses_a_team_with_no_completed_state(self):
        tr, _ = tracker({"issue": ISSUE}, {"workflowStates": {"nodes": []}})
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.close_item("SYM-8")
        self.assertIn("completed", str(caught.exception))


class PatchBodySection(unittest.TestCase):
    def test_writes_back_the_patched_body(self):
        tr, client = tracker({"issue": ISSUE}, {"issueUpdate": {"success": True}})
        tr.patch_body_section("SYM-8", "Index", "SYM-9 — resolved")
        body = client.mutations()[0][1]["input"]["description"]
        self.assertIn("## Index\nSYM-9 — resolved", body)
        self.assertNotIn("old", body)


# --- listing ----------------------------------------------------------------


def child_node(**over):
    node = {
        "identifier": "SYM-9", "url": "https://linear.app/x/SYM-9", "title": "decide",
        "priority": 3, "state": {"name": "Todo", "type": "unstarted"},
        "assignee": None, "inverseRelations": {"nodes": []},
    }
    node.update(over)
    return node


class ListChildren(unittest.TestCase):
    def _answers(self, nodes, has_next=False):
        return (
            {"issue": ISSUE},
            {"issue": {"children": {"pageInfo": {"hasNextPage": has_next}, "nodes": nodes}}},
        )

    def test_maps_every_field_the_frontier_needs(self):
        node = child_node(
            assignee={"name": "Ana"},
            priority=2,
            inverseRelations={"nodes": [
                {"type": "blocks", "issue": {"identifier": "SYM-7"}},
                {"type": "duplicate", "issue": {"identifier": "SYM-6"}},
            ]},
        )
        tr, _ = tracker(*self._answers([node]))
        (child,) = tr.list_children("SYM-8")
        self.assertEqual(child.key, "SYM-9")
        self.assertEqual(child.state_type, "unstarted")
        self.assertEqual(child.assignee, "Ana")
        self.assertEqual(child.priority, "high")
        # Only `blocks` relations count as blockers.
        self.assertEqual(child.blocked_by, ("SYM-7",))

    def test_unclaimed_and_unprioritized_read_as_empty_strings(self):
        tr, _ = tracker(*self._answers([child_node(priority=0)]))
        (child,) = tr.list_children("SYM-8")
        self.assertEqual((child.assignee, child.priority), ("", ""))

    def test_a_float_priority_still_maps(self):
        tr, _ = tracker(*self._answers([child_node(priority=4.0)]))
        (child,) = tr.list_children("SYM-8")
        self.assertEqual(child.priority, "low")

    def test_an_overflowing_page_raises_instead_of_truncating(self):
        tr, _ = tracker(*self._answers([child_node()], has_next=True))
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.list_children("SYM-8")
        self.assertIn("refusing to truncate silently", str(caught.exception))


# --- priority ---------------------------------------------------------------


class SetPriority(unittest.TestCase):
    def test_maps_the_three_neutral_names(self):
        for level, number in (("high", 2), ("medium", 3), ("low", 4)):
            with self.subTest(level=level):
                tr, client = tracker({"issue": ISSUE}, {"issueUpdate": {"success": True}})
                tr.set_priority("SYM-8", level, user_requested=True)
                self.assertEqual(client.mutations()[0][1]["input"], {"priority": number})

    def test_refuses_a_level_outside_the_vocabulary(self):
        tr, client = tracker()
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.set_priority("SYM-8", "urgent", user_requested=True)
        self.assertIn("high", str(caught.exception))
        self.assertEqual(client.calls, [])

    def test_refuses_without_the_user_flag(self):
        tr, client = tracker()
        with self.assertRaises(LINEAR.LinearError) as caught:
            tr.set_priority("SYM-8", "high", user_requested=False)
        self.assertIn("user_requested", str(caught.exception))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
