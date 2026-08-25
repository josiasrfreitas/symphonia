"""Everything this package asks of Linear: one GraphQL client and the twelve
tracker operations the workflow actually performs — read a ticket, read its
comments, post a comment, flag Needs Attention, toggle the Human Gate label,
create a card, link a blocker, assign, close, patch a body section, list
children, set a neutral priority.

Transport is GraphQL over urllib, authenticated with `LINEAR_API_KEY` from
the environment (`env.load()` fills it from a `.env` if the shell did not).
No retries, no interpretation: transport and GraphQL errors raise
`LinearError` loudly. MCP is not used — it is only callable by an agent,
never by a deterministic script.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from env import SHARED_ENV, load

API_URL = "https://api.linear.app/graphql"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
"""What Linear hands back as a team id. `--team` takes either this or the
team key, and only the key needs a round trip to resolve."""

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
class Child:
    """One child card, with everything the frontier needs to order and
    filter it — nothing more."""

    key: str
    url: str
    title: str
    state: str
    state_type: str
    """The provider's state category (`backlog`, `unstarted`, `started`,
    `completed`, `canceled`) — what a script branches on, since the state
    *name* is a team setting a human can rename."""
    assignee: str
    """Display name, or `""` when unclaimed."""
    blocked_by: tuple[str, ...]
    """Keys of the cards that block this one."""
    priority: str
    """`high`, `medium`, `low`, or `""` when unset."""


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    author_name: str
    created_at: str
    """ISO-8601, as the provider returns it."""


# The neutral priority vocabulary (SYM-8): three names, provider numbers
# kept here. Linear's `urgent` (1) is deliberately not exposed — a fourth
# level is a workflow decision, not a mapping detail.
PRIORITY = {"high": 2, "medium": 3, "low": 4}

_PRIORITY_NAME = {number: name for name, number in PRIORITY.items()}


def _as_int(value) -> int | None:
    return None if value is None else int(value)


def _outside_fences(lines: list[str]) -> list[bool]:
    """One flag per line: is it outside every fenced code block? A `## `
    inside a ``` or ~~~ fence is a template someone is quoting, not a
    heading — the body of a design ticket is full of them."""

    flags = []
    fence = ""
    for line in lines:
        opener = line.lstrip()[:3]
        if fence:
            flags.append(False)
            if opener == fence:
                fence = ""
        elif opener in ("```", "~~~"):
            flags.append(False)
            fence = opener
        else:
            flags.append(True)
    return flags


def patch_section(body: str, heading: str, content: str) -> str:
    """Replace the body of the `## <heading>` section, keeping every other
    section exactly as written — including its trailing newline; append the
    section at the end when it does not exist yet. Headings inside fenced
    code blocks are text, not structure. Pure — the network call around it
    is `LinearTracker.patch_body_section`."""

    lines = body.splitlines()
    outside = _outside_fences(lines)
    marker = f"## {heading}"
    try:
        start = next(
            i for i, line in enumerate(lines)
            if outside[i] and line.strip() == marker
        )
    except StopIteration:
        prefix = body.rstrip("\n")
        return (prefix + "\n\n" if prefix else "") + f"{marker}\n{content}\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if outside[i] and lines[i].startswith("## ")),
        len(lines),
    )
    tail = lines[end:]
    separator = [""] if tail else []  # the blank line the replaced section owned
    patched = "\n".join(lines[: start + 1] + content.splitlines() + separator + tail)
    return patched + "\n" if body.endswith("\n") else patched


class LinearTracker:
    """The twelve operations, over `LinearClient`."""

    def __init__(self, client: LinearClient | None = None, config: dict | None = None):
        self._c = client or LinearClient()
        self._cfg = config or json.loads(CONFIG.read_text())["linear"]
        self._label_ids: dict[tuple[str, str], str] = {}
        self._team_ids: dict[str, str] = {}

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

    def _team_id(self, team: str) -> str:
        """A team's UUID, from either its UUID or its key (`SYM`).

        Every mutation wants the UUID, but the key is what a person knows
        and what `map new --team SYM` is documented with. Passing the key
        straight through as `teamId` reached Linear as `Argument
        Validation Error`, which names nothing a caller can act on."""

        if _UUID.fullmatch(team):
            return team
        if team not in self._team_ids:
            data = self._c.query(
                "query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes { id } } }",
                {"key": team},
            )
            nodes = data["teams"]["nodes"]
            if not nodes:
                raise LinearError(
                    f"no team with the key {team!r}; pass the key shown on a card "
                    f"(the SYM of SYM-123) or the team's UUID"
                )
            self._team_ids[team] = nodes[0]["id"]
        return self._team_ids[team]

    def _label_id(self, team_id: str, name: str) -> str:
        """The id of the label called `name` that this team can use.

        A Linear label is either scoped to one team or shared across the
        whole workspace, and both are usable on a card of this team — so
        both are searched. Looking only at team-scoped labels made every
        workspace label read as missing, and the create that followed came
        back `duplicate label name`: the tool could not use a label it
        could not see, and could not create it either."""

        if (team_id, name) not in self._label_ids:
            data = self._c.query(
                """query($team: ID!, $name: String!) {
                  issueLabels(filter: {name: {eq: $name}, or: [
                    {team: {id: {eq: $team}}}, {team: {null: true}}]}) {
                    nodes { id team { id } } } }""",
                {"team": team_id, "name": name},
            )
            # A workspace label and a team label may share a name; the
            # team's own is the more specific and wins.
            nodes = sorted(data["issueLabels"]["nodes"],
                           key=lambda n: n["team"] is None)
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

    def _user_id(self, name_or_email: str) -> str:
        """Resolve a person to their id by display name or email. Zero
        matches or more than one both raise: picking one of two people
        named "Ana" silently is worse than refusing."""

        data = self._c.query(
            """query($who: String!) { users(filter: {or: [
              {name: {eq: $who}}, {email: {eq: $who}}]}) {
                nodes { id name email } } }""",
            {"who": name_or_email},
        )
        nodes = data["users"]["nodes"]
        if not nodes:
            raise LinearError(f"no such user: {name_or_email!r}")
        if len(nodes) > 1:
            names = ", ".join(f"{n.get('name')} <{n.get('email')}>" for n in nodes)
            raise LinearError(
                f"{name_or_email!r} matches more than one user ({names}); "
                f"use the email, which is unique"
            )
        return nodes[0]["id"]

    def _done_state_id(self, team_id: str) -> str:
        """The team's first `completed` workflow state, by position. A team
        may have several (Done, Shipped); the first is the one a board
        shows leftmost, and closing into it is what `close_item` means."""

        data = self._c.query(
            """query($team: ID!) { workflowStates(filter: {
              team: {id: {eq: $team}}, type: {eq: "completed"}}) {
                nodes { id name position } } }""",
            {"team": team_id},
        )
        nodes = data["workflowStates"]["nodes"]
        if not nodes:
            raise LinearError(f"team {team_id} has no workflow state of type 'completed'")
        return sorted(nodes, key=lambda n: n.get("position", 0))[0]["id"]

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

    def list_children(self, id: str) -> list[Child]:
        """Every child card with what the frontier needs: state, owner,
        blockers, priority. Blockers come from each child's *inverse*
        relations — the relation is stored on the blocker, so the card
        being blocked only sees it from the other side."""

        node = self._issue(id)
        data = self._c.query(
            """query($id: String!) { issue(id: $id) {
              children(first: 100) { pageInfo { hasNextPage }
                nodes { identifier url title priority
                  state { name type } assignee { name }
                  inverseRelations { nodes { type issue { identifier } } } } } } }""",
            {"id": node["id"]},
        )
        connection = data["issue"]["children"]
        # Same rule as `list_comments`: no silent caps.
        if connection["pageInfo"]["hasNextPage"]:
            raise LinearError("more children than one page holds; refusing to truncate silently")
        return [
            Child(
                key=c["identifier"],
                url=c["url"],
                title=c["title"],
                state=(c["state"] or {}).get("name", ""),
                state_type=(c["state"] or {}).get("type", ""),
                assignee=(c["assignee"] or {}).get("name", ""),
                blocked_by=tuple(
                    r["issue"]["identifier"]
                    for r in (c.get("inverseRelations") or {}).get("nodes", [])
                    if r.get("type") == "blocks" and r.get("issue")
                ),
                # Linear types `priority` as a Float, so 2 may arrive as
                # 2.0; the map is keyed by int.
                priority=_PRIORITY_NAME.get(_as_int(c.get("priority")), ""),
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

    def create_item(
        self,
        title: str,
        body: str,
        *,
        parent: str | None = None,
        labels: tuple[str, ...] | list[str] = (),
        team: str | None = None,
    ) -> ItemRef:
        """Create a card, optionally under a parent and with labels. The
        team is inherited from the parent when not given; with neither, the
        call refuses rather than guessing at a workspace default."""

        parent_node = self._issue(parent) if parent else None
        team_id = (self._team_id(team) if team
                   else (parent_node["team"]["id"] if parent_node else None))
        if not team_id:
            raise LinearError(
                "create_item needs a team: pass team=, or parent= to inherit the parent's"
            )
        payload: dict = {"teamId": team_id, "title": title, "description": body}
        if parent_node:
            payload["parentId"] = parent_node["id"]
        if labels:
            payload["labelIds"] = [self._label_id(team_id, name) for name in labels]
        data = self._c.query(
            """mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) { issue { id identifier url } } }""",
            {"input": payload},
        )
        issue = data["issueCreate"]["issue"]
        return ItemRef(id=issue["id"], key=issue["identifier"], url=issue["url"])

    def add_blocker(self, id: str, blocked_by: str) -> None:
        """Record that `id` cannot start until `blocked_by` is done.

        Direction, because it is the one thing easy to invert: Linear's
        `blocks` relation reads `issueId` blocks `relatedIssueId`, so the
        *blocker* is `issueId` and the card being blocked is
        `relatedIssueId`."""

        blocked = self._issue(id)
        blocker = self._issue(blocked_by)
        self._c.query(
            """mutation($input: IssueRelationCreateInput!) {
              issueRelationCreate(input: $input) { success } }""",
            {"input": {
                "type": "blocks",
                "issueId": blocker["id"],
                "relatedIssueId": blocked["id"],
            }},
        )

    def assign(self, id: str, assignee: str) -> None:
        """Claim a card for one person, by display name or email."""

        node = self._issue(id)
        self._c.query(
            """mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success } }""",
            {"id": node["id"], "input": {"assigneeId": self._user_id(assignee)}},
        )

    def close_item(self, id: str) -> None:
        """Move a card into its team's completed state."""

        node = self._issue(id)
        self._c.query(
            """mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success } }""",
            {"id": node["id"], "input": {"stateId": self._done_state_id(node["team"]["id"])}},
        )

    def patch_body_section(self, id: str, heading: str, content: str) -> None:
        """Rewrite one `## <heading>` section of a card's body and leave
        the rest byte for byte as it was — the index line a map keeps is
        script-written, the prose around it is the user's."""

        node = self._issue(id)
        body = patch_section(node["description"] or "", heading, content)
        self._c.query(
            """mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success } }""",
            {"id": node["id"], "input": {"description": body}},
        )

    def set_priority(self, id: str, level: str, *, user_requested: bool) -> None:
        """Set the neutral priority. Priority is the user's field (SYM-8):
        without `user_requested=True` this refuses, so an agent cannot
        promote its own work by deciding it matters."""

        if not user_requested:
            raise LinearError(
                "priority is the user's field: set_priority needs user_requested=True, "
                "which only a `--user-requested` call from the user carries"
            )
        if level not in PRIORITY:
            raise LinearError(
                f"unknown priority {level!r}; use one of {', '.join(PRIORITY)}"
            )
        node = self._issue(id)
        self._c.query(
            """mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success } }""",
            {"id": node["id"], "input": {"priority": PRIORITY[level]}},
        )
