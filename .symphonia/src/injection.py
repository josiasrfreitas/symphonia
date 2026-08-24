"""The one formatter for Context Injection refusals.

Context Injection (the glossary term in `CONTEXT.md`) is everything a
script hands an agent to read. A refusal is the hardest case: the agent
called something wrong and has to fix the call without a human in the
loop, so the text must teach, not just complain. The design (SYM-8,
comment of 2026-08-19) fixes one shape for every refusal the `map` tool
emits: what blocked it, what would be accepted, one example — plus the
kind, so a caller can tell an incomplete call from a rejected one without
reading prose.

This module is the only place that shape is written. Verbs raise
`Refused`; the `map` boundary renders it. Nothing here talks to a tracker,
a terminal, or the network.
"""
from __future__ import annotations

from dataclasses import dataclass

# The two kinds, coined here and imported everywhere else. A literal
# duplicated at the other end would not error — it would just never match.
INCOMPLETE = "incomplete"
"""The call was understood but is missing something: a verb, a required
parameter. The fix is to supply it and call again."""

REFUSED = "refused"
"""The call was understood and is not allowed: an unknown verb, a state
that forbids the operation. Supplying more of the same does not fix it."""

KINDS = (INCOMPLETE, REFUSED)


@dataclass(frozen=True)
class Refusal:
    """The four fields, all required. A formatter that accepts an empty
    field produces a refusal that teaches nothing — so emptiness is a
    programming error caught here, not a blank line an agent has to guess
    around."""

    blocked: str
    """What stopped the call."""

    accepted: str
    """What would be accepted instead."""

    example: str
    """One concrete call that would work."""

    kind: str
    """`INCOMPLETE` or `REFUSED`."""

    def __post_init__(self) -> None:
        for name in ("blocked", "accepted", "example"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"refusal field {name!r} is empty; a refusal must teach the fix")
        if self.kind not in KINDS:
            raise ValueError(f"unknown refusal kind {self.kind!r}; use one of {KINDS}")


class Refused(Exception):
    """Carries a `Refusal`. The only exception the `map` boundary turns
    into formatted output: anything else keeps its traceback, so a bug
    never disguises itself as a polite refusal."""

    def __init__(self, refusal: Refusal):
        super().__init__(refusal.blocked)
        self.refusal = refusal


def render(refusal: Refusal) -> str:
    """The four fields as text, in the order the design fixes."""

    return "\n".join([
        f"Blocked: {refusal.blocked}",
        f"Accepted: {refusal.accepted}",
        f"Example: {refusal.example}",
        f"Kind: {refusal.kind}",
    ])


def as_dict(refusal: Refusal) -> dict:
    """The same four fields, keyed — for `--json` callers and for tests
    that assert on a field rather than on a substring."""

    return {
        "blocked": refusal.blocked,
        "accepted": refusal.accepted,
        "example": refusal.example,
        "kind": refusal.kind,
    }
