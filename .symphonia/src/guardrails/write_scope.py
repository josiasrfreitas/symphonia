#!/usr/bin/env python3
"""Write Scope guardrail (skeleton).

Two checks, both deterministic:
- collision: intersect the declared Write Scope path lists of tickets about to
  run in parallel; any shared path blocks the dispatch.
- audit: compare `git diff --name-only` of a finished ticket against its
  declared Write Scope; any undeclared path raises Needs Attention.

Real implementation has its own ticket. This file fixes the CLI shape only.
"""
import sys


def collision(scopes: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """Return (ticket_a, ticket_b, shared_path) for every collision."""
    raise NotImplementedError("skeleton — see .symphonia/guardrails/README.md")


def audit(declared: list[str], changed: list[str]) -> list[str]:
    """Return every changed path not covered by the declared Write Scope."""
    raise NotImplementedError("skeleton — see .symphonia/guardrails/README.md")


if __name__ == "__main__":
    sys.exit("write_scope.py is a skeleton; not runnable yet")
