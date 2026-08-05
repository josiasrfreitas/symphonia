/**
 * PROTOTYPE — runnable harness for the Runtime Adapter contract (GRE-150). Throwaway.
 *
 *   node docs/contracts/runtime-adapter.harness.prototype.ts
 *
 * An in-memory fake runtime that reproduces the hostile parts of the real one — no
 * graceful stop, no reaper, cooperative completion, a control binding anyone can seize, a
 * capped mailbox, and a provider that will happily serve a tier nobody asked for — plus a
 * walkthrough that pushes the contract through the cases that are hard to judge on paper:
 * a second writer in one workspace, a correction that must not open a fresh context, a
 * worker that finishes silently, a stolen control binding, a zombie's late result, and the
 * accepted debt on a runtime that cannot say which tier answered.
 *
 * The state is printed after every step. Nothing is persisted.
 */

import { needsIntervention, unverifiedTierNote } from "./runtime-adapter.contract.prototype.ts";
import type {
  Access,
  AttemptRef,
  AttemptStatus,
  CapabilityTier,
  ContextRef,
  EventBatch,
  EventKind,
  KillReceipt,
  LaunchResult,
  Liveness,
  RoleName,
  RoleSpec,
  RuntimeAdapter,
  RuntimeCapabilities,
  RuntimeEvent,
  TicketKey,
  TierEvidence,
  WorkspaceRef,
} from "./runtime-adapter.contract.prototype.ts";

// ---------------------------------------------------------------------------
// The fake runtime — deliberately as unhelpful as the real one
// ---------------------------------------------------------------------------

let seq = 0;
const tick = () => {
  seq += 1;
  return new Date(Date.UTC(2026, 7, 4, 9, seq)).toISOString();
};

/** What the runtime actually holds about a process. Note there is no "role" and no tier. */
type Process = {
  id: string;
  workspaceId: string;
  /** The launch string, verbatim. The runtime never parses it. */
  command: string;
  /** What the display identity says. The only place the Ticket Key exists. */
  displayName: string;
  alive: boolean;
  quietFor: number;
  /**
   * What the provider recorded once the process answered. `null` on a provider that
   * records only what was requested — which is the entire point of GRE-163.
   */
  recordedAnsweredTier: CapabilityTier | null;
  /** What the provider was actually asked for, recorded before the request went out. */
  recordedRequestedTier: CapabilityTier;
  /** Truth, visible to the harness and to nobody else. Used to stage a silent downgrade. */
  actuallyServing: CapabilityTier;
};

type MailboxEntry = { event: RuntimeEvent; acked: boolean };

class FakeRuntime {
  workspaces = new Map<string, { id: string; setupDone: boolean; ticketKey: TicketKey }>();
  processes = new Map<string, Process>();
  mailbox: MailboxEntry[] = [];
  /** One binding for the whole system, and anyone can take it. GRE-160. */
  controlHolder: string | null = null;
  /** The provider caps a batch. Small here so the walkthrough can reach it. */
  batchCap = 3;

  createWorkspace(ticketKey: TicketKey): string {
    seq += 1;
    const id = `ws-${seq}`;
    this.workspaces.set(id, { id, setupDone: false, ticketKey });
    // Creating a workspace with no agent opens a stray fallback shell. Every time.
    this.processes.set(`shell-${seq}`, {
      id: `shell-${seq}`,
      workspaceId: id,
      command: "/bin/zsh",
      displayName: "fallback shell",
      alive: true,
      quietFor: 0,
      recordedAnsweredTier: null,
      recordedRequestedTier: "fast",
      actuallyServing: "fast",
    });
    return id;
  }

  runSetup(workspaceId: string): void {
    const ws = this.workspaces.get(workspaceId);
    if (!ws) throw new Error(`no such workspace: ${workspaceId}`);
    this.workspaces.set(workspaceId, { ...ws, setupDone: true });
  }

  spawn(workspaceId: string, command: string, displayName: string, staged: StagedTier): string {
    seq += 1;
    const id = `proc-${seq}`;
    this.processes.set(id, {
      id,
      workspaceId,
      command,
      displayName,
      alive: true,
      quietFor: 0,
      recordedAnsweredTier: null,
      recordedRequestedTier: staged.requested,
      actuallyServing: staged.serving,
    });
    return id;
  }

  /** The process answers. Only now does a provider that records answers have anything. */
  answer(id: string, recordsAnswers: boolean): void {
    const p = this.mustGet(id);
    this.processes.set(id, {
      ...p,
      quietFor: 0,
      recordedAnsweredTier: recordsAnswers ? p.actuallyServing : null,
    });
  }

  goQuiet(id: string, ms: number): void {
    const p = this.mustGet(id);
    this.processes.set(id, { ...p, quietFor: ms });
  }

  /** The only way to stop a process. There is no signal to ask it nicely. */
  kill(id: string): void {
    const p = this.mustGet(id);
    this.processes.set(id, { ...p, alive: false });
  }

  post(event: RuntimeEvent): void {
    this.mailbox.push({ event, acked: false });
  }

  mustGet(id: string): Process {
    const p = this.processes.get(id);
    if (!p) throw new Error(`no such process: ${id}`);
    return p;
  }

  liveIn(workspaceId: string): Process[] {
    return [...this.processes.values()].filter((p) => p.alive && p.workspaceId === workspaceId);
  }
}

type StagedTier = { requested: CapabilityTier; serving: CapabilityTier };

// ---------------------------------------------------------------------------
// The adapter
// ---------------------------------------------------------------------------

/** The `(provider, tier) -> launch command` table GRE-161 put behind this boundary. */
const COMMANDS: Record<string, Record<CapabilityTier, string>> = {
  "records-answers": {
    high: "agent-a --model opus --effort high --session-id",
    standard: "agent-a --model sonnet --effort medium --session-id",
    fast: "agent-a --model haiku --effort low --session-id",
  },
  "records-requests": {
    high: "agent-b --model gpt-x --effort high",
    standard: "agent-b --model gpt-x --effort medium",
    fast: "agent-b --model gpt-mini --effort low",
  },
};

type AttemptRecord = {
  ref: AttemptRef;
  contextId: string;
  processId: string;
  open: boolean;
  fenced: boolean;
};

class FakeRuntimeAdapter implements RuntimeAdapter {
  readonly capabilities: RuntimeCapabilities;

  rt: FakeRuntime;
  provider: string;
  coordinatorId: string;
  /** Every event the adapter itself raised, e.g. a reclaimed control binding. */
  attempts = new Map<string, AttemptRecord>();
  contexts = new Map<string, { ref: ContextRef; processId: string; workspaceId: string }>();
  /** Staged tiers the fake provider will serve, keyed by role. Test rig only. */
  staging: Partial<Record<RoleName, CapabilityTier>> = {};
  pendingQuestions = new Set<string>();
  lastBatch: RuntimeEvent[] = [];

  constructor(rt: FakeRuntime, provider: string, coordinatorId: string) {
    this.rt = rt;
    this.provider = provider;
    this.coordinatorId = coordinatorId;
    this.capabilities = {
      tierVerification: provider === "records-answers" ? "observed" : "requested",
      tiers: ["high", "standard", "fast"],
      workspaceSetup: true,
    };
    if (rt.controlHolder === null) rt.controlHolder = coordinatorId;
  }

  // --- guarantee 7: every operation re-establishes the control binding first ---

  private ensureControl(): void {
    if (this.rt.controlHolder === this.coordinatorId) return;
    const observed = this.rt.controlHolder;
    this.rt.controlHolder = this.coordinatorId;
    this.rt.post({
      kind: "control-lost",
      observedHolder: observed,
      reclaimed: true,
      at: tick(),
    });
  }

  // --- workspaces ---

  async createWorkspace(ticketKey: TicketKey, opts: { branch: string }): Promise<WorkspaceRef> {
    this.ensureControl();
    const id = this.rt.createWorkspace(ticketKey);
    this.rt.runSetup(id); // guarantee 3: setup finishes before this resolves
    for (const p of this.rt.liveIn(id)) this.rt.kill(p.id); // and the stray shell is closed
    return { id, ticketKey, path: `/workspaces/${ticketKey.toLowerCase()}`, branch: opts.branch };
  }

  async destroyWorkspace(workspace: WorkspaceRef): Promise<void> {
    this.ensureControl();
    const live = this.rt.liveIn(workspace.id);
    if (live.length > 0) {
      throw new Error(
        `refusing to destroy ${workspace.ticketKey}: ${live.length} context(s) still live. ` +
          `An abandoned role may still be writing here.`,
      );
    }
    this.rt.workspaces.delete(workspace.id);
  }

  // --- role contexts ---

  async launchRole(workspace: WorkspaceRef, spec: RoleSpec): Promise<LaunchResult> {
    this.ensureControl();

    // guarantee 2: at most one writer per workspace.
    if (spec.access === "write") {
      const writer = [...this.contexts.values()].find(
        (c) =>
          c.workspaceId === workspace.id &&
          c.ref.access === "write" &&
          this.rt.mustGet(c.processId).alive,
      );
      if (writer) {
        throw new Error(
          `refusing to launch ${spec.role} into ${workspace.ticketKey}: ` +
            `${writer.ref.role} already holds write access. The runtime cannot detect the collision.`,
        );
      }
    }

    if (!this.capabilities.tiers.includes(spec.tier)) {
      throw new Error(`unknown tier ${spec.tier}`);
    }

    seq += 1;
    const contextId = `ctx-${seq}`;
    const command = `${COMMANDS[this.provider][spec.tier]} ${contextId}`;
    // guarantee 5: the Ticket Key and the context identity travel in the display name and
    // in the charter, because no process id is exposed to correlate on.
    const displayName = `${workspace.ticketKey}/${spec.role}/${contextId}`;
    const staged = this.staging[spec.role] ?? spec.tier;
    const processId = this.rt.spawn(workspace.id, command, displayName, {
      requested: spec.tier,
      serving: staged,
    });

    const ref: ContextRef = {
      id: contextId,
      ticketKey: workspace.ticketKey,
      role: spec.role,
      access: spec.access,
    };
    this.contexts.set(contextId, { ref, processId, workspaceId: workspace.id });

    return { context: ref, tierEvidence: await this.verifyTier(ref) };
  }

  async verifyTier(context: ContextRef): Promise<TierEvidence> {
    const entry = this.contexts.get(context.id);
    if (!entry) return { strength: "unavailable", reason: "no such context", at: tick() };
    const p = this.rt.mustGet(entry.processId);

    // guarantee 4: `observed` only from a field written after the process answered.
    if (this.capabilities.tierVerification === "observed") {
      if (p.recordedAnsweredTier === null) {
        return { strength: "requested", tier: p.recordedRequestedTier, at: tick() };
      }
      return { strength: "observed", tier: p.recordedAnsweredTier, at: tick() };
    }
    // This provider records only what was asked for, before the request went out.
    return { strength: "requested", tier: p.recordedRequestedTier, at: tick() };
  }

  async closeRole(context: ContextRef): Promise<void> {
    this.ensureControl();
    const entry = this.contexts.get(context.id);
    if (!entry) return;
    this.rt.kill(entry.processId);
    this.contexts.delete(context.id);
  }

  // --- attempts ---

  async dispatch(
    context: ContextRef,
    instruction: { body: string; supersedes?: AttemptRef },
  ): Promise<AttemptRef> {
    this.ensureControl();
    const entry = this.contexts.get(context.id);
    if (!entry) throw new Error(`context ${context.id} is gone; dispatch has nowhere to land`);

    if (instruction.supersedes) {
      const old = this.attempts.get(instruction.supersedes.id);
      if (old) this.attempts.set(old.ref.id, { ...old, open: false, fenced: true });
    }

    seq += 1;
    const ref: AttemptRef = {
      id: `att-${seq}`,
      ticketKey: context.ticketKey,
      role: context.role,
      supersedes: instruction.supersedes?.id ?? null,
    };
    this.attempts.set(ref.id, {
      ref,
      contextId: context.id,
      processId: entry.processId,
      open: true,
      fenced: false,
    });
    this.rt.goQuiet(entry.processId, 0);
    return ref;
  }

  async kill(attempt: AttemptRef): Promise<KillReceipt> {
    this.ensureControl();
    const rec = this.attempts.get(attempt.id);
    if (rec) {
      this.rt.kill(rec.processId);
      this.attempts.set(attempt.id, { ...rec, open: false, fenced: true });
      this.contexts.delete(rec.contextId);
    }
    return { attemptId: attempt.id, workspaceLeftDirty: true };
  }

  // --- observation ---

  async drain(kinds: EventKind[]): Promise<EventBatch> {
    this.ensureControl();
    const pending = this.rt.mailbox.filter((m) => !m.acked && kinds.includes(m.event.kind));
    const taken = pending.slice(0, this.rt.batchCap);

    // guarantee 6: a result for a fenced or unknown attempt never reaches the core.
    const events = taken
      .map((m) => m.event)
      .filter((e) => {
        if (e.kind !== "result") return true;
        const rec = this.attempts.get(e.attemptId);
        return rec !== undefined && !rec.fenced;
      });

    for (const e of events) {
      if (e.kind === "result") {
        const rec = this.attempts.get(e.attemptId);
        if (rec) this.attempts.set(e.attemptId, { ...rec, open: false });
      }
      if (e.kind === "question") this.pendingQuestions.add(e.token);
    }

    this.lastBatch = taken.map((m) => m.event);
    return {
      events,
      truncated: pending.length > this.rt.batchCap,
      receipt: `receipt-${taken.length}-${seq}`,
    };
  }

  async ack(_receipt: string): Promise<void> {
    for (const m of this.rt.mailbox) {
      if (this.lastBatch.includes(m.event)) m.acked = true;
    }
    this.lastBatch = [];
  }

  async respond(token: string, _body: string): Promise<void> {
    if (!this.pendingQuestions.has(token)) {
      throw new Error(`question token ${token} is spent or unknown; it cannot be answered twice`);
    }
    this.pendingQuestions.delete(token);
  }

  async sweep(): Promise<AttemptStatus[]> {
    this.ensureControl();
    const out: AttemptStatus[] = [];
    for (const rec of this.attempts.values()) {
      if (!rec.open) continue;
      const p = this.rt.processes.get(rec.processId);
      let liveness: Liveness = "gone";
      let quietFor: number | null = null;
      if (p && p.alive) {
        liveness = p.quietFor === 0 ? "running" : "idle";
        quietFor = p.quietFor;
      }
      out.push({ attemptId: rec.ref.id, ticketKey: rec.ref.ticketKey, role: rec.ref.role, liveness, quietFor });
    }
    return out;
  }
}

// ---------------------------------------------------------------------------
// Walkthrough
// ---------------------------------------------------------------------------

const line = (s = "") => console.log(s);
const step = (n: string, title: string) => {
  line();
  line("─".repeat(76));
  line(`${n}  ${title}`);
  line("─".repeat(76));
};
const show = (label: string, value: unknown) =>
  line(`   ${label.padEnd(22)} ${typeof value === "string" ? value : JSON.stringify(value)}`);
const note = (s: string) => line(`   ${s}`);

const charter = (role: RoleName) =>
  [
    `You are the ${role}.`,
    "You never address the coordinator's control binding.",
    "You never ask the user directly; questions go to the coordinator.",
  ].join(" ");

const spec = (role: RoleName, tier: CapabilityTier, access: Access): RoleSpec => ({
  role,
  tier,
  access,
  charter: charter(role),
});

async function main() {
  const rt = new FakeRuntime();
  const strong = new FakeRuntimeAdapter(rt, "records-answers", "orchestrator");

  // --- 1. a prepared workspace ---------------------------------------------

  step("1.", "Create the workspace for one Implementation Ticket");

  const ws = await strong.createWorkspace("GRE-201", { branch: "gre-201-pilot-feature" });
  show("workspace", `${ws.ticketKey} at ${ws.path} on ${ws.branch}`);
  show("live processes", rt.liveIn(ws.id).length);
  line();
  note("Setup ran and the stray fallback shell the runtime opened was closed, both before");
  note("this call resolved. No caller can name an unprepared workspace, so no role can");
  note("enter one — the ordering rule is enforced by there being nothing else to pass.");

  // --- 2. the planner, and what a launch can and cannot promise -------------

  step("2.", "Launch the Planner and ask what tier it is on");

  const planner = await strong.launchRole(ws, spec("planner", "standard", "write"));
  show("context", planner.context.id);
  show("evidence at launch", planner.tierEvidence);

  const plan = await strong.dispatch(planner.context, { body: "Write the Local Technical Plan." });
  rt.answer(strong.contexts.get(planner.context.id)!.processId, true);
  show("evidence after answer", await strong.verifyTier(planner.context));
  line();
  note("Nothing had answered at launch, so nothing had been recorded — the strong check is");
  note("necessarily a second call. Returning the weak evidence anyway makes the gap visible");
  note("at the moment it opens instead of leaving the field absent.");

  // --- 3. the collision the runtime cannot see -----------------------------

  step("3.", "A second writer in the same workspace");

  try {
    await strong.launchRole(ws, spec("implementer", "standard", "write"));
    note("launched — which would be a bug");
  } catch (err) {
    show("refused", (err as Error).message);
  }
  const earlyReviewer = await strong.launchRole(ws, spec("spec-reviewer", "fast", "read"));
  show("read access", `${earlyReviewer.context.role} launched alongside the writer`);
  await strong.closeRole(earlyReviewer.context);
  line();
  note("Two agents writing one checkout is invisible to the runtime, so the adapter is the");
  note("only place it can be stopped. Readers are unlimited — which is exactly what makes");
  note("the two reviewers legal in parallel later on.");

  // --- 4. the result envelope ----------------------------------------------

  step("4.", "The Planner delivers");

  rt.post({
    kind: "result",
    attemptId: plan.id,
    contextId: planner.context.id,
    ticketKey: "GRE-201",
    role: "planner",
    outcome: "delivered",
    summary: "Plan appended to the ticket. Four steps, one migration.",
    tierEvidence: await strong.verifyTier(planner.context),
    at: tick(),
  });

  let batch = await strong.drain(["result", "question", "escalation", "control-lost"]);
  show("events", batch.events.map((e) => e.kind));
  show("truncated", batch.truncated);
  await strong.ack(batch.receipt);
  const planResult = batch.events[0];
  show("debt note", planResult.kind === "result" ? unverifiedTierNote(planResult) : "n/a");
  line();
  note("The envelope carries no changed files and no test verdict. Completion here is the");
  note("worker's own claim, so any quality field would be an unverified assertion wearing");
  note("the runtime's authority. `outcome` is about the process, never the product.");

  await strong.closeRole(planner.context);

  // --- 5. the correction loop ----------------------------------------------

  step("5.", "The Implementer, and a correction that must not open a fresh context");

  const impl = await strong.launchRole(ws, spec("implementer", "standard", "write"));
  const build = await strong.dispatch(impl.context, { body: "Implement the approved plan." });
  rt.answer(strong.contexts.get(impl.context.id)!.processId, true);

  const specR = await strong.launchRole(ws, spec("spec-reviewer", "standard", "read"));
  const stdR = await strong.launchRole(ws, spec("standards-reviewer", "standard", "read"));
  show("in flight", [impl.context.role, specR.context.role, stdR.context.role]);

  const fix = await strong.dispatch(impl.context, {
    body: "The spec reviewer found two gaps. Address them.",
    supersedes: build,
  });
  show("correction attempt", { id: fix.id, supersedes: fix.supersedes });
  show("same context", fix.role === impl.context.role && strong.attempts.get(fix.id)!.contextId === impl.context.id);
  line();
  note("A correction is a new attempt into the *same* context — `dispatch`, which takes a");
  note("ContextRef and can only reuse. Opening a fresh one would be `launchRole`, which takes");
  note("a workspace and can only create. There is no argument that turns one into the other,");
  note("so the Planner-Implementer-Reviewer-in-one-session trap cannot be written down.");

  await strong.closeRole(specR.context);
  await strong.closeRole(stdR.context);

  // --- 6. the accepted debt ------------------------------------------------

  step("6.", "A runtime that cannot say which tier answered");

  const weak = new FakeRuntimeAdapter(rt, "records-requests", "orchestrator");
  show("capability", weak.capabilities.tierVerification);

  const ws2 = await weak.createWorkspace("GRE-202", { branch: "gre-202-second-ticket" });
  weak.staging = { planner: "fast" }; // the provider silently serves something else
  const weakPlanner = await weak.launchRole(ws2, spec("planner", "high", "write"));
  const weakAttempt = await weak.dispatch(weakPlanner.context, { body: "Plan it." });
  const weakProc = weak.contexts.get(weakPlanner.context.id)!.processId;
  rt.answer(weakProc, false);

  show("truth (harness only)", rt.mustGet(weakProc).actuallyServing);
  show("what the adapter says", await weak.verifyTier(weakPlanner.context));

  rt.post({
    kind: "result",
    attemptId: weakAttempt.id,
    contextId: weakPlanner.context.id,
    ticketKey: "GRE-202",
    role: "planner",
    outcome: "delivered",
    summary: "Plan written.",
    tierEvidence: await weak.verifyTier(weakPlanner.context),
    at: tick(),
  });
  batch = await weak.drain(["result"]);
  const weakResult = batch.events[0];
  await weak.ack(batch.receipt);
  line();
  show("debt note", weakResult.kind === "result" ? unverifiedTierNote(weakResult) : "n/a");
  line();
  note("`high` was asked for and `fast` answered, and nothing in this runtime can tell. The");
  note("decision on this ticket was to keep the provider and carry that. What the contract");
  note("adds is that the gap cannot be hidden: the evidence is a union, so no caller can");
  note("read a uniform promise, and the note above is written onto the ticket where the");
  note("debt was incurred rather than only in a design document.");

  // --- 7. the silent finisher ----------------------------------------------

  step("7.", "A worker that finishes without saying so");

  rt.goQuiet(weakProc, 11 * 60 * 1000);
  const orphan = await weak.dispatch(weakPlanner.context, { body: "One more thing." });
  rt.goQuiet(weakProc, 11 * 60 * 1000);

  const emptyDrain = await weak.drain(["result", "escalation", "question"]);
  show("events waiting", emptyDrain.events.length);
  const statuses = await weak.sweep();
  show("sweep", statuses.map((s) => `${s.attemptId} ${s.role} ${s.liveness} quiet=${s.quietFor}ms`));
  show("needs a human", needsIntervention(statuses, 10 * 60 * 1000).map((s) => s.attemptId));
  line();
  note(`Attempt ${orphan.id} is outstanding and the stream is empty. No event will ever come,`);
  note("because the failure is the *absence* of one and the runtime has no reaper. This is");
  note("why `sweep` is a separate call and not something derived from `drain`: a coordinator");
  note("that only reads the stream waits forever. `idle` deliberately does not distinguish");
  note("finished-quietly from wedged — the runtime cannot, so neither does the contract.");

  // --- 8. the stolen control binding ---------------------------------------

  step("8.", "Someone else takes the control binding");

  rt.controlHolder = "some-other-pane";
  show("holder before", rt.controlHolder);
  const afterTheft = await strong.drain(["result", "control-lost"]);
  show("holder after", rt.controlHolder);
  show("events", afterTheft.events.map((e) => (e.kind === "control-lost" ? e : e.kind)));
  await strong.ack(afterTheft.receipt);
  line();
  note("Reclaiming is automatic, because every call re-establishes the binding before acting.");
  note("Reporting it is not optional: a worker touching the control plane breaks a rule the");
  note("whole system rests on, and a silent reclaim would make the breach invisible.");

  // --- 9. no graceful stop, and the zombie's late result --------------------

  step("9.", "Stopping the Implementer");

  const receipt = await strong.kill(fix);
  show("receipt", receipt);

  rt.post({
    kind: "result",
    attemptId: fix.id,
    contextId: impl.context.id,
    ticketKey: "GRE-201",
    role: "implementer",
    outcome: "delivered",
    summary: "Done! (sent by a context that was killed)",
    tierEvidence: { strength: "observed", tier: "standard", at: tick() },
    at: tick(),
  });
  const afterKill = await strong.drain(["result"]);
  show("results delivered", afterKill.events.length);
  await strong.ack(afterKill.receipt);
  line();
  note("`workspaceLeftDirty` is always true and has no other value to take. Stopping a role");
  note("is a kill — there is nothing cooperative to ask for — so the workspace may hold a");
  note("half-written edit, and a boolean that is always true forces that into every call");
  note("site. The killed context's result was dropped, not delivered: an attempt that has");
  note("been fenced cannot be completed by the zombie that was doing it.");

  // --- 10. the mailbox cap --------------------------------------------------

  step("10.", "More events than one batch holds");

  for (let i = 0; i < 5; i += 1) {
    rt.post({
      kind: "escalation",
      attemptId: `att-noise-${i}`,
      ticketKey: "GRE-201",
      body: `escalation ${i}`,
      at: tick(),
    });
  }
  const capped = await strong.drain(["escalation"]);
  show("taken", capped.events.length);
  show("truncated", capped.truncated);
  const redelivered = await strong.drain(["escalation"]); // no ack in between
  show("redelivered", redelivered.events.length);
  await strong.ack(redelivered.receipt);
  const remaining = await strong.drain(["escalation"]);
  show("after ack", remaining.events.length);
  line();
  note("`truncated` is the signal that the coordinator has fallen behind — a full batch and");
  note("a timer is how parallel work goes unnoticed. Without an ack the same events come");
  note("back, so the core has to be idempotent per attempt; acking before the event is");
  note("durably recorded loses it.");

  // --- 11. teardown ---------------------------------------------------------

  step("11.", "Removing the workspace");

  const stillLive = await strong.launchRole(ws, spec("implementer", "standard", "write"));
  try {
    await strong.destroyWorkspace(ws);
  } catch (err) {
    show("refused", (err as Error).message);
  }
  await strong.closeRole(stillLive.context);
  await strong.destroyWorkspace(ws);
  show("workspace gone", !rt.workspaces.has(ws.id));
  line();
  note("There is no force flag. A dropped worker does nothing to its processes or files and");
  note("may still be writing, so deleting under it would destroy work mid-flight. The core");
  note("removes a workspace when the pull request merges; on the unhappy path it asks.");

  line();
  line("─".repeat(76));
  line("done");
  line("─".repeat(76));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
