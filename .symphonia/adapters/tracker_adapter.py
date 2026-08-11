"""Tracker Adapter contract.

TLDR: the provider-neutral boundary through which the workflow creates,
relates, queries, and updates canonical tracker artifacts and mutable
delivery state. No provider concept (Linear, workspace, team, project)
appears here. Specification: ``docs/contracts/tracker-adapter.contract.prototype.ts``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol

from .attention import Attention

TicketKey = str
"""The short, durable, human-visible identifier (e.g. ``GRE-170``)."""

ActorId = str


class ItemKind(enum.Enum):
    DECISION_MAP = "decision-map"
    DECISION_TICKET = "decision-ticket"
    IMPLEMENTATION_TICKET = "implementation-ticket"


class DecisionType(enum.Enum):
    RESEARCH = "research"
    PROTOTYPE = "prototype"
    GRILLING = "grilling"
    TASK = "task"


class Openness(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class DeliveryPhase(enum.Enum):
    """Where an Implementation Ticket stands between its brief and its merge.

    The tracker holds it and reads it back unchanged. Human Gates are
    detected by reading this field — deterministic, never inferred.
    """

    BRIEFED = "briefed"
    PLANNING = "planning"
    PLAN_GATE = "plan-gate"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    MERGE_GATE = "merge-gate"
    MERGED = "merged"


class CloseOutcome(enum.Enum):
    DONE = "done"
    CANCELED = "canceled"


@dataclass(frozen=True)
class ItemRef:
    id: str
    key: TicketKey
    url: str


@dataclass(frozen=True)
class Delivery:
    """The mutable delivery state of an Implementation Ticket."""

    phase: DeliveryPhase
    attention: Attention
    branch: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class Item:
    ref: ItemRef
    kind: ItemKind
    title: str
    body: str
    openness: Openness
    assignee: ActorId | None = None
    decision_type: DecisionType | None = None
    delivery: Delivery | None = None
    blocked_by: tuple[ItemRef, ...] = ()


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    author: ActorId
    author_name: str
    created_at: str
    """``created_at`` is ISO-8601, as the provider returns it."""


@dataclass(frozen=True)
class Artifact:
    """A pointer to a canonical long-form artifact the tracker stores."""

    id: str
    title: str
    url: str


@dataclass(frozen=True)
class BodyOp:
    """One anchored patch operation against an item body.

    Anchored patching is the mitigation for the tracker's lack of optimistic
    locking: the anchor must match exactly once, so a stale patch fails
    loudly instead of clobbering (GRE-152). Bodies are never rewritten whole.
    """

    op: str  # "insert_before" | "insert_after" | "replace"
    anchor: str
    text: str


@dataclass(frozen=True)
class ClaimResult:
    """Claim with verified re-read: ``held`` is true only if the re-read
    shows this actor as assignee (there is no compare-and-swap upstream)."""

    held: bool
    holder: ActorId | None


@dataclass(frozen=True)
class TrackerCapabilities:
    anchored_patch: bool
    artifact_read_back: bool


class TrackerAdapter(Protocol):
    """Everything the workflow may ask of a tracker. Nothing else."""

    @property
    def capabilities(self) -> TrackerCapabilities: ...

    # Reading
    def get_item(self, id: str, *, with_relations: bool = False) -> Item: ...
    def list_children(self, map_id: str, *, with_relations: bool = False) -> list[Item]: ...
    def list_needing_attention(self, map_id: str) -> list[Item]: ...
    def list_comments(self, id: str) -> list[Comment]: ...
    def list_artifacts(self, id: str) -> list[Artifact]: ...
    def read_artifact(self, artifact: Artifact) -> str: ...

    # Structure
    def create_child(
        self,
        map_id: str,
        kind: ItemKind,
        title: str,
        body: str,
        *,
        decision_type: DecisionType | None = None,
    ) -> ItemRef: ...
    def add_blocker(self, id: str, blocker_id: str) -> None: ...
    def remove_blocker(self, id: str, blocker_id: str) -> None: ...
    def patch_body(self, id: str, ops: list[BodyOp]) -> None: ...

    # Ownership and delivery state
    def claim(self, id: str, actor: ActorId) -> ClaimResult: ...
    def release(self, id: str, actor: ActorId) -> None: ...
    def close(self, id: str, outcome: CloseOutcome) -> None: ...
    def set_phase(self, id: str, phase: DeliveryPhase) -> None: ...
    def set_attention(self, id: str, attention: Attention) -> None: ...
    def set_delivery(self, id: str, **delivery: object) -> None: ...
    def set_gate(self, id: str, waiting: bool) -> None:
        """Put or lift the sign that a Human Gate is waiting. Human Gate is a
        workflow concept, not a provider one — every provider must expose it."""
        ...

    # Communication
    def post_comment(self, id: str, body: str) -> Comment: ...
    def post_resolution(self, id: str, *, tldr: str, body: str) -> Comment: ...
    def record_gate(self, ticket: str, gate: str, decision: str, evidence: str) -> Comment:
        """The tracker half of automatic gate recording: one templated
        comment on the ticket, TLDR first."""
        ...
    def attach_artifact(self, id: str, artifact: Artifact) -> None: ...

    # Rendering
    def render_ref(self, ref: ItemRef, label: str) -> str:
        """Render a link that does not create phantom relations (GRE-152)."""
        ...
