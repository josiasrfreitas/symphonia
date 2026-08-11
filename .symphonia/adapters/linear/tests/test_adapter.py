"""Offline conformance tests for the Linear Tracker Adapter (GRE-174).

TLDR: a fake in-memory provider behind the ``LinearClient.query()`` surface,
reproducing the hostile parts of the real one — last write wins silently, no
compare-and-swap, page caps — so every contract behavior is verifiable from
the branch without a Linear API key. Run either way:

    cd .symphonia && python3 -m unittest adapters.linear.tests.test_adapter
    python3 .symphonia/adapters/linear/tests/test_adapter.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.attention import Attention, AttentionCode
from adapters.linear.adapter import LinearTracker, PatchError, _apply_ops
from adapters.linear.client import LinearClient, LinearError
from adapters.tracker_adapter import (
    Artifact,
    BodyOp,
    ClaimResult,
    CloseOutcome,
    DecisionType,
    DeliveryPhase,
    ItemKind,
    ItemRef,
    Openness,
)

STATES = {
    "Briefed": ("s-briefed", "unstarted"),
    "Planning": ("s-planning", "started"),
    "Implementing": ("s-implementing", "started"),
    "In Review": ("s-inreview", "started"),
    "Awaiting Merge": ("s-awaiting", "started"),
    "Done": ("s-done", "completed"),
    "Canceled": ("s-canceled", "canceled"),
    "Backlog": ("s-backlog", "backlog"),
}
STATE_BY_ID = {sid: name for name, (sid, _) in STATES.items()}


class FakeProvider:
    """What Linear actually is to the adapter: rows, last write wins."""

    def __init__(self):
        self.seq = 0
        self.issues: dict[str, dict] = {}
        self.labels: dict[tuple[str, str], str] = {}
        self.relations: dict[str, tuple[str, str]] = {}
        self.comments: list[dict] = []
        self.attachments: list[dict] = []
        self.documents: dict[str, str] = {}  # resolvable id -> content
        self.on_assignee_write = None  # racer landing right after a write
        self.force_has_next_page = False

    def new_issue(self, title, body="", state="Backlog", labels=(), parent=None):
        self.seq += 1
        uid = f"uuid-{self.seq}"
        self.issues[uid] = {
            "id": uid, "identifier": f"SYM-{self.seq}",
            "url": f"https://linear.app/x/issue/SYM-{self.seq}/t",
            "title": title, "description": body, "assignee": None,
            "state": state, "team": "team-sym", "labels": list(labels),
            "parent": parent,
        }
        return uid

    def resolve(self, ref):
        if ref in self.issues:
            return self.issues[ref]
        return next((r for r in self.issues.values() if r["identifier"] == ref), None)


class FakeClient:
    """Dispatches the adapter's GraphQL strings against the fake provider."""

    def __init__(self, db: FakeProvider):
        self.db = db

    def _page(self, nodes):
        return {"pageInfo": {"hasNextPage": self.db.force_has_next_page}, "nodes": nodes}

    def query(self, gql, variables=None):
        db, v = self.db, variables or {}
        if "issue(id: $id)" in gql and not any(
            k in gql for k in ("comments(", "attachments(", "document(")
        ):
            row = db.resolve(v["id"])
            return {"issue": self._node(row, "inverseRelations" in gql) if row else None}
        if "issues(first: 250" in gql:
            nodes = [r for r in db.issues.values() if r["parent"] == v["parent"]]
            if "$label" in gql:
                nodes = [r for r in nodes if v["label"] in r["labels"]]
            return {"issues": self._page([self._node(r, "inverseRelations" in gql) for r in nodes])}
        if "comments(first: 100)" in gql:
            return {"issue": {"comments": self._page([c for c in db.comments if c["issue"] == v["id"]])}}
        if "attachments(first: 100)" in gql:
            return {"issue": {"attachments": self._page([a for a in db.attachments if a["issue"] == v["id"]])}}
        if "team(id: $id) { states" in gql:
            return {"team": {"states": {"nodes": [
                {"id": sid, "name": name} for name, (sid, _) in STATES.items()]}}}
        if "issueLabels(filter" in gql:
            key = (v["team"], v["name"])
            return {"issueLabels": {"nodes": [{"id": db.labels[key]}] if key in db.labels else []}}
        if "issueLabelCreate" in gql:
            key = (v["input"]["teamId"], v["input"]["name"])
            db.labels[key] = f"label-{len(db.labels) + 1}"
            return {"issueLabelCreate": {"issueLabel": {"id": db.labels[key]}}}
        if "issueUpdate" in gql:
            row, inp = db.issues[v["id"]], v["input"]
            if "description" in inp:
                row["description"] = inp["description"]
            if "assigneeId" in inp:
                row["assignee"] = inp["assigneeId"]
                if db.on_assignee_write:
                    hook, db.on_assignee_write = db.on_assignee_write, None
                    hook()
            if "stateId" in inp:
                row["state"] = STATE_BY_ID[inp["stateId"]]
            return {"issueUpdate": {"success": True}}
        if "issueCreate" in gql:
            inp = v["input"]
            names = [n for (_, n), lid in db.labels.items() if lid in inp.get("labelIds", [])]
            uid = db.new_issue(inp["title"], inp.get("description", ""), "Backlog",
                               names, inp.get("parentId"))
            row = db.issues[uid]
            return {"issueCreate": {"issue": {
                "id": uid, "identifier": row["identifier"], "url": row["url"]}}}
        if "issueRelationCreate" in gql:
            inp = v["input"]
            rid = f"rel-{len(db.relations) + 1}"
            db.relations[rid] = (inp["issueId"], inp["relatedIssueId"])
            return {"issueRelationCreate": {"success": True}}
        if "issueRelationDelete" in gql:
            db.relations.pop(v["id"])
            return {"issueRelationDelete": {"success": True}}
        if "issueAddLabel" in gql or "issueRemoveLabel" in gql:
            row = db.issues[v["id"]]
            name = next(n for (_, n), lid in db.labels.items() if lid == v["label"])
            if "issueAddLabel" in gql:
                if name not in row["labels"]:
                    row["labels"].append(name)
                return {"issueAddLabel": {"success": True}}
            if name in row["labels"]:
                row["labels"].remove(name)
            return {"issueRemoveLabel": {"success": True}}
        if "commentCreate" in gql:
            inp = v["input"]
            c = {"id": f"c-{len(db.comments) + 1}", "body": inp["body"],
                 "createdAt": f"2026-08-{len(db.comments) + 1:02d}T00:00:00.000Z",
                 "user": {"id": "me", "name": "Me"}, "issue": inp["issueId"]}
            db.comments.append(c)
            return {"commentCreate": {"comment": c}}
        if "attachmentCreate" in gql:
            inp = v["input"]
            db.attachments.append({"id": f"a-{len(db.attachments) + 1}", "title": inp["title"],
                                   "url": inp["url"], "issue": inp["issueId"]})
            return {"attachmentCreate": {"success": True}}
        if "document(id" in gql:
            content = db.documents.get(v["id"])
            return {"document": {"content": content} if content is not None else None}
        raise AssertionError(f"unhandled query: {gql[:120]}")

    def _node(self, row, relations=False):
        name = row["state"]
        node = {
            "id": row["id"], "identifier": row["identifier"], "url": row["url"],
            "title": row["title"], "description": row["description"],
            "assignee": {"id": row["assignee"]} if row["assignee"] else None,
            "state": {"name": name, "type": STATES[name][1]},
            "team": {"id": row["team"]},
            "labels": {"nodes": [{"name": n} for n in row["labels"]]},
        }
        if relations:
            node["inverseRelations"] = {"nodes": [
                {"id": rid, "type": "blocks",
                 "issue": {"id": b, "identifier": self.db.issues[b]["identifier"],
                           "url": self.db.issues[b]["url"]}}
                for rid, (b, t) in self.db.relations.items() if t == row["id"]]}
        return node


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeProvider()
        self.tracker = LinearTracker(client=FakeClient(self.db))


class TestPhaseMapping(AdapterTest):
    def test_round_trip_with_documented_lossy_exception(self):
        impl = self.db.new_issue("Impl", "Body.", "Briefed")
        for phase, expect in [
            (DeliveryPhase.BRIEFED, DeliveryPhase.BRIEFED),
            (DeliveryPhase.PLANNING, DeliveryPhase.PLANNING),
            (DeliveryPhase.PLAN_GATE, DeliveryPhase.PLANNING),  # lossy, GRE-164
            (DeliveryPhase.IMPLEMENTING, DeliveryPhase.IMPLEMENTING),
            (DeliveryPhase.REVIEWING, DeliveryPhase.REVIEWING),
            (DeliveryPhase.MERGE_GATE, DeliveryPhase.MERGE_GATE),
            (DeliveryPhase.MERGED, DeliveryPhase.MERGED),
        ]:
            self.tracker.set_phase(impl, phase)
            self.assertIs(self.tracker.get_item(impl).delivery.phase, expect)
        self.assertIs(self.tracker.get_item(impl).openness, Openness.CLOSED)

    def test_close(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.tracker.close(impl, CloseOutcome.DONE)
        self.assertIs(self.tracker.get_item(impl).openness, Openness.CLOSED)
        other = self.db.new_issue("Other", "", "Planning")
        self.tracker.close(other, CloseOutcome.CANCELED)
        self.assertIs(self.tracker.get_item(other).openness, Openness.CLOSED)


class TestPatch(AdapterTest):
    def test_multi_op_patch_applies_in_one_write(self):
        impl = self.db.new_issue("Impl", "## Brief\n\nBody.", "Implementing")
        self.tracker.patch_body(impl, [
            BodyOp("insert_after", "## Brief", "\n\nInserted."),
            BodyOp("replace", "Body.", "New body."),
        ])
        body = self.tracker.get_item(impl).body
        self.assertIn("Inserted.", body)
        self.assertIn("New body.", body)

    def test_failing_anchor_aborts_with_body_untouched(self):
        impl = self.db.new_issue("Impl", "## Brief\n\nBody.", "Implementing")
        before = self.tracker.get_item(impl).body
        with self.assertRaises(PatchError):
            self.tracker.patch_body(impl, [
                BodyOp("replace", "Body.", "x"),          # would apply...
                BodyOp("replace", "NO SUCH ANCHOR", "y"),  # ...but this aborts all
            ])
        self.assertEqual(self.tracker.get_item(impl).body, before)

    def test_ambiguous_anchor_raises(self):
        with self.assertRaises(PatchError):
            _apply_ops("aa", [BodyOp("replace", "a", "b")])


class TestClaim(AdapterTest):
    def test_established_holder_is_not_stolen(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.db.issues[impl]["assignee"] = "session-b"  # held for an hour
        writes: list = []
        self.db.on_assignee_write = lambda: writes.append(True)
        result = self.tracker.claim(impl, "session-a")
        self.assertEqual(result, ClaimResult(held=False, holder="session-b"))
        self.assertEqual(writes, [], "claim must refuse without writing")
        self.assertEqual(self.db.issues[impl]["assignee"], "session-b")

    def test_lost_race_is_reported_with_holder(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.db.on_assignee_write = (
            lambda: self.db.issues[impl].__setitem__("assignee", "session-b")
        )
        result = self.tracker.claim(impl, "session-a")
        self.assertEqual(result, ClaimResult(held=False, holder="session-b"))

    def test_free_claim_verified_by_re_read(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.assertTrue(self.tracker.claim(impl, "session-a").held)
        self.assertTrue(self.tracker.claim(impl, "session-a").held, "re-claim of own is fine")

    def test_release(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.db.issues[impl]["assignee"] = "session-b"
        self.tracker.release(impl, "session-a")
        self.assertEqual(self.db.issues[impl]["assignee"], "session-b", "non-holder is a no-op")
        self.tracker.release(impl, "session-b")
        self.assertIsNone(self.db.issues[impl]["assignee"])


class TestDeliveryState(AdapterTest):
    def test_branch_and_workspace_round_trip(self):
        impl = self.db.new_issue("Impl", "## Brief\n\nBody.", "Implementing")
        self.tracker.set_delivery(impl, branch="feature/sym-1-x", workspace="/ws/SYM-1")
        item = self.tracker.get_item(impl)
        self.assertEqual(item.delivery.branch, "feature/sym-1-x")
        self.assertEqual(item.delivery.workspace, "/ws/SYM-1")
        self.assertIn("## Symphonia delivery", item.body)

    def test_unknown_delivery_field_raises(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        with self.assertRaises(LinearError):
            self.tracker.set_delivery(impl, pull_request="x")

    def test_attention_round_trips_and_clears(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        flag = Attention(needs=True, code=AttentionCode.WORKER_QUIET, reason="quiet 20m")
        self.tracker.set_attention(impl, flag)
        self.assertEqual(self.tracker.get_item(impl).delivery.attention, flag)
        self.tracker.set_attention(impl, Attention(needs=False))
        self.assertEqual(self.tracker.get_item(impl).delivery.attention, Attention(needs=False))

    def test_gate_label_round_trips(self):
        impl = self.db.new_issue("Impl", "", "Implementing")
        self.tracker.set_gate(impl, True)
        self.assertIn("human-gate", self.db.issues[impl]["labels"])
        self.tracker.set_gate(impl, False)
        self.assertNotIn("human-gate", self.db.issues[impl]["labels"])


class TestStructure(AdapterTest):
    def setUp(self):
        super().setUp()
        self.map_id = self.db.new_issue("Map", "## Destination\n", "Backlog")

    def test_create_child_labels_round_trip(self):
        child = self.tracker.create_child(
            self.map_id, ItemKind.DECISION_TICKET, "Decide X", "## Question\n\nX?",
            decision_type=DecisionType.RESEARCH)
        self.assertIsInstance(child, ItemRef)
        got = self.tracker.get_item(child.id)
        self.assertIs(got.kind, ItemKind.DECISION_TICKET)
        self.assertIs(got.decision_type, DecisionType.RESEARCH)

    def test_blockers_add_and_remove(self):
        child = self.tracker.create_child(self.map_id, ItemKind.DECISION_TICKET, "A", "?")
        blocker = self.tracker.create_child(self.map_id, ItemKind.DECISION_TICKET, "B", "?")
        self.tracker.add_blocker(child.id, blocker.id)
        kids = self.tracker.list_children(self.map_id, with_relations=True)
        target = next(k for k in kids if k.ref.id == child.id)
        self.assertEqual([b.id for b in target.blocked_by], [blocker.id])
        self.tracker.remove_blocker(child.id, blocker.id)
        self.assertEqual(self.tracker.get_item(child.id, with_relations=True).blocked_by, ())

    def test_needing_attention_is_label_filtered(self):
        child = self.tracker.create_child(self.map_id, ItemKind.DECISION_TICKET, "A", "?")
        self.tracker.create_child(self.map_id, ItemKind.DECISION_TICKET, "B", "?")
        self.tracker.set_attention(
            child.id, Attention(needs=True, code=AttentionCode.ROLE_REPORTED, reason="r"))
        flagged = self.tracker.list_needing_attention(self.map_id)
        self.assertEqual([i.ref.id for i in flagged], [child.id])

    def test_pagination_overflow_fails_loud(self):
        self.tracker.create_child(self.map_id, ItemKind.DECISION_TICKET, "A", "?")
        self.db.force_has_next_page = True
        with self.assertRaises(LinearError):
            self.tracker.list_children(self.map_id)
        with self.assertRaises(LinearError):
            self.tracker.list_comments(self.map_id)
        with self.assertRaises(LinearError):
            self.tracker.list_artifacts(self.map_id)


class TestCommunication(AdapterTest):
    def setUp(self):
        super().setUp()
        self.impl = self.db.new_issue("Impl", "", "Implementing")

    def test_resolution_is_tldr_first(self):
        c = self.tracker.post_resolution(self.impl, tldr="The answer.", body="## Findings\n\nDetail.")
        self.assertTrue(c.body.startswith("**TLDR:** The answer."))

    def test_record_gate_is_templated_tldr_first(self):
        g = self.tracker.record_gate(self.impl, "plan-gate", "approved", "Plan reviewed; see thread.")
        self.assertTrue(g.body.startswith("**TLDR: plan-gate — approved.**"))
        self.assertIn("## Evidence", g.body)

    def test_comments_listed(self):
        self.tracker.post_comment(self.impl, "one")
        self.tracker.post_comment(self.impl, "two")
        self.assertEqual([c.body for c in self.tracker.list_comments(self.impl)], ["one", "two"])

    def test_comments_carry_author_name_and_created_at(self):
        posted = self.tracker.post_comment(self.impl, "one")
        self.assertEqual(posted.author_name, "Me")
        self.assertTrue(posted.created_at)
        listed = self.tracker.list_comments(self.impl)[0]
        self.assertEqual(listed.author_name, "Me")
        self.assertEqual(listed.created_at, posted.created_at)

    def test_render_ref_suppresses_phantom_relations(self):
        ref = ItemRef(id="u", key="SYM-9", url="https://linear.app/x/issue/SYM-9/t")
        self.assertRegex(
            self.tracker.render_ref(ref, "Decide X"),
            re.compile(r"^\[Decide X\]\(<https://[^)]+>\)$"))

    def test_artifacts_attach_and_list(self):
        self.tracker.attach_artifact(
            self.impl, Artifact(id="docs/x.md", title="X notes", url="https://example.com/x"))
        arts = self.tracker.list_artifacts(self.impl)
        self.assertEqual([a.title for a in arts], ["X notes"])

    def test_document_artifact_resolves_id_from_url(self):
        self.db.documents["abc123"] = "doc body"
        # The artifact id is the attachment's UUID — useless for the document
        # query; the slugId must come from the URL.
        art = Artifact(id="a-1", title="D", url="https://linear.app/x/document/my-doc-abc123")
        self.assertEqual(self.tracker.read_artifact(art), "doc body")

    def test_unresolvable_document_fails_loud(self):
        art = Artifact(id="a-1", title="D", url="https://linear.app/x/document/nope-xyz")
        with self.assertRaises(LinearError):
            self.tracker.read_artifact(art)




class TestProtocolConformance(unittest.TestCase):
    """A cheap guard against contract drift: every member the ``TrackerAdapter``
    Protocol declares must exist on ``LinearTracker``, so a promoted method
    that is only added to one of the two never passes in silence."""

    def test_every_protocol_member_exists_on_linear_tracker(self):
        from adapters import tracker_adapter

        missing = [
            name
            for name in vars(tracker_adapter.TrackerAdapter)
            if not name.startswith("_") and not hasattr(LinearTracker, name)
        ]
        self.assertEqual(missing, [])


class TestApiKeyComesFromTheEnvironmentOrAFile(unittest.TestCase):
    """The key is read from the environment; a `.env` only fills the gap. The
    wiring is here, not just in `adapters/env.py`: without it a machine with a
    perfectly good `.env` still cannot build a Brief."""

    def setUp(self):
        import os
        import tempfile

        from adapters import env as ENV

        self.os = os
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = os.environ.pop("LINEAR_API_KEY", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("LINEAR_API_KEY", self.previous)
            if self.previous is not None else None
        )
        self.addCleanup(os.environ.pop, "SYMPHONIA_ENV", None)
        # The loader's other two candidates are real paths on this machine —
        # the developer's own ~/.symphonia/.env and the repo's .env — and
        # either one would decide this test instead of the fixture.
        for attribute in ("SHARED_ENV", "REPO"):
            self.addCleanup(setattr, ENV, attribute, getattr(ENV, attribute))
        ENV.SHARED_ENV = Path(self.tmp.name) / "shared-absent.env"
        ENV.REPO = Path(self.tmp.name) / "repo-absent"

    def env_file(self, text: str) -> str:
        path = Path(self.tmp.name) / ".env"
        path.write_text(text)
        return str(path)

    def test_a_dotenv_supplies_a_missing_key(self):
        self.os.environ["SYMPHONIA_ENV"] = self.env_file('LINEAR_API_KEY="lin_api_file"\n')
        self.assertEqual(LinearClient().api_key, "lin_api_file")

    def test_the_environment_still_wins(self):
        self.os.environ["SYMPHONIA_ENV"] = self.env_file("LINEAR_API_KEY=lin_api_file\n")
        self.os.environ["LINEAR_API_KEY"] = "lin_api_shell"
        self.assertEqual(LinearClient().api_key, "lin_api_shell")

    def test_with_neither_the_error_says_where_to_put_it(self):
        self.os.environ["SYMPHONIA_ENV"] = str(Path(self.tmp.name) / "absent.env")
        with self.assertRaises(LinearError) as ctx:
            LinearClient()
        self.assertIn(".symphonia/.env", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)