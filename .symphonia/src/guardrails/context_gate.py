#!/usr/bin/env python3
"""Context Budget gate (skeleton).

Runs from the Stop hook, starting at `stop_check_from_stop`. Reads the window
size recorded in the Workspace and reports the fraction of context used —
always fraction + window resolution, never absolute tokens (GRE-166). At
`context_gate_used_fraction` (default 0.40) the session must write a
`/handoff` and stop: new Attempt, same ticket, same Workspace, plan intact.

Builds on `docs/research/gre-166/context-left.py`. Real implementation has
its own ticket. This file fixes the CLI shape only.
"""
import sys


def used_fraction(transcript_path: str, window_size: int) -> float:
    """Return the fraction of the context window already used."""
    raise NotImplementedError("skeleton — see .symphonia/guardrails/README.md")


if __name__ == "__main__":
    sys.exit("context_gate.py is a skeleton; not runnable yet")
