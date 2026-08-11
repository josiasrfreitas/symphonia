"""Linear GraphQL client.

TLDR: one function — ``query(gql, variables)`` — over urllib, authenticated
with ``LINEAR_API_KEY`` from the environment. No retries, no interpretation:
transport and GraphQL errors raise ``LinearError`` loudly.

Deviation (recorded on GRE-174): the ticket says "via MCP", but MCP is only
callable by an agent, not by a deterministic Python script. The adapter talks
to Linear's public GraphQL API directly instead.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..env import SHARED_ENV, load

API_URL = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    """Any failure talking to Linear: transport, HTTP, or GraphQL errors."""


class LinearClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        if not self.api_key:
            # Only now — a shell that already exported the key never reads a
            # file, and this is the one place in the package that needs it.
            load()
            self.api_key = os.environ.get("LINEAR_API_KEY", "")
        if not self.api_key:
            raise LinearError(
                f"LINEAR_API_KEY is not set, and no .env carrying it was found. "
                f"Put it in {SHARED_ENV} (shared with every role, in any worktree), "
                f"or in the repo's own .env, or export it in the shell. "
                f"Without it a Brief cannot be built and the gate cannot label a ticket."
            )
        self.timeout = timeout

    def query(self, gql: str, variables: dict | None = None) -> dict:
        """Run one GraphQL operation and return its ``data`` object."""
        payload = json.dumps({"query": gql, "variables": variables or {}})
        request = urllib.request.Request(
            API_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
            },
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
