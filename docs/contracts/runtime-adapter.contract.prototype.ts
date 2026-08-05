/**
 * PROTOTYPE — Runtime Adapter contract (GRE-150). Throwaway; react to it, don't build on it.
 *
 * The Runtime Adapter is the boundary through which the workflow creates, observes and
 * controls isolated execution contexts. It is the sibling of the Tracker Adapter: that one
 * owns what is written down, this one owns what is running.
 *
 * Three rules shape everything below:
 *
 *   1. Nothing provider-specific crosses this boundary. No Run, pane, terminal, worktree,
 *      Task, model name or effort value appears in any type here.
 *   2. Where the provider is weak, the contract makes the weakness unrepresentable rather
 *      than documented. There is no graceful stop. There is no way to accidentally reuse a
 *      context. There is no way to claim a tier was verified when the provider cannot say.
 *   3. The runtime never attests to work. It reports what it can observe about a process,
 *      and nothing about whether the work was any good. Judgement belongs to the reviewers
 *      and the deterministic gates, not here.
 *
 * Written against four settled decisions, none of which is reopened:
 *
 *   GRE-149  official orchestration is the control plane, one workspace per Implementation
 *            Ticket, one supervised unit per role step, a fresh context per role, and four
 *            defensive rules about the coordinator's control binding.
 *   GRE-161  a role declares a Capability Tier and never a model; every context is launched
 *            with an explicit command; there is no automatic escalation; the launched tier
 *            is verified by a script reading a field.
 *   GRE-163  that verification is a per-provider property — one provider proves what
 *            answered, the other only proves what was asked for.
 *   GRE-153  the shape of a sibling adapter, and the Ticket Key that ties running work back
 *            to the tracker.
 */

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

/**
 * The short, durable, human-visible tracker identifier — `ItemRef.key` from the Tracker
 * Adapter contract. It is required on every workspace and every role launched into one.
 *
 * Not a convenience. GRE-149 established that nothing in the runtime binds a running
 * process back to a tracker item, so this string travelling in the context's display
 * identity is the only thing that makes a stray process attributable to work.
 */
export type TicketKey = string;

/** An isolated checkout. One per Implementation Ticket, and never shared between two. */
export type WorkspaceRef = {
  id: string;
  ticketKey: TicketKey;
  path: string;
  branch: string;
};

/** A live Role Context. Opaque; the core addresses contexts only through this. */
export type ContextRef = {
  id: string;
  ticketKey: TicketKey;
  role: RoleName;
  access: Access;
};

/**
 * One supervised unit of work handed to one Role Context, and the fence that makes its
 * result attributable.
 *
 * This is deliberately one type where the provider has two. Orca separates the durable
 * task from the dispatch that runs it, and uses the pair as an anti-replay fence. In the
 * confirmed lifecycle a role step is dispatched exactly once — a correction is a new step,
 * a retry is a new step — so the pair never varies independently and one id fences just as
 * well. Collapsing them is the single largest simplification this contract makes.
 */
export type AttemptRef = {
  id: string;
  ticketKey: TicketKey;
  role: RoleName;
  /** Set when this attempt supersedes an earlier one. Records intent; inherits nothing. */
  supersedes: string | null;
};

// ---------------------------------------------------------------------------
// Roles and capability
// ---------------------------------------------------------------------------

export type RoleName = "planner" | "implementer" | "spec-reviewer" | "standards-reviewer";

/** GRE-161. The core names a tier; the adapter owns the tier-to-command translation. */
export type CapabilityTier = "high" | "standard" | "fast";

/**
 * Whether a role may write into the workspace.
 *
 * The runtime cannot detect two agents writing the same checkout, so the adapter enforces
 * a single writer per workspace. This is what makes the two reviewers legal in parallel
 * and a second implementer impossible — see the SINGLE WRITER guarantee below.
 */
export type Access = "write" | "read";

/**
 * What is known about the tier a context is actually running at.
 *
 * GRE-163 found the two pilot providers give different kinds of evidence, and GRE-150's
 * recorded user decision was to keep both and carry the gap rather than drop one. This
 * union is that decision expressed as a type: there is no way to construct `observed` on a
 * provider that cannot produce it, and no single field a caller could mistake for a
 * uniform guarantee.
 *
 *   requested   the launch string asked for this tier and the process started. Nothing
 *               says this is what answered. An unauthenticated session, an exhausted usage
 *               limit or a server-side substitution all look exactly like success.
 *   observed    read from a field the provider recorded *after* answering. This is the
 *               only strength that proves anything about the work that was done.
 *   unavailable the check could not be run at all. Never silently downgraded to
 *               `requested` — not knowing and knowing-only-the-request are different
 *               facts, and the tuning loop compares them.
 */
export type TierEvidence =
  | { strength: "requested"; tier: CapabilityTier; at: string }
  | { strength: "observed"; tier: CapabilityTier; at: string }
  | { strength: "unavailable"; reason: string; at: string };

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/**
 * The result envelope. Note what is absent.
 *
 * There is no changed-file list, no test verdict, no "succeeded" that means the work is
 * good. Completion in this runtime is cooperative — the worker says it is done and the
 * runtime believes it — so every field a worker could assert about its own quality would
 * be an unverified claim wearing the runtime's authority. `outcome` is about the process,
 * not the product. Whether the work is acceptable is the reviewers' answer, and it arrives
 * through the tracker, not through here.
 *
 * `tierEvidence` rides on the envelope rather than being fetched separately because the
 * tuning loop compares executions after the fact, and an execution whose tier was never
 * confirmed has to be legible as such in its own record.
 */
export type ResultEvent = {
  kind: "result";
  attemptId: string;
  contextId: string;
  ticketKey: TicketKey;
  role: RoleName;
  /**
   * delivered  the role says it finished its step.
   * declined   the role says it cannot do the step — bad brief, missing dependency.
   * failed     the role stopped on an error it reported.
   */
  outcome: "delivered" | "declined" | "failed";
  summary: string;
  tierEvidence: TierEvidence;
  at: string;
};

/**
 * A worker asking the human something, through the coordinator.
 *
 * `token` is the only way to answer, and it is single-use. The provider leaves an
 * unanswered question pending, resumable only by its original identity and never
 * re-askable, so a question that is dropped is a thread the user has to reconcile from
 * memory. That is why the core serializes Human Gates — a policy this contract does not
 * own but does have to make expressible.
 */
export type QuestionEvent = {
  kind: "question";
  attemptId: string;
  ticketKey: TicketKey;
  token: string;
  body: string;
  at: string;
};

/** A worker reporting it is stuck in a way it believes needs a decision above it. */
export type EscalationEvent = {
  kind: "escalation";
  attemptId: string;
  ticketKey: TicketKey;
  body: string;
  at: string;
};

/**
 * The adapter reporting that its own control binding was taken and reclaimed.
 *
 * GRE-160 found any actor can silently seize the coordinator's binding, and GRE-149 made
 * reclaiming it mandatory before every operation. Reclaiming quietly would hide a
 * violation of a rule the whole system rests on — no worker may touch the control binding
 * — so the reclaim is automatic and the event is not suppressible.
 */
export type ControlLostEvent = {
  kind: "control-lost";
  observedHolder: string | null;
  reclaimed: boolean;
  at: string;
};

export type RuntimeEvent = ResultEvent | QuestionEvent | EscalationEvent | ControlLostEvent;

export type EventKind = RuntimeEvent["kind"];

/**
 * One drain of the coordinator's mailbox.
 *
 * `truncated` exists because the provider caps a batch and parallel work overruns it.
 * A caller that drains a full batch and waits for a timer has already fallen behind.
 */
export type EventBatch = {
  events: RuntimeEvent[];
  truncated: boolean;
  /** Pass to `ack`. Events stay pending — and are redelivered — until acknowledged. */
  receipt: string;
};

// ---------------------------------------------------------------------------
// Liveness
// ---------------------------------------------------------------------------

/**
 * What a sweep can tell about an outstanding attempt.
 *
 * This type exists because the runtime has no reaper and completion is cooperative: a
 * worker that finishes without sending a result sits outstanding forever, and with two
 * Implementation Tickets in flight nobody notices. Heartbeats do not help — they fill the
 * mailbox and prove only that a process is alive, which is exactly what is not in doubt.
 *
 *   running       producing output; leave it alone.
 *   idle          alive and quiet with no result. The silent finisher, or a wedged agent.
 *                 Indistinguishable from outside, which is why this is one state and not
 *                 two — the adapter must not guess.
 *   gone          the context no longer exists and no result ever arrived.
 */
export type Liveness = "running" | "idle" | "gone";

export type AttemptStatus = {
  attemptId: string;
  ticketKey: TicketKey;
  role: RoleName;
  liveness: Liveness;
  /** Milliseconds since the context last produced output. Null when `gone`. */
  quietFor: number | null;
};

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

/**
 * The little that genuinely varies between runtimes. Everything else the adapter emulates.
 *
 * `tierVerification` is the load-bearing one and the reason this record exists at all: it
 * lets the core decide what to record about an execution before launching anything, rather
 * than discovering after the fact that a provider cannot answer.
 */
export type RuntimeCapabilities = {
  /** The strongest evidence this provider can ever produce about a launched tier. */
  tierVerification: "observed" | "requested";
  /** Tiers the adapter's table can translate. A tier outside this fails at load. */
  tiers: CapabilityTier[];
  /**
   * True when a workspace can run a setup step before any role enters it. False means the
   * core must treat the first role's environment as unprepared.
   */
  workspaceSetup: boolean;
};

// ---------------------------------------------------------------------------
// Launch
// ---------------------------------------------------------------------------

export type RoleSpec = {
  role: RoleName;
  tier: CapabilityTier;
  access: Access;
  /**
   * The standing instructions the context is born with — the role's charter, and the rules
   * that keep a worker from touching the control plane.
   *
   * Mandatory, because the adapter appends to it: a correlation marker without which a
   * running context cannot be tied back to its attempt on providers that expose no process
   * identity, and the prohibition on a worker addressing the coordinator's control binding
   * or the human directly.
   */
  charter: string;
};

/**
 * What a launch returns.
 *
 * `tierEvidence` here is always `requested` or `unavailable` — never `observed`. A context
 * that has not answered anything has recorded nothing, so the strong check is necessarily
 * a second call after the first dispatch. Returning the weak evidence anyway makes the gap
 * visible at the moment it opens rather than leaving the field absent.
 */
export type LaunchResult = {
  context: ContextRef;
  tierEvidence: TierEvidence;
};

/** What a kill leaves behind. There is no field that could say the workspace is clean. */
export type KillReceipt = {
  attemptId: string;
  /**
   * Always true. Stopping a role is a kill — there is no cooperative shutdown to ask for —
   * so the workspace may hold a half-written edit and the core must treat it as suspect.
   * A boolean that is always true is a deliberate shape: it forces the fact into every
   * call site instead of into a comment nobody reads.
   */
  workspaceLeftDirty: true;
};

// ---------------------------------------------------------------------------
// The contract
// ---------------------------------------------------------------------------

export interface RuntimeAdapter {
  readonly capabilities: RuntimeCapabilities;

  // --- workspaces ---

  /**
   * Create the isolated workspace for an Implementation Ticket and prepare it.
   *
   * Resolves only when setup has finished and any process the provider opened
   * incidentally has been closed. That ordering is the whole reason this is one operation
   * rather than create-then-wait: GRE-161 replaced a runtime startup guarantee with an
   * explicit rule that roles enter a prepared workspace, and a contract that let a caller
   * launch into an unprepared one would have made the rule optional.
   */
  createWorkspace(ticketKey: TicketKey, opts: { branch: string }): Promise<WorkspaceRef>;

  /**
   * Remove the workspace. Refuses while any context is live, and offers no override.
   *
   * The runtime performs no process or filesystem action when supervision is dropped, so
   * an abandoned role may still be writing here. Automatic deletion on the unhappy path
   * would destroy work mid-flight; the core removes a workspace when its pull request
   * merges, and otherwise asks the user.
   */
  destroyWorkspace(workspace: WorkspaceRef): Promise<void>;

  // --- role contexts ---

  /**
   * Open a fresh Role Context in a prepared workspace.
   *
   * There is no parameter that points at an existing context, and that absence is the
   * point. The provider's reuse operation does not create a context — it types into the
   * session that is already there — so a Planner, Implementer and Reviewer expressed as
   * three reuses of one handle produce one contaminated context that believes it is all
   * three. Here creating and reusing are different functions with different arguments and
   * neither can be mistaken for the other.
   */
  launchRole(workspace: WorkspaceRef, spec: RoleSpec): Promise<LaunchResult>;

  /**
   * Read the strongest tier evidence the provider can currently produce.
   *
   * Callable at any time; the answer strengthens from `requested` to `observed` only after
   * the context has answered something, and only where the provider records what answered.
   * Poll rather than sleep — the field appears a few seconds after the first dispatch —
   * and never treat a missing field as a default.
   */
  verifyTier(context: ContextRef): Promise<TierEvidence>;

  /**
   * Close a context. Its workspace and everything written into it survive.
   *
   * The Implementer's context is deliberately kept open through review, because
   * corrections are dispatched back into it. Closing it early is legal and irreversible,
   * and costs the correction loop its context.
   */
  closeRole(context: ContextRef): Promise<void>;

  // --- attempts ---

  /**
   * Hand a role step to a context and start supervising it.
   *
   * One dispatch per attempt. A correction round is a new attempt into the same context; a
   * retry is a new attempt into a new context, with `supersedes` set. Neither inherits
   * anything from the attempt it follows — placement, tier and instructions are all stated
   * again — because a retry that quietly inherits is a retry nobody can read afterwards.
   */
  dispatch(
    context: ContextRef,
    instruction: { body: string; supersedes?: AttemptRef },
  ): Promise<AttemptRef>;

  /**
   * Kill an attempt's context.
   *
   * The only way to stop work. There is no graceful variant to reach for, because the
   * runtime has none and a contract that offered one would be promising something no
   * adapter could keep.
   */
  kill(attempt: AttemptRef): Promise<KillReceipt>;

  // --- observation ---

  /**
   * Take everything pending of the given kinds.
   *
   * `kinds` is required. An unfiltered drain fills its batch with noise from parallel
   * workers and silently drops what the coordinator was actually waiting for.
   */
  drain(kinds: EventKind[]): Promise<EventBatch>;

  /** Acknowledge a drained batch. Until this lands, those events are redelivered. */
  ack(receipt: string): Promise<void>;

  /** Answer a pending question. The token is single-use; answering twice is an error. */
  respond(token: string, body: string): Promise<void>;

  /**
   * The liveness of every outstanding attempt.
   *
   * Not derived from the event stream, and deliberately a separate call from `drain`:
   * the failure it exists to catch is the absence of an event, which no amount of reading
   * the stream will reveal. A coordinator that only drains will wait forever on a worker
   * that finished quietly.
   */
  sweep(): Promise<AttemptStatus[]>;
}

// ---------------------------------------------------------------------------
// Guarantees the adapter owes the core
// ---------------------------------------------------------------------------

/**
 * 1. FRESH BY CONSTRUCTION. `launchRole` never returns a context that has seen another
 *    role's instructions. If the provider cannot open a clean one, it fails.
 *
 * 2. SINGLE WRITER. At most one `access: "write"` context per workspace at a time. The
 *    second is refused, loudly. The runtime cannot detect a working-tree collision, so
 *    this is the only place it can be prevented.
 *
 * 3. PREPARED WORKSPACE. `createWorkspace` resolves only after setup completes and any
 *    incidental process is closed. No role can enter an unprepared workspace because no
 *    caller can name one.
 *
 * 4. HONEST EVIDENCE. `observed` is returned only after reading a field the provider wrote
 *    once it had answered. A provider that cannot do this returns `requested` forever, and
 *    says so upfront in `capabilities.tierVerification`.
 *
 * 5. CORRELATION EVERYWHERE. The Ticket Key and the attempt identity appear in every
 *    context's display identity and in its charter. A stray process is always attributable
 *    to the work it was doing, without depending on a process id the provider may not
 *    expose.
 *
 * 6. FENCED RESULTS. A result whose attempt has been superseded or killed is dropped, not
 *    delivered. Anything else lets a zombie complete a step it is no longer doing.
 *
 * 7. COORDINATOR-ONLY CONTROL. The adapter has no serializable form and is never handed to
 *    a Role Context. Every operation re-establishes the coordinator's control binding
 *    first, reclaims it if it was taken, and emits `control-lost` — which the core may
 *    read but the adapter may not suppress.
 *
 * 8. AT-LEAST-ONCE EVENTS. Unacknowledged events are redelivered, so the core must be
 *    idempotent per attempt. Acknowledging before the event is durably recorded loses it.
 */

// ---------------------------------------------------------------------------
// Core-side logic — deliberately not in the adapter
// ---------------------------------------------------------------------------

/**
 * The accepted debt, made visible where it is incurred.
 *
 * GRE-150's recorded decision keeps a provider that cannot confirm which tier answered.
 * The price is specific: an unauthenticated session, an exhausted usage limit, or a
 * server-side substitution runs to completion at a tier nobody chose and nothing notices.
 * This turns that into a line the core writes onto the Implementation Ticket, so an
 * execution carries its own uncertainty and the tuning loop stays honest.
 *
 * Pure, and outside the adapter, because it is a statement about what the workflow
 * chose to accept — not a property of any runtime.
 */
export function unverifiedTierNote(result: ResultEvent): string | null {
  const e = result.tierEvidence;
  if (e.strength === "observed") return null;
  if (e.strength === "requested") {
    return `${result.ticketKey} ${result.role}: tier "${e.tier}" was requested and launched, but this runtime cannot confirm which tier answered. Compare this execution with that in mind.`;
  }
  return `${result.ticketKey} ${result.role}: the tier check could not run (${e.reason}). The tier this execution ran at is unknown.`;
}

/**
 * Attempts the coordinator is waiting on that will never arrive on their own.
 *
 * The rule is deliberately dumb — quiet for longer than the threshold, no result — because
 * `idle` cannot distinguish a worker that finished silently from one that is wedged, and
 * a cleverer rule would be inventing a distinction the runtime does not offer. What it
 * produces is a list to show a human, not an action to take.
 */
export function needsIntervention(
  statuses: AttemptStatus[],
  quietThresholdMs: number,
): AttemptStatus[] {
  return statuses.filter((s) => {
    if (s.liveness === "gone") return true;
    if (s.liveness === "running") return false;
    return (s.quietFor ?? 0) >= quietThresholdMs;
  });
}

// ---------------------------------------------------------------------------
// Left to other tickets, on purpose
// ---------------------------------------------------------------------------

/**
 * - Which side wins when the runtime and the tracker disagree about where a ticket stands,
 *   and how a coordinator that restarts discovers work it did not launch. That is "Define
 *   progress authority, discovery, and reconciliation" (GRE-155), which this ticket blocks.
 *   The contract exposes the pieces — fenced attempts, at-least-once events, a sweep that
 *   reports what the stream cannot — and decides no policy over them.
 *
 * - The concrete tier table, escalation thresholds, and which role gets which tier. The
 *   map's fog holds these until the pilot produces evidence.
 *
 * - How many Implementation Tickets run at once and how Human Gates are serialized. Core
 *   policy from GRE-149; the adapter enforces only the single-writer rule per workspace.
 */
