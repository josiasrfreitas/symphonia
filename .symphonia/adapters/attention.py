"""Structured Needs Attention codes.

TLDR: a Needs Attention flag carries a code from this enum plus a free-text
reason. Scripts branch on the code, never on substrings of the reason —
this replaces the prototype's substring matching (the GRE-153 note).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class AttentionCode(enum.Enum):
    """Why an Implementation Ticket stopped and needs the user.

    The first six come from Reconciliation (``ReconcileCode`` in the
    prototype); the rest are raised by guardrail scripts.
    """

    # Reconciliation findings
    TICKET_WITHOUT_WORKER = "ticket-without-worker"
    """In flight per the tracker, nothing alive for it in the runtime."""
    WORKER_WITHOUT_TICKET = "worker-without-ticket"
    """Alive in the runtime, at rest or at a gate per the tracker."""
    WORKER_WITHOUT_ITEM = "worker-without-item"
    """Alive in the runtime, but the tracker has never heard of the key."""
    WORKER_QUIET = "worker-quiet"
    """In flight, alive, and quiet past the threshold."""
    INTENT_UNRESOLVED = "intent-unresolved"
    """An intent recorded and never closed, with nothing running to close it."""
    STARTED_LOCKED = "started-locked"
    """In flight with a predecessor that has not merged."""

    # Guardrail findings
    WRITE_SCOPE_VIOLATION = "write-scope-violation"
    """The ticket's diff touched paths outside its declared Write Scope."""
    REVIEW_BUDGET_EXCEEDED = "review-budget-exceeded"
    """Changed lines exceed ``review_budget_lines``; a split needs approval."""
    TIER_UNVERIFIED = "tier-unverified"
    """The Capability Tier check could not confirm what the role ran at."""

    # Role reports, translated by the Orchestrator
    ROLE_REPORTED = "role-reported"
    """A role reported a blocker; the Orchestrator raised the flag for it."""


@dataclass(frozen=True)
class Attention:
    """The flag itself. ``needs=False`` means code and reason are empty."""

    needs: bool
    code: AttentionCode | None = None
    reason: str = ""
