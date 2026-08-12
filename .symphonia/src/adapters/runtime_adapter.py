"""Runtime Adapter contract.

TLDR: the provider-neutral boundary through which the workflow creates,
observes, and controls isolated execution contexts. No provider concept
(Orca, terminal, model name, effort value) appears here — a role's
Capability Tier is spent entirely inside the ``PreparedLaunch`` a
``HarnessAdapter`` (``harness_adapter.py``) builds before ``open_context``
is ever called (GRE-186 S3); this module knows only role and access.
Specification: ``docs/contracts/runtime-adapter.contract.prototype.ts``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Union

if TYPE_CHECKING:
    # Harness vocabulary the Protocol's signature needs (GRE-186 S3): the
    # runtime now takes a harness's ``PreparedLaunch`` instead of building
    # its own launch command. This is the one exception to "nothing in
    # runtime_adapter imports from harness_adapter" (`harness_adapter.py`'s
    # own docstring) — a `TYPE_CHECKING`-only import, invisible at runtime
    # (this module's own `from __future__ import annotations` makes every
    # annotation a lazy string), so the real import edge still runs one way:
    # `harness_adapter` -> `runtime_adapter`, never back.
    from .harness_adapter import PreparedLaunch

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
class KillReceipt:
    attempt: AttemptRef
    confirmed_gone: bool


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What the runtime itself promises. Tier evidence is a harness
    contract now, not a runtime one (GRE-186 S3) — the runtime stopped
    promising verification of a dial it never chose; a role's Capability
    Tier is spent entirely inside the launch a harness prepares, before
    ``open_context`` ever sees it."""

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
    def open_context(
        self, workspace: WorkspaceRef, *, role: RoleName, access: Access, launch: "PreparedLaunch"
    ) -> ContextRef:
        """Open a fresh Role Context, already launched. The runtime places
        it (workspace, badge/title) and enforces the single-writer guard;
        the launch itself — model, effort, permissions, session — was
        already decided by a ``HarnessAdapter.prepare()`` before this is
        called. The runtime never sees a Capability Tier: only ``role``
        (badge/title) and ``access`` (the writer guard) are its business."""
        ...

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
