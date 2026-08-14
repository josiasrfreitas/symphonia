"""Everything this package asks of Linear: one GraphQL client and the five
tracker operations the workflow actually performs — read a ticket, read its
comments, post a comment, flag Needs Attention, toggle the Human Gate label.

Transport is GraphQL over urllib, authenticated with `LINEAR_API_KEY` from
the environment (`env.load()` fills it from a `.env` if the shell did not).
No retries, no interpretation: transport and GraphQL errors raise
`LinearError` loudly. MCP is not used — it is only callable by an agent,
never by a deterministic script.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from env import SHARED_ENV, load

API_URL = "https://api.linear.app/graphql"

CONFIG = Path(__file__).resolve().parents[1] / "config.json"


class LinearError(RuntimeError):
    """Any failure talking to Linear: transport, HTTP, or GraphQL errors."""


class LinearClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        if not self.api_key:
            load()  # a shell that already exported the key never reads a file
            self.api_key = os.environ.get("LINEAR_API_KEY", "")
        if not self.api_key:
            raise LinearError(
                f"LINEAR_API_KEY is not set, and no .env carrying it was found. "
                f"Put it in {SHARED_ENV} (shared with every role, in any worktree), "
                f"or in the repo's own .env, or export it in the shell."
            )
        self.timeout = timeout

    def query(self, gql: str, variables: dict | None = None) -> dict:
        """Run one GraphQL operation and return its `data` object."""

        payload = json.dumps({"query": gql, "variables": variables or {}})
        request = urllib.request.Request(
            API_URL,
            data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
            raise LinearError(f"HTTP {err.code} from Linear: {detail}") from err
        except urllib.error.URLError as err:
            raise LinearError(f"cannot reach Linear: {err.reason}") from err
        if data.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in data["errors"])
            raise LinearError(f"GraphQL error: {messages}")
        return data["data"]


@dataclass(frozen=True)
class ItemRef:
    id: str
    key: str
    url: str


@dataclass(frozen=True)
class Item:
    ref: ItemRef
    title: str
    body: str


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    author_name: str
    created_at: str
    """ISO-8601, as the provider returns it."""


class LinearTracker:
    """The five operations, over `LinearClient`."""

    def __init__(self, client: LinearClient | None = None, config: dict | None = None):
        self._c = client or LinearClient()
        self._cfg = config or json.loads(CONFIG.read_text())["linear"]
        self._label_ids: dict[tuple[str, str], str] = {}

    def _issue(self, id: str) -> dict:
        """Fetch one issue by UUID or key (`issue` accepts both)."""

        data = self._c.query(
            """query($id: String!) { issue(id: $id) {
              id identifier url title description team { id } } }""",
            {"id": id},
        )
        if data["issue"] is None:
            raise LinearError(f"no such issue: {id}")
        return data["issue"]

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

    def _toggle_label(self, node: dict, label_name: str, on: bool) -> None:
        label_id = self._label_id(node["team"]["id"], label_name)
        mutation = "issueAddLabel" if on else "issueRemoveLabel"
        self._c.query(
            "mutation($id: String!, $label: String!) { %s(id: $id, labelId: $label) { success } }"
            % mutation,
            {"id": node["id"], "label": label_id},
        )

    # --- reading -----------------------------------------------------------

    def get_item(self, id: str) -> Item:
        node = self._issue(id)
        return Item(
            ref=ItemRef(id=node["id"], key=node["identifier"], url=node["url"]),
            title=node["title"],
            body=node["description"] or "",
        )

    def list_comments(self, id: str) -> list[Comment]:
        node = self._issue(id)
        data = self._c.query(
            """query($id: String!) { issue(id: $id) {
              comments(first: 100) { pageInfo { hasNextPage }
                nodes { id body createdAt user { id name } } } } }""",
            {"id": node["id"]},
        )
        connection = data["issue"]["comments"]
        # No silent caps: a listing that overflows its page raises rather
        # than returning a truncated result that reads as complete.
        if connection["pageInfo"]["hasNextPage"]:
            raise LinearError("more comments than one page holds; refusing to truncate silently")
        return [
            Comment(
                id=c["id"],
                body=c["body"],
                author_name=(c["user"] or {}).get("name", ""),
                created_at=c["createdAt"],
            )
            for c in connection["nodes"]
        ]

    # --- writing -----------------------------------------------------------

    def post_comment(self, id: str, body: str) -> Comment:
        node = self._issue(id)
        data = self._c.query(
            """mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                comment { id body createdAt user { id name } } } }""",
            {"input": {"issueId": node["id"], "body": body}},
        )
        comment = data["commentCreate"]["comment"]
        return Comment(
            id=comment["id"],
            body=comment["body"],
            author_name=(comment["user"] or {}).get("name", ""),
            created_at=comment["createdAt"],
        )

    def set_attention(self, id: str, code: str, reason: str) -> None:
        """Raise the Needs Attention flag: the label goes on and the reason
        lands as a comment — which `build_brief` folds into the next role's
        brief automatically. Only a human clears the label."""

        node = self._issue(id)
        self._toggle_label(node, self._cfg["attention_label"], True)
        self.post_comment(id, f"**Needs Attention — `{code}`.**\n\n{reason}")

    def set_gate(self, id: str, waiting: bool) -> None:
        """Put or lift the `human-gate` label — the visible sign that a
        Human Gate is waiting. Always script-driven: on submission or on
        verdict, never typed by an agent."""

        node = self._issue(id)
        self._toggle_label(node, self._cfg["gate_label"], waiting)
