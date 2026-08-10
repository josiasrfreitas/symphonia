/**
 * PROTOTYPE — Progress authority, discovery and reconciliation (GRE-155). Throwaway;
 * react to it, don't build on it.
 *
 * This is not an adapter. The two adapters are already drawn — "Choose the pilot tracker
 * and define the Tracker Adapter contract" (GRE-153) owns what is written down, "Define the
 * provider-neutral Runtime Adapter contract" (GRE-150) owns what is running — and both
 * deliberately left the policy over them undecided. This file is that policy: how the
 * Orchestrator discovers what is in flight, what it does when the two sides do not line up,
 * and what it is forbidden from doing about it.
 *
 * Four rules shape everything below:
 *
 *   1. AUTHORITY IS SPLIT BY QUESTION, NOT BY SYSTEM. The tracker answers *where this
 *      ticket stands*. The runtime answers *what is alive right now*. They are not two
 *      opinions about one fact, so there is no tie to break and no winner to declare.
 *   2. RUNTIME EVIDENCE NEVER MOVES A PHASE. Not forward, not back. A Delivery Phase moves
 *      when a workflow step actually completes. What runtime evidence may do is exactly one
 *      thing: raise Needs Attention with a reason.
 *   3. THE ORCHESTRATOR KEEPS NO DURABLE STATE OF ITS OWN. No file, no database, no list
 *      that survives the session. Every cycle rebuilds the whole picture from the tracker
 *      and the runtime, joined on the Ticket Key.
 *   4. WHATEVER IT WOULD OTHERWISE HAVE TO REMEMBER, IT WRITES TO THE TRACKER BEFORE
 *      ACTING. That is GRE-153's durable-first ordering applied to the Orchestrator's own
 *      intentions, and it is what makes rule 3 survivable.
 *
 * The payoff of 3 and 4 together is that *restart and find work I did not launch* stops
 * being a recovery mode and becomes the ordinary path. There is no emergency code that runs
 * rarely and is therefore never right — there is one cycle, and it does the same thing every
 * time. The side effect is the best part: what makes the Orchestrator restartable is the
 * same thing that makes it readable. It does not write to the tracker *and also* survive
 * restart. It survives restart *because* it writes to the tracker.
 */

import type { AttemptStatus, RoleName, TicketKey } from "./runtime-adapter.contract.prototype.ts";
import type { Attention, DeliveryPhase, ItemRef } from "./tracker-adapter.contract.prototype.ts";

// ---------------------------------------------------------------------------
// The two sides
// ---------------------------------------------------------------------------

/**
 * What the tracker says about one Implementation Ticket.
 *
 * Note what it does not contain: anything about processes. The tracker has never heard of
 * an attempt and must not learn — GRE-153 kept `delivery` down to workspace, branch and
 * pull request, all of which outlive any process that made them.
 */
export type TrackerSide = {
  ref: ItemRef;
  phase: DeliveryPhase;
  attention: Attention;
  /**
   * The intentions the Orchestrator recorded before acting on them, still open. This is the
   * whole of rule 4's storage, and it lives here rather than beside the Orchestrator on
   * purpose — see `Intent`.
   */
  openIntents: Intent[];
  /**
   * Whether the Execution DAG says this may be in flight at all: every predecessor merged.
   * Computed by GRE-154's graph, passed in rather than recomputed, because reconciliation
   * has no business reading topology.
   */
  unlocked: boolean;
};

/**
 * What the runtime says is alive for one Ticket Key — `sweep()` from GRE-150, grouped.
 *
 * `AttemptStatus` carries `liveness: "running" | "idle" | "gone"` and nothing finer, because
 * the runtime genuinely cannot tell a worker that finished silently from one that is wedged.
 * Nothing below tries to tell them apart either. A reconciler that guessed would be
 * inventing a distinction its own source refused to make.
 */
export type RuntimeSide = {
  ticketKey: TicketKey;
  attempts: AttemptStatus[];
};

/**
 * The one string the two sides join on.
 *
 * GRE-150 made the Ticket Key mandatory on every workspace, every context and every attempt
 * precisely so this join exists. No correspondence table is kept anywhere — and that is not
 * an optimisation, it is rule 3: a table mapping attempts to tickets would itself be durable
 * Orchestrator state, and the first thing to go stale after a crash.
 */
export type Join = { key: TicketKey; tracker: TrackerSide | null; runtime: RuntimeSide | null };

// ---------------------------------------------------------------------------
// Intent
// ---------------------------------------------------------------------------

/**
 * Something the Orchestrator is about to do, written down before it does it.
 *
 * This type exists because rule 3 has one genuine hole, and intent is all of it. Every other
 * fact the Orchestrator needs is observable from one of the two sides. Its own plans are
 * not: if it kills an attempt because it is about to relaunch, and dies between the kill and
 * the relaunch, the next Orchestrator wakes to a dead attempt with no successor and cannot
 * tell a deliberate kill from a worker falling over. The two call for opposite responses.
 *
 * So the plan is recorded on the ticket first. The successor does not have to infer intent,
 * because the intent is sitting there in the ticket's own history, in a form a human reading
 * the ticket understands without being told what it is.
 *
 * `closedAt` and not deletion, because GRE-153's body edits are append-only for a reason:
 * nothing here is compare-and-swap, and a delete race silently loses the record.
 */
export type Intent =
  | { kind: "killing"; attemptId: string; why: string; at: string; closedAt: string | null }
  | {
      kind: "relaunching";
      role: RoleName;
      supersedes: string | null;
      why: string;
      at: string;
      closedAt: string | null;
    }
  | {
      /**
       * A worker's question, recorded before the event that carried it was acknowledged.
       *
       * GRE-150's question tokens are single-use and unanswerable once dropped, and its
       * events are at-least-once — redelivered until acked. Recording first and acking
       * second means a crash in between costs a duplicate, not a question. Acking first
       * costs the question, and the user is left reconciling a half-finished thread from
       * memory.
       */
      kind: "question-pending";
      token: string;
      body: string;
      at: string;
      closedAt: string | null;
    };

/** The phases at which the workflow expects a live worker. Everything else is at rest. */
const IN_FLIGHT: ReadonlySet<DeliveryPhase> = new Set<DeliveryPhase>([
  "planning",
  "implementing",
  "reviewing",
]);

/**
 * Phases where a ticket is waiting on the user and no worker should be running.
 *
 * Kept separate from "at rest" because a live worker means something different here: at
 * `awaiting-plan-approval` a still-running Planner is a worker that never stopped, which is
 * a different sentence to show the user than a stray worker on a merged ticket.
 */
const AT_A_GATE: ReadonlySet<DeliveryPhase> = new Set<DeliveryPhase>([
  "awaiting-plan-approval",
  "awaiting-merge",
]);

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

/**
 * Stable codes, in the style of GRE-154's validator, and for the same reason: the first
 * consumer of a finding is a script, and a script keyed on a message string breaks the day
 * someone improves the wording. Retired codes are never reused.
 */
export type ReconcileCode =
  /** In flight per the tracker, nothing alive for it in the runtime. */
  | "TICKET_WITHOUT_WORKER"
  /** Alive in the runtime, at rest or at a gate per the tracker. */
  | "WORKER_WITHOUT_TICKET"
  /** Alive in the runtime, but the tracker has never heard of the key at all. */
  | "WORKER_WITHOUT_ITEM"
  /** In flight, alive, and quiet past the threshold. Finished silently, or wedged. */
  | "WORKER_QUIET"
  /** An intent recorded and never closed, with nothing running that would close it. */
  | "INTENT_UNRESOLVED"
  /** In flight with a predecessor that has not merged. */
  | "STARTED_LOCKED";

export type ReconcileFinding = {
  code: ReconcileCode;
  key: TicketKey;
  /** The sentence the user reads. Written for someone who has not been watching. */
  reason: string;
  /** Attempts implicated, for the transcript trail. Empty when the finding is about absence. */
  attemptIds: string[];
};

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

/**
 * Everything reconciliation is permitted to do. Read the union, then read what is missing
 * from it.
 *
 * There is no `set-phase`. There is no `kill`, no `relaunch`, no `close`, no
 * `destroy-workspace`. That absence is rule 2 made unrepresentable rather than documented:
 * a reconciler that cannot express a phase change cannot be argued into one by a future
 * caller who is sure they know what the `idle` meant.
 *
 * The Orchestrator does of course advance phases — when a Planner delivers, planning is
 * over. That is the workflow acting on a step that completed, and it does not come through
 * here. Reconciliation is the other path: the one driven by evidence about processes, which
 * is exactly the evidence that may not move a phase.
 */
export type ReconcileAction =
  | {
      /** GRE-153's `setAttention`. The only outward effect this function has. */
      kind: "raise-attention";
      key: TicketKey;
      reason: string;
      finding: ReconcileCode;
    }
  | {
      /**
       * Clear an attention this reconciler raised whose cause is gone — the worker came
       * back, the intent closed. Not for attentions raised by anything else: a reviewer's
       * rejection is nobody's to clear but the user's, and reconciliation cannot tell it
       * apart from its own except by the `finding` it carries.
       */
      kind: "clear-attention";
      key: TicketKey;
      was: ReconcileCode;
    };

// ---------------------------------------------------------------------------
// The cycle
// ---------------------------------------------------------------------------

/**
 * Proof that the mailbox was drained and acknowledged this cycle.
 *
 * Reconciliation runs after every drain and never on a clock, and this token is that rule
 * expressed as a type: there is no way to call `reconcile` without having drained first,
 * because there is no other way to obtain one of these.
 *
 * Why it matters. The Orchestrator's whole life is: open the mailbox, read what is there,
 * act, open it again. But one message never arrives — the one from a worker that died. An
 * empty mailbox means either "nobody has anything to say" or "it died and never will", and
 * from the mailbox alone those are the same. Reconciliation is the second question, *who is
 * still alive?*, and it is only meaningful once the first has been asked and answered.
 *
 * The alternative — a separate timer, every few minutes, independent of the drain — was
 * rejected because it creates a second moment at which the Orchestrator writes the tracker,
 * and nothing stops the two moments from landing on the same ticket at once. Draining first
 * means there is one moment, and it is the moment at which a missing message means something.
 */
export type MailboxDrained = { readonly receipt: string; readonly acked: true };

export type ReconcileInput = {
  /** The witness. Present for its type, not its contents. */
  drained: MailboxDrained;
  /** Every Implementation Ticket the tracker holds open, read fresh this cycle. */
  tracker: TrackerSide[];
  /** Every outstanding attempt the runtime reports, read fresh this cycle. */
  runtime: AttemptStatus[];
  /** Quiet-for threshold, in milliseconds. GRE-150's `needsIntervention` threshold. */
  quietThresholdMs: number;
  /** The clock, passed in. Nothing here reads it, so the same inputs always reconcile the same. */
  at: string;
};

export type ReconcileOutput = {
  findings: ReconcileFinding[];
  /** Findings not already represented on the ticket, plus clears for causes that are gone. */
  actions: ReconcileAction[];
};

// ---------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------

/**
 * Pure, total, and with no parameter for "what I saw last time".
 *
 * The missing parameter is rule 3 enforced at the signature: a reconciler that cannot be
 * handed its own history cannot come to depend on one. A restart is not a special case
 * because there is no case — the function has never had a memory to lose.
 */
export function reconcile(input: ReconcileInput): ReconcileOutput {
  const findings: ReconcileFinding[] = [];

  const byKey = new Map<TicketKey, AttemptStatus[]>();
  for (const a of input.runtime) {
    byKey.set(a.ticketKey, [...(byKey.get(a.ticketKey) ?? []), a]);
  }
  const known = new Set(input.tracker.map((t) => t.ref.key));

  for (const t of input.tracker) {
    const attempts = (byKey.get(t.ref.key) ?? []).filter((a) => a.liveness !== "gone");
    const inFlight = IN_FLIGHT.has(t.phase);
    const atGate = AT_A_GATE.has(t.phase);

    if (inFlight && !t.unlocked) {
      // Reported, never repaired. Killing live work over a graph edge is destructive, and
      // GRE-150 already refused automatic workspace deletion on the unhappy path for the
      // same reason: the Orchestrator cannot see what is half-written underneath it.
      findings.push({
        code: "STARTED_LOCKED",
        key: t.ref.key,
        reason: `${t.ref.key} is ${t.phase} but the Execution DAG says something it depends on has not merged. Nothing was stopped.`,
        attemptIds: attempts.map((a) => a.attemptId),
      });
    }

    if (inFlight && attempts.length === 0) {
      findings.push({
        code: "TICKET_WITHOUT_WORKER",
        key: t.ref.key,
        reason: `${t.ref.key} is ${t.phase} but nothing is running for it. The phase was left where it was.`,
        attemptIds: [],
      });
    }

    if (!inFlight && attempts.length > 0) {
      findings.push({
        code: "WORKER_WITHOUT_TICKET",
        key: t.ref.key,
        reason: atGate
          ? `${t.ref.key} is waiting at ${t.phase} but ${attempts.length} context(s) are still running for it.`
          : `${t.ref.key} is ${t.phase} and should be at rest, but ${attempts.length} context(s) are still running for it.`,
        attemptIds: attempts.map((a) => a.attemptId),
      });
    }

    // The silent finisher and the wedged worker, together in one finding because the
    // runtime reports them as one state and there is nothing here that could split them.
    const quiet = attempts.filter(
      (a) => a.liveness === "idle" && (a.quietFor ?? 0) >= input.quietThresholdMs,
    );
    if (inFlight && quiet.length > 0) {
      findings.push({
        code: "WORKER_QUIET",
        key: t.ref.key,
        reason: `${t.ref.key}: ${quiet.map((a) => a.role).join(", ")} alive and quiet for over ${Math.round(input.quietThresholdMs / 60000)} minutes with no result. It may have finished without saying so, or it may be stuck.`,
        attemptIds: quiet.map((a) => a.attemptId),
      });
    }

    // A half-finished action, seen from the far side of a crash. The intent says what was
    // meant; the runtime says it did not happen.
    for (const intent of t.openIntents) {
      if (intent.closedAt !== null) continue;
      if (intentStillCovered(intent, attempts)) continue;
      findings.push({
        code: "INTENT_UNRESOLVED",
        key: t.ref.key,
        reason: describeUnresolved(t.ref.key, intent),
        attemptIds: intent.kind === "killing" ? [intent.attemptId] : [],
      });
    }
  }

  // Work nobody in the tracker claims. This is what a restart into a running system looks
  // like when the tracker has been pruned, or when a key was mistyped into a launch — and it
  // is the one finding whose ticket cannot be flagged, because there is no ticket.
  for (const [key, attempts] of byKey) {
    if (known.has(key)) continue;
    const live = attempts.filter((a) => a.liveness !== "gone");
    if (live.length === 0) continue;
    findings.push({
      code: "WORKER_WITHOUT_ITEM",
      key,
      reason: `${live.length} context(s) are running under "${key}", which is not an open ticket. Nothing was stopped.`,
      attemptIds: live.map((a) => a.attemptId),
    });
  }

  return { findings, actions: toActions(findings, input.tracker) };
}

/**
 * Does the runtime still show what this intent was about?
 *
 * `relaunching` is the interesting one: it is satisfied by *any* live attempt for the role,
 * because the successor is a new attempt with a new id that the dead Orchestrator never saw
 * and could not have written down.
 */
function intentStillCovered(intent: Intent, attempts: AttemptStatus[]): boolean {
  switch (intent.kind) {
    case "killing":
      // Still there means the kill did not land. Gone means it did and the intent is stale
      // bookkeeping — which is a finding either way, so the check is only about which.
      return attempts.some((a) => a.attemptId === intent.attemptId);
    case "relaunching":
      return attempts.some((a) => a.role === intent.role);
    case "question-pending":
      // Nothing in the runtime can confirm a question was answered; the token is single-use
      // and the provider will not say. Only the Orchestrator closing the intent clears this.
      return false;
  }
}

function describeUnresolved(key: TicketKey, intent: Intent): string {
  switch (intent.kind) {
    case "killing":
      return `${key}: a kill of ${intent.attemptId} was recorded (${intent.why}) and never closed out. Nothing is running under that attempt.`;
    case "relaunching":
      return `${key}: a relaunch of the ${intent.role} was recorded (${intent.why}) and no ${intent.role} is running. The previous Orchestrator did not get that far.`;
    case "question-pending":
      return `${key}: a worker asked a question that was recorded and never answered — "${intent.body}".`;
  }
}

/**
 * Findings become actions only where they change what the ticket already says.
 *
 * Idempotence is not a nicety here. Every cycle rebuilds every finding from scratch, so a
 * ticket with a wedged worker produces WORKER_QUIET on every pass for as long as it stays
 * wedged. Writing unconditionally would bury the ticket under identical flags and make the
 * one thing GRE-153 promised to answer server-side — `listNeedingAttention` — useless.
 *
 * The reason string is compared, not just the code, so a finding whose sentence changed
 * (a second worker went quiet) is re-raised. Reasons are generated, never hand-written, and
 * that is what makes comparing them safe.
 *
 * Findings are merged per ticket before becoming an action, and that is not cosmetic. A
 * ticket can produce several at once — the first Orchestrator cycle after a crash routinely
 * produces two — and `Attention` holds exactly one reason. One action per finding means the
 * second write silently overwrites the first and the user reads only the last thing that
 * happened to be checked. Merging is also what keeps a ticket written at most once per pass.
 *
 * The `finding` field carries the first code in the merge. Codes are emitted in a fixed
 * order per ticket, so "first" is stable, and it is the one a script keys on; the full set
 * is legible in the reason.
 */
function toActions(findings: ReconcileFinding[], tracker: TrackerSide[]): ReconcileAction[] {
  const actions: ReconcileAction[] = [];
  const found = new Map<TicketKey, ReconcileFinding[]>();
  for (const f of findings) found.set(f.key, [...(found.get(f.key) ?? []), f]);

  for (const [key, group] of found) {
    const t = tracker.find((x) => x.ref.key === key);
    // No ticket to flag. WORKER_WITHOUT_ITEM is reported to the user directly and there is
    // nowhere durable to put it — which is itself worth seeing, not worth papering over.
    if (!t) continue;
    const reason = group.map((f) => f.reason).join("\n");
    if (t.attention.needs && t.attention.reason === reason) continue;
    actions.push({ kind: "raise-attention", key, reason, finding: group[0].code });
  }

  for (const t of tracker) {
    if (!t.attention.needs) continue;
    if (found.has(t.ref.key)) continue;
    const mine = attentionCode(t.attention.reason);
    // Raised by something that is not this function — a reviewer, a failed gate, the user.
    // Not ours to clear.
    if (mine === null) continue;
    actions.push({ kind: "clear-attention", key: t.ref.key, was: mine });
  }

  return actions;
}

/**
 * Recover the code from an attention this function raised, or null if it did not raise it.
 *
 * Matching on the generated sentence is the weak point of this file and it is deliberate:
 * GRE-153's `Attention` carries a free-text reason and nothing else, and adding a code field
 * to it means every provider stores a second field for one consumer. The cost of getting
 * this wrong is bounded in one direction only — a missed match leaves an attention up for
 * the user to clear, which is safe; a false match clears somebody else's flag, which is not.
 * So the markers below are exact and the fallback is null.
 */
function attentionCode(reason: string): ReconcileCode | null {
  if (reason.includes("nothing is running for it")) return "TICKET_WITHOUT_WORKER";
  if (reason.includes("still running for it")) return "WORKER_WITHOUT_TICKET";
  if (reason.includes("quiet for over")) return "WORKER_QUIET";
  if (reason.includes("never closed out") || reason.includes("did not get that far")) {
    return "INTENT_UNRESOLVED";
  }
  if (reason.includes("has not merged")) return "STARTED_LOCKED";
  return null;
}

// ---------------------------------------------------------------------------
// Ordering the Orchestrator owes this function
// ---------------------------------------------------------------------------

/**
 * 1. DRAIN, ACK, RECONCILE, ACT. In that order, every cycle, with no timer anywhere. The
 *    `MailboxDrained` witness enforces the first half; the second half is the caller's.
 *
 * 2. RECORD BEFORE ACTING. Every kill, every relaunch, every question is written to the
 *    ticket as an open `Intent` before the runtime call that carries it out, and closed
 *    after. A crash between the two is what `INTENT_UNRESOLVED` is for, and it is the only
 *    reason that finding can exist.
 *
 * 3. ACK AFTER RECORDING, NEVER BEFORE. GRE-150 redelivers unacknowledged events, so
 *    recording first costs a duplicate on crash. Acking first costs the event.
 *
 * 4. NO PHASE MOVES FROM HERE. A phase advances when a workflow step completes and at no
 *    other time. Nothing in `ReconcileAction` can express otherwise.
 *
 * 5. READ FRESH EVERY CYCLE. Both sides, in full, every pass. Caching the tracker read to
 *    save a call reintroduces exactly the durable state rule 3 removed, in the place where
 *    it is hardest to notice.
 *
 * 6. ONE WRITER PER TICKET PER CYCLE. Actions are applied after reconciliation, not during,
 *    so a ticket is written at most once per pass and the order is the caller's to see.
 */

// ---------------------------------------------------------------------------
// Left to other tickets, on purpose
// ---------------------------------------------------------------------------

/**
 * - What the user is offered once a Needs Attention is raised. This function ends at the
 *   flag; the menu after it — relaunch, abandon, take over by hand — is the Human Gate
 *   policy, and gates are serialized by the core (GRE-149), not here.
 *
 * - How many Implementation Tickets run at once. Reconciliation is indifferent to it; it
 *   reads whatever is open.
 *
 * - Getting the flag to the user's phone. `listNeedingAttention` is the hook, and the
 *   notification path is still in the map's fog.
 *
 * - Whether `Attention` should carry a structured code instead of free text. Worth
 *   reopening on GRE-153 if `attentionCode` above proves as brittle in the pilot as it
 *   looks on paper.
 */
