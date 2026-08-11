"""Runtime Adapter contract.

TLDR: the provider-neutral boundary through which the workflow creates,
observes, and controls isolated execution contexts. No provider concept
(Orca, terminal, model name, effort value) appears here — roles declare a
Capability Tier and the adapter translates it. Specification:
``docs/contracts/runtime-adapter.contract.prototype.ts``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal, Protocol, Union

TicketKey = str


class RoleName(enum.Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    SPEC_REVIEWER = "spec-reviewer"
    STANDARDS_REVIEWER = "standards-reviewer"


class CapabilityTier(enum.Enum):
    """Abstract level of model capability a role declares. The adapter —
    never the role — translates it into a concrete model and effort."""

    FRONTIER = "frontier"
    HIGH = "high"
    STANDARD = "standard"
    FAST = "fast"


class Access(enum.Enum):
    WRITE = "write"
    READ = "read"


@dataclass(frozen=True)
class RolePolicy:
    """What one role is allowed to run at and touch — core vocabulary, not a
    harness detail. ``workflow.roles.load_policies`` is the only thing that
    reads it off disk; this is just the shape (GRE-186 S1, relocated here in
    the same round's correction round so ``adapters/`` never has to import
    ``workflow/`` to know it)."""

    role: RoleName
    tier: CapabilityTier
    access: Access
    role_file: str


class Liveness(enum.Enum):
    """Completion is cooperative — the runtime has no reaper."""

    RUNNING = "running"
    IDLE = "idle"
    GONE = "gone"


@dataclass(frozen=True)
class WorkspaceRef:
    """The isolated checkout owned by exactly one Implementation Ticket.

    ``id`` is the provider's own identifier for the worktree (GRE-184 M2) —
    composed verbs address the terminal/badge calls by ``id:<id>``, never by
    ``path:``, because the display name changes with the phase while the id
    does not."""

    ticket_key: TicketKey
    id: str
    path: str
    branch: str


@dataclass(frozen=True)
class ContextRef:
    """One Role Context: a fresh, uncontaminated agent session. Deliberately
    one type where the provider has two (durable terminal + session)."""

    id: str
    role: RoleName
    workspace: WorkspaceRef


@dataclass(frozen=True)
class AttemptRef:
    """One supervised unit of work handed to one Role Context — the fence
    that makes a result attributable. A retry is a new Attempt."""

    attempt_id: str
    ticket_key: TicketKey
    context: ContextRef


TierEvidenceKind = Literal["requested", "observed", "unverifiable"]


@dataclass(frozen=True)
class TierEvidence:
    """What is actually known about the tier a Role Context ran at."""

    kind: TierEvidenceKind
    tier: CapabilityTier | None = None
    detail: str = ""


@dataclass(frozen=True)
class RoleSpec:
    role: RoleName
    tier: CapabilityTier
    access: Access
    # Kept on purpose (GRE-184 M3 review): no caller reads this back. The
    # brief a role actually receives arrives as the body of its first
    # `dispatch` — there is no second channel. GRE-186 (S5) rewrites this
    # whole boundary; removing the field belongs there, not here.
    briefing: str


@dataclass(frozen=True)
class ResultEvent:
    kind: Literal["result"]
    attempt: AttemptRef
    outcome: Literal["succeeded", "failed"]
    summary: str


@dataclass(frozen=True)
class QuestionEvent:
    kind: Literal["question"]
    attempt: AttemptRef
    token: str
    question: str


@dataclass(frozen=True)
class EscalationEvent:
    kind: Literal["escalation"]
    attempt: AttemptRef
    reason: str


@dataclass(frozen=True)
class ControlLostEvent:
    kind: Literal["control-lost"]
    attempt: AttemptRef
    detail: str


RuntimeEvent = Union[ResultEvent, QuestionEvent, EscalationEvent, ControlLostEvent]
EventKind = Literal["result", "question", "escalation", "control-lost"]


@dataclass(frozen=True)
class EventBatch:
    """Events plus a receipt; nothing is consumed until the receipt is acked."""

    events: tuple[RuntimeEvent, ...]
    receipt: str


@dataclass(frozen=True)
class AttemptStatus:
    attempt_id: str
    ticket_key: TicketKey
    role: RoleName
    liveness: Liveness
    quiet_for_ms: int | None = None


@dataclass(frozen=True)
class LaunchResult:
    context: ContextRef
    tier_evidence: TierEvidence


@dataclass(frozen=True)
class KillReceipt:
    attempt: AttemptRef
    confirmed_gone: bool


@dataclass(frozen=True)
class RuntimeCapabilities:
    tier_verification: bool
    cooperative_completion: bool


class RuntimeAdapter(Protocol):
    """Everything the workflow may ask of a runtime. Nothing else."""

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    # Workspaces
    def create_workspace(self, ticket_key: TicketKey, *, base_branch: str) -> WorkspaceRef: ...
    def find_workspace(self, ticket_key: TicketKey) -> WorkspaceRef | None: ...
    def destroy_workspace(self, workspace: WorkspaceRef) -> None: ...

    # Role Contexts
    def launch_role(self, workspace: WorkspaceRef, spec: RoleSpec) -> LaunchResult: ...
    def verify_tier(self, context: ContextRef) -> TierEvidence: ...
    def close_role(self, context: ContextRef) -> None: ...

    # Attempts
    def dispatch(self, context: ContextRef, work: str) -> AttemptRef: ...
    def kill(self, attempt: AttemptRef) -> KillReceipt: ...

    # Events (drain + ack: nothing is lost between crashes)
    def drain(self, kinds: list[EventKind]) -> EventBatch: ...
    def ack(self, receipt: str) -> None: ...
    def respond(self, token: str, body: str) -> None: ...

    # Observation
    def sweep(self) -> list[AttemptStatus]: ...
