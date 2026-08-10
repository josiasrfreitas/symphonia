"""Linear implementation of the Tracker Adapter contract (GRE-174).

TLDR: ``LinearTracker`` implements the ``TrackerAdapter`` protocol from
``..tracker_adapter`` against Linear's GraphQL API. The provider-specific
identifiers (team, statuses, labels) live in ``config.json`` next to this
file — nothing here is hardcoded to a status name.

The two hostile facts of the provider, and what this file does about them:

- No optimistic locking anywhere; last write wins silently. Body writes are
  therefore anchored patches staged fully in memory — a failing anchor aborts
  before anything is written (the client-side rollback), and every write is
  re-read and compared before the call returns. ``claim`` writes the assignee,
  re-reads, and only reports ``held`` when the re-read confirms it.
- A bare issue URL in a body silently creates a real relation. References are
  rendered ``[label](<url>)`` — angle brackets suppress the phantom relation.

Deviations, recorded on GRE-174:

- Phase read-back is lossy: the SYM team maps both ``planning`` and
  ``plan-gate`` onto the single ``Planning`` status (GRE-164), so a re-read of
  a ``plan-gate`` ticket reports ``planning``. The map lives in ``config.json``
  so a dedicated status, once created, slots in with a config-only change.
- Transport is GraphQL over urllib, not MCP (see ``client.py``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..attention import Attention, AttentionCode
from ..tracker_adapter import (
    ActorId,
    Artifact,
    BodyOp,
    ClaimResult,
    CloseOutcome,
    Comment,
    DecisionType,
    Delivery,
    DeliveryPhase,
    Item,
    ItemKind,
    ItemRef,
    Openness,
    TrackerCapabilities,
)
from .client import LinearClient, LinearError


class PatchError(LinearError):
    """An anchored patch could not be applied; nothing was written."""


_CLOSED_STATE_TYPES = {"completed", "canceled", "duplicate"}

_FIELDS = """id identifier url title description
    assignee { id }
    state { name type }
    team { id }
    labels { nodes { name } }"""

_RELATIONS = """inverseRelations { nodes { id type issue { id identifier url } } }"""

_M_UPDATE = """mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success } }"""


def _apply_ops(body: str, ops: list[BodyOp]) -> str:
    """Stage every op in memory. Any anchor that does not match exactly once
    aborts the whole patch — that precondition is the only compare-and-swap
    the provider offers, so it must fail loudly, never best-effort."""
    for op in ops:
        hits = body.count(op.anchor)
        if hits != 1:
            raise PatchError(
                f"anchor {op.anchor!r} matched {hits} times, expected exactly 1; "
                "patch aborted, body untouched"
            )
        if op.op == "insert_before":
            body = body.replace(op.anchor, op.text + op.anchor)
        elif op.op == "insert_after":
            body = body.replace(op.anchor, op.anchor + op.text)
        elif op.op == "replace":
            body = body.replace(op.anchor, op.text)
        else:
            raise PatchError(f"unknown body op {op.op!r}")
    return body


class LinearTracker:
    """The Tracker Adapter for Linear. Satisfies the ``TrackerAdapter`` protocol."""

    capabilities = TrackerCapabilities(anchored_patch=True, artifact_read_back=True)

    def __init__(self, client: LinearClient | None = None, config: dict | None = None):
        self._c = client or LinearClient()
        self._cfg = config or json.loads(
            (Path(__file__).parent / "config.json").read_text()
        )
        self._phase_status = {
            DeliveryPhase(phase): status
            for phase, status in self._cfg["phase_status"].items()
        }
        # Reverse map in enum declaration order: the first phase mapped to a
        # status wins the read-back, so `Planning` reads as PLANNING — the
        # lossy projection accepted in GRE-164.
        self._status_phase: dict[str, DeliveryPhase] = {}
        for phase in DeliveryPhase:
            self._status_phase.setdefault(self._phase_status[phase], phase)
        self._state_ids: dict[tuple[str, str], str] = {}
        self._label_ids: dict[tuple[str, str], str] = {}

    # --- raw provider access -------------------------------------------------

    def _issue(self, id: str, *, relations: bool = False) -> dict:
        """Fetch one issue by UUID or key (``issue`` accepts both)."""
        fields = _FIELDS + ("\n" + _RELATIONS if relations else "")
        data = self._c.query(
            "query($id: String!) { issue(id: $id) { %s } }" % fields, {"id": id}
        )
        if data["issue"] is None:
            raise LinearError(f"no such issue: {id}")
        return data["issue"]

    def _state_id(self, team_id: str, name: str) -> str:
        if (team_id, name) not in self._state_ids:
            data = self._c.query(
                "query($id: String!) { team(id: $id) { states { nodes { id name } } } }",
                {"id": team_id},
            )
            for node in data["team"]["states"]["nodes"]:
                self._state_ids[(team_id, node["name"])] = node["id"]
        if (team_id, name) not in self._state_ids:
            raise LinearError(f"team {team_id} has no status named {name!r}")
        return self._state_ids[(team_id, name)]

    def _label_id(self, team_id: str, name: str) -> str:
        if (team_id, name) not in self._label_ids:
            data = self._c.query(
                """query($team: ID!, $name: String!) {
                  issueLabels(filter: {name: {eq: $name}, team: {id: {eq: $team}}}) {
                    nodes { id } } }""",
                {"team": team_id, "name": name},
            )
            nodes = data["issueLabels"]["nodes"]
            if not nodes:
                created = self._c.query(
                    """mutation($input: IssueLabelCreateInput!) {
                      issueLabelCreate(input: $input) { issueLabel { id } } }""",
                    {"input": {"teamId": team_id, "name": name}},
                )
                nodes = [created["issueLabelCreate"]["issueLabel"]]
            self._label_ids[(team_id, name)] = nodes[0]["id"]
        return self._label_ids[(team_id, name)]

    # --- hydration -----------------------------------------------------------

    @staticmethod
    def _ref(node: dict) -> ItemRef:
        return ItemRef(id=node["id"], key=node["identifier"], url=node["url"])

    def _label_value(self, labels: list[str], prefix: str) -> str | None:
        return next((l[len(prefix):] for l in labels if l.startswith(prefix)), None)

    def _hydrate(self, node: dict, *, with_relations: bool) -> Item:
        labels = [l["name"] for l in node["labels"]["nodes"]]
        body = node["description"] or ""
        fields = self._parse_delivery(body)

        kind_value = self._label_value(labels, self._cfg["kind_label_prefix"])
        # An issue with no kind label is delivery work: the adapter only meets
        # unlabeled issues in the execution team, never on the Decision Map.
        kind = ItemKind(kind_value) if kind_value else ItemKind.IMPLEMENTATION_TICKET
        type_value = self._label_value(labels, self._cfg["type_label_prefix"])

        if self._cfg["attention_label"] in labels:
            code_value, _, reason = fields.get("attention", "").partition(" — ")
            try:
                code = AttentionCode(code_value)
            except ValueError:
                code = None
                reason = fields.get("attention", "")
            attention = Attention(needs=True, code=code, reason=reason)
        else:
            attention = Attention(needs=False)

        phase = self._status_phase.get(node["state"]["name"])
        blocked_by = ()
        if with_relations:
            blocked_by = tuple(
                self._ref(r["issue"])
                for r in node["inverseRelations"]["nodes"]
                if r["type"] == "blocks"
            )
        return Item(
            ref=self._ref(node),
            kind=kind,
            title=node["title"],
            body=body,
            openness=(
                Openness.CLOSED
                if node["state"]["type"] in _CLOSED_STATE_TYPES
                else Openness.OPEN
            ),
            assignee=node["assignee"]["id"] if node["assignee"] else None,
            decision_type=DecisionType(type_value) if type_value else None,
            delivery=(
                Delivery(
                    phase=phase,
                    attention=attention,
                    branch=fields.get("branch"),
                    workspace=fields.get("workspace"),
                )
                if phase
                else None
            ),
            blocked_by=blocked_by,
        )

    # --- the delivery section in the body ------------------------------------
    # Branch, workspace and the attention reason have no native field on the
    # provider, so they live in one anchored section at the end of the body,
    # maintained by the same staged-write path as every other patch.

    def _delivery_section(self, body: str) -> str | None:
        start = body.find(self._cfg["delivery_heading"])
        if start < 0:
            return None
        end = body.find("\n## ", start + len(self._cfg["delivery_heading"]))
        return body[start:] if end < 0 else body[start:end]

    def _parse_delivery(self, body: str) -> dict[str, str]:
        section = self._delivery_section(body) or ""
        return {
            m.group(1): m.group(2).strip()
            for m in re.finditer(
                r"^[-*] (branch|workspace|attention): (.+)$", section, re.M
            )
        }

    def _patch_delivery(self, node: dict, updates: dict[str, str | None]) -> None:
        body = node["description"] or ""
        fields = self._parse_delivery(body)
        for key, value in updates.items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        lines = "".join(f"- {k}: {v}\n" for k, v in sorted(fields.items()))
        section = f"{self._cfg['delivery_heading']}\n\n{lines}"
        old = self._delivery_section(body)
        if old is not None:
            new_body = body.replace(old, section, 1)
        elif fields:
            new_body = (body.rstrip() + "\n\n" + section) if body.strip() else section
        else:
            new_body = body
        if new_body != body:
            self._write_body(node["id"], new_body)

    def _write_body(self, issue_id: str, new_body: str) -> None:
        """One whole staged write, then read back and compare. The provider has
        no conditional update, so the read-back is the only confirmation that
        a concurrent writer did not land on top of ours."""
        self._c.query(_M_UPDATE, {"id": issue_id, "input": {"description": new_body}})
        read_back = self._issue(issue_id)["description"] or ""
        if read_back != new_body:
            raise PatchError(
                f"read-back of {issue_id} does not match what was written; "
                "a concurrent edit landed on top — re-read and re-patch"
            )

    # --- reading -------------------------------------------------------------

    def get_item(self, id: str, *, with_relations: bool = False) -> Item:
        return self._hydrate(
            self._issue(id, relations=with_relations), with_relations=with_relations
        )

    def _children(self, map_id: str, *, relations: bool, extra_filter: str = "") -> list[Item]:
        parent = self._issue(map_id)
        fields = _FIELDS + ("\n" + _RELATIONS if relations else "")
        data = self._c.query(
            """query($parent: ID!%s) {
              issues(first: 250, filter: {parent: {id: {eq: $parent}}%s}) {
                nodes { %s } } }"""
            % (
                ", $label: String!" if extra_filter else "",
                extra_filter,
                fields,
            ),
            {"parent": parent["id"], **({"label": self._cfg["attention_label"]} if extra_filter else {})},
        )
        return [
            self._hydrate(node, with_relations=relations)
            for node in data["issues"]["nodes"]
        ]

    def list_children(self, map_id: str, *, with_relations: bool = False) -> list[Item]:
        return self._children(map_id, relations=with_relations)

    def list_needing_attention(self, map_id: str) -> list[Item]:
        """The one query answered server-side: filtered by the attention label."""
        return self._children(
            map_id, relations=False, extra_filter=", labels: {name: {eq: $label}}"
        )

    def list_comments(self, id: str) -> list[Comment]:
        node = self._issue(id)
        data = self._c.query(
            """query($id: String!) { issue(id: $id) {
              comments(first: 100) { nodes { id body user { id } } } } }""",
            {"id": node["id"]},
        )
        return [
            Comment(id=c["id"], body=c["body"], author=(c["user"] or {}).get("id", ""))
            for c in data["issue"]["comments"]["nodes"]
        ]

    def list_artifacts(self, id: str) -> list[Artifact]:
        node = self._issue(id)
        data = self._c.query(
            """query($id: String!) { issue(id: $id) {
              attachments { nodes { id title url } } } }""",
            {"id": node["id"]},
        )
        return [
            Artifact(id=a["id"], title=a["title"], url=a["url"])
            for a in data["issue"]["attachments"]["nodes"]
        ]

    def read_artifact(self, artifact: Artifact) -> str:
        """Documents read back through the API; repo files (whose ``id`` is
        their repo path) read from the working tree. Uploaded file attachments
        are unreadable on this provider — that is why nothing writes them."""
        if "/document/" in artifact.url:
            data = self._c.query(
                "query($id: String!) { document(id: $id) { content } }",
                {"id": artifact.id},
            )
            return data["document"]["content"] or ""
        path = Path(artifact.id)
        if path.is_file():
            return path.read_text()
        raise LinearError(f"artifact {artifact.title!r} is not readable: {artifact.url}")

    # --- structure -----------------------------------------------------------

    def create_child(
        self,
        map_id: str,
        kind: ItemKind,
        title: str,
        body: str,
        *,
        decision_type: DecisionType | None = None,
    ) -> ItemRef:
        parent = self._issue(map_id)
        team_id = parent["team"]["id"]
        label_ids = [
            self._label_id(team_id, self._cfg["kind_label_prefix"] + kind.value)
        ]
        if decision_type:
            label_ids.append(
                self._label_id(
                    team_id, self._cfg["type_label_prefix"] + decision_type.value
                )
            )
        data = self._c.query(
            """mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) { issue { id identifier url } } }""",
            {
                "input": {
                    "teamId": team_id,
                    "parentId": parent["id"],
                    "title": title,
                    "description": body,
                    "labelIds": label_ids,
                }
            },
        )
        return self._ref(data["issueCreate"]["issue"])

    def add_blocker(self, id: str, blocker_id: str) -> None:
        blocker = self._issue(blocker_id)
        blocked = self._issue(id)
        self._c.query(
            """mutation($input: IssueRelationCreateInput!) {
              issueRelationCreate(input: $input) { success } }""",
            {
                "input": {
                    "issueId": blocker["id"],
                    "relatedIssueId": blocked["id"],
                    "type": "blocks",
                }
            },
        )

    def remove_blocker(self, id: str, blocker_id: str) -> None:
        blocker = self._issue(blocker_id)
        node = self._issue(id, relations=True)
        for relation in node["inverseRelations"]["nodes"]:
            if relation["type"] == "blocks" and relation["issue"]["id"] == blocker["id"]:
                self._c.query(
                    "mutation($id: String!) { issueRelationDelete(id: $id) { success } }",
                    {"id": relation["id"]},
                )
                return
        raise LinearError(f"{blocker_id} is not a blocker of {id}")

    def patch_body(self, id: str, ops: list[BodyOp]) -> None:
        """Anchored patch with client-side rollback: every op is staged in
        memory against one read, so a failing anchor aborts the whole patch
        with the body untouched. Only a fully applied result is ever written,
        and the write is read back and compared before returning."""
        node = self._issue(id)
        self._write_body(node["id"], _apply_ops(node["description"] or "", ops))

    # --- ownership and delivery state ----------------------------------------

    def claim(self, id: str, actor: ActorId) -> ClaimResult:
        """Write, then re-read, then answer — never optimistic. The provider
        accepts every assignee write silently, so the re-read is the only way
        to learn a concurrent claimant got there last."""
        node = self._issue(id)
        self._c.query(_M_UPDATE, {"id": node["id"], "input": {"assigneeId": actor}})
        read_back = self._issue(node["id"])["assignee"]
        holder = read_back["id"] if read_back else None
        return ClaimResult(held=holder == actor, holder=holder)

    def release(self, id: str, actor: ActorId) -> None:
        node = self._issue(id)
        if node["assignee"] and node["assignee"]["id"] == actor:
            self._c.query(_M_UPDATE, {"id": node["id"], "input": {"assigneeId": None}})

    def close(self, id: str, outcome: CloseOutcome) -> None:
        node = self._issue(id)
        state_id = self._state_id(
            node["team"]["id"], self._cfg["close_status"][outcome.value]
        )
        self._c.query(_M_UPDATE, {"id": node["id"], "input": {"stateId": state_id}})

    def set_phase(self, id: str, phase: DeliveryPhase) -> None:
        node = self._issue(id)
        state_id = self._state_id(node["team"]["id"], self._phase_status[phase])
        self._c.query(_M_UPDATE, {"id": node["id"], "input": {"stateId": state_id}})

    def set_attention(self, id: str, attention: Attention) -> None:
        node = self._issue(id)
        label_id = self._label_id(node["team"]["id"], self._cfg["attention_label"])
        mutation = "issueAddLabel" if attention.needs else "issueRemoveLabel"
        self._c.query(
            "mutation($id: String!, $label: String!) { %s(id: $id, labelId: $label) { success } }"
            % mutation,
            {"id": node["id"], "label": label_id},
        )
        self._patch_delivery(
            node,
            {
                "attention": (
                    f"{attention.code.value} — {attention.reason}"
                    if attention.needs and attention.code
                    else None
                )
            },
        )

    def set_delivery(self, id: str, **delivery: object) -> None:
        unknown = set(delivery) - {"branch", "workspace"}
        if unknown:
            raise LinearError(f"unknown delivery fields: {sorted(unknown)}")
        self._patch_delivery(
            self._issue(id),
            {k: (str(v) if v is not None else None) for k, v in delivery.items()},
        )

    # --- communication -------------------------------------------------------

    def post_comment(self, id: str, body: str) -> Comment:
        node = self._issue(id)
        data = self._c.query(
            """mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) { comment { id body user { id } } } }""",
            {"input": {"issueId": node["id"], "body": body}},
        )
        comment = data["commentCreate"]["comment"]
        return Comment(
            id=comment["id"],
            body=comment["body"],
            author=(comment["user"] or {}).get("id", ""),
        )

    def post_resolution(self, id: str, *, tldr: str, body: str) -> Comment:
        return self.post_comment(id, f"**TLDR:** {tldr}\n\n{body}")

    def record_gate(self, ticket: str, gate: str, decision: str, evidence: str) -> Comment:
        """The tracker half of automatic gate recording (the runtime half is
        GRE-175): one templated comment on the ticket, TLDR first."""
        return self.post_comment(
            ticket,
            f"**TLDR: {gate} — {decision}.**\n\n## Evidence\n\n{evidence}",
        )

    def attach_artifact(self, id: str, artifact: Artifact) -> None:
        node = self._issue(id)
        self._c.query(
            """mutation($input: AttachmentCreateInput!) {
              attachmentCreate(input: $input) { success } }""",
            {"input": {"issueId": node["id"], "title": artifact.title, "url": artifact.url}},
        )

    # --- rendering -----------------------------------------------------------

    def render_ref(self, ref: ItemRef, label: str) -> str:
        """Angle brackets keep the provider from turning the URL into a real
        relation — a bare URL in a body silently creates one (GRE-152)."""
        return f"[{label}](<{ref.url}>)"
