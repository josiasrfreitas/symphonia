#!/usr/bin/env python3
"""Review Budget guardrail (skeleton).

Measures changed lines of a finished Implementation Ticket against
`review_budget_lines` in `.symphonia/config.json` (default 400). Overflow
raises Needs Attention; the split proposal is agentic, the approval is human.

Real implementation has its own ticket. This file fixes the CLI shape only.
"""
import sys


def measure(diff_stat: str) -> int:
    """Return total changed lines from `git diff --stat` output."""
    raise NotImplementedError("skeleton — see .symphonia/guardrails/README.md")


if __name__ == "__main__":
    sys.exit("review_budget.py is a skeleton; not runnable yet")
