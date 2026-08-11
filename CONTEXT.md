# Symphonia Workflow

This context defines the shared language for a provider-neutral development workflow that turns ambiguous work into explicit decisions and bounded implementation delivery.

## Language

**Destination**:
The observable state that marks the end of a Wayfinder map and fixes the effort's scope.
_Avoid_: Goal, epic, project

**Decision Map**:
The canonical Wayfinder issue that names the Destination, indexes resolved Decision Tickets, and records fog and excluded work.
_Avoid_: Decision graph, project plan

**Decision Ticket**:
A child issue that resolves one consequential question through research, prototyping, grilling, or prerequisite work.
_Avoid_: Work item, implementation task

**Frontier**:
The ordered set of open, unblocked, and unclaimed Decision Tickets that can be worked now.
_Avoid_: Backlog, queue

**Fog of War**:
In-scope uncertainty that is visible but cannot yet be phrased as a precise Decision Ticket.
_Avoid_: Backlog, open ticket

**Execution DAG**:
The canonical machine-readable graph of stable implementation topology and constraints produced after consequential decisions are resolved.
_Avoid_: Decision graph, status board

**Implementation Ticket**:
One independently reviewable delivery unit that owns one workspace, one branch, and at most one pull request, and that is as large as the Context Budget and the Review Budget allow.
_Avoid_: Decision Ticket, task packet

**Execution Brief**:
The intent, boundaries, dependencies, acceptance criteria, and global constraints supplied to an Implementation Ticket before local planning.
_Avoid_: Prompt, specification

**Local Technical Plan**:
The Planner's repository-grounded execution plan appended to an Implementation Ticket and approved before implementation begins.
_Avoid_: Global plan, task prompt

**Unlock**:
The one relationship the Execution DAG expresses: work is unlocked when everything it depends on has merged, and until then it may not start.
_Avoid_: Blocks, precedes, triggers

**Role Context**:
A fresh, uncontaminated agent session that carries out exactly one role inside one Implementation Ticket.
_Avoid_: Session, worker, pane

**Human Gate**:
A point in delivery where progress requires the user's judgement; gates are presented one at a time even when Implementation Tickets run in parallel.
_Avoid_: Approval step, blocker

**Capability Tier**:
The abstract level of model capability a role declares — high, standard, or fast — which an adapter translates into a concrete model and reasoning effort.
_Avoid_: Model, model name, effort level

**Runtime Adapter**:
The provider-neutral boundary through which the workflow creates, observes, and controls isolated execution contexts.
_Avoid_: Orca wrapper, terminal driver

**Tracker Adapter**:
The provider-neutral boundary through which the workflow creates, relates, queries, and updates canonical tracker artifacts and mutable delivery state.
_Avoid_: Linear client, issue helper

**Delivery Phase**:
Where an Implementation Ticket stands between its brief and its merge; the tracker holds it and reads it back unchanged.
_Avoid_: Status, state, column

**Ticket Key**:
The short, durable, human-visible identifier that travels outside the tracker and binds running work back to the ticket it belongs to.
_Avoid_: Id, issue number, slug

**Needs Attention**:
A flag on an Implementation Ticket, carrying a reason, that stops it and hands the next choice to the user; independent of its Delivery Phase.
_Avoid_: Blocked, failed, error state

**Workspace**:
The isolated checkout owned by exactly one Implementation Ticket, prepared before any role enters it and written by at most one role at a time.
_Avoid_: Worktree, checkout, directory

**Attempt**:
One supervised unit of work handed to one Role Context, and the fence that makes its result attributable; a correction or a retry is a new Attempt rather than a continuation of the old one.
_Avoid_: Task, dispatch, run

**Tier Evidence**:
What is actually known about the Capability Tier a Role Context ran at — that the tier was requested, that it was observed answering, or that the check could not run at all.
_Avoid_: Model check, verified flag

**Write Scope**:
Everything an Implementation Ticket is expected to write. Two tickets nothing orders may not share one, because neither the runtime nor the tracker can see the collision.
_Avoid_: Touched files, ownership, module

**Context Budget**:
How much work one Role Context can hold at once. Judged when the work is planned, never checked by a tool.
_Avoid_: Token limit, window, model memory

**Review Budget**:
How much change a person can review in one pass.
_Avoid_: Ticket size, story points

**Orchestrator**:
The single session that drives delivery — it creates Workspaces, opens Role Contexts, observes what they produce, and keeps the tracker as the readable picture of progress. It writes no code and decides nothing for the user.
_Avoid_: Coordinator, supervisor, main agent

**Reconciliation**:
Comparing what the tracker says about an Implementation Ticket with what the runtime says is running for it, and acting on the difference.
_Avoid_: Sync, refresh, healing

**Source**:
What a person writes and a review approves, and the package imports or executes — the Python under `src/`, entrypoints and tests included.
_Avoid_: Scripts, tooling, the bin

**Resource**:
Authored content the Source reads at run time but never executes — role contracts, skills, config, brief templates. Versioned and reviewed like Source; changing one changes behavior without changing code.
_Avoid_: Docs, assets, data files

**Artifact**:
Anything an execution produces — the spawn registry, a handoff baton, a transcript, an instantiated Execution DAG, a report. Never committed and never reviewed; it lives outside every checkout. A ticket attachment is an Artifact the tracker keeps.
_Avoid_: Output, state file, log
