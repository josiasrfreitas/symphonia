/**
 * PROTOTYPE — runnable harness for progress authority and reconciliation (GRE-155).
 * Throwaway.
 *
 *   node docs/contracts/progress-reconciliation.harness.prototype.ts
 *
 * A fake tracker and a fake runtime, wired to one Orchestrator cycle that owns no state
 * between passes — every pass rebuilds the picture from both sides and joins on the Ticket
 * Key. The walkthrough pushes it through the cases that are hard to judge on paper: a worker
 * that finished silently versus one that is wedged, a live worker whose ticket is not in
 * flight, a ticket in flight with nothing running, a kill that landed and a relaunch that
 * did not, a question recorded but never answered, and a fresh Orchestrator waking into a
 * system full of work it never launched.
 *
 * The Orchestrator object is thrown away and rebuilt mid-run to prove the point. Nothing is
 * persisted anywhere else either.
 */

import { reconcile } from "./progress-reconciliation.contract.prototype.ts";
import type {
  Intent,
  MailboxDrained,
  ReconcileAction,
  ReconcileFinding,
  TrackerSide,
} from "./progress-reconciliation.contract.prototype.ts";
import type { AttemptStatus, RoleName, TicketKey } from "./runtime-adapter.contract.prototype.ts";
import type { Attention, DeliveryPhase } from "./tracker-adapter.contract.prototype.ts";

// ---------------------------------------------------------------------------
// Clock
// ---------------------------------------------------------------------------

let seq = 0;
const tick = () => {
  seq += 1;
  return new Date(Date.UTC(2026, 7, 5, 9, seq)).toISOString();
};

const QUIET_MS = 10 * 60 * 1000;

// ---------------------------------------------------------------------------
// The fake tracker — durable, dumb, and the only thing that survives a restart
// ---------------------------------------------------------------------------

type TicketRow = {
  key: TicketKey;
  phase: DeliveryPhase;
  attention: Attention;
  intents: Intent[];
  unlocked: boolean;
};

class FakeTracker {
  rows = new Map<TicketKey, TicketRow>();

  add(key: TicketKey, phase: DeliveryPhase, unlocked = true): void {
    this.rows.set(key, { key, phase, attention: { needs: false }, intents: [], unlocked });
  }

  setPhase(key: TicketKey, phase: DeliveryPhase): void {
    this.rows.set(key, { ...this.must(key), phase });
  }

  setAttention(key: TicketKey, attention: Attention): void {
    this.rows.set(key, { ...this.must(key), attention });
  }

  /** Append-only, like GRE-153's body edits. Closing is a field, never a delete. */
  recordIntent(key: TicketKey, intent: Intent): void {
    const row = this.must(key);
    this.rows.set(key, { ...row, intents: [...row.intents, intent] });
  }

  closeIntent(key: TicketKey, match: (i: Intent) => boolean, at: string): void {
    const row = this.must(key);
    this.rows.set(key, {
      ...row,
      intents: row.intents.map((i) => (match(i) && i.closedAt === null ? { ...i, closedAt: at } : i)),
    });
  }

  must(key: TicketKey): TicketRow {
    const row = this.rows.get(key);
    if (!row) throw new Error(`no such ticket: ${key}`);
    return row;
  }

  /** What the Orchestrator reads at the top of every cycle. Fresh, always, in full. */
  read(): TrackerSide[] {
    return [...this.rows.values()].map((r) => ({
      ref: { id: `id-${r.key}`, key: r.key, url: `https://tracker.example/${r.key}` },
      phase: r.phase,
      attention: r.attention,
      openIntents: r.intents.filter((i) => i.closedAt === null),
      unlocked: r.unlocked,
    }));
  }
}

// ---------------------------------------------------------------------------
// The fake runtime — only ever asked one question: what is alive?
// ---------------------------------------------------------------------------

type Live = {
  attemptId: string;
  ticketKey: TicketKey;
  role: RoleName;
  alive: boolean;
  quietFor: number;
};

class FakeRuntime {
  attempts = new Map<string, Live>();

  launch(ticketKey: TicketKey, role: RoleName): string {
    seq += 1;
    const attemptId = `att-${seq}`;
    this.attempts.set(attemptId, { attemptId, ticketKey, role, alive: true, quietFor: 0 });
    return attemptId;
  }

  goQuiet(attemptId: string, ms: number): void {
    const a = this.must(attemptId);
    this.attempts.set(attemptId, { ...a, quietFor: ms });
  }

  /** The only way to stop work. GRE-150 has no cooperative shutdown. */
  kill(attemptId: string): void {
    const a = this.must(attemptId);
    this.attempts.set(attemptId, { ...a, alive: false });
  }

  /** A result arrived and the attempt is no longer outstanding. */
  complete(attemptId: string): void {
    this.attempts.delete(attemptId);
  }

  must(attemptId: string): Live {
    const a = this.attempts.get(attemptId);
    if (!a) throw new Error(`no such attempt: ${attemptId}`);
    return a;
  }

  /** GRE-150's `sweep()`. Note it knows nothing about phases, and never will. */
  sweep(): AttemptStatus[] {
    return [...this.attempts.values()].map((a) => ({
      attemptId: a.attemptId,
      ticketKey: a.ticketKey,
      role: a.role,
      liveness: !a.alive ? "gone" : a.quietFor === 0 ? "running" : "idle",
      quietFor: a.alive ? a.quietFor : null,
    }));
  }
}

// ---------------------------------------------------------------------------
// The Orchestrator — deliberately holds nothing between cycles
// ---------------------------------------------------------------------------

/**
 * Every field here is derived inside a single cycle and thrown away at the end of it. There
 * is no map from tickets to attempts, no list of what was launched, no "last seen". The
 * class exists to hold two adapter handles and nothing else, which is why replacing it
 * mid-run changes no behaviour at all.
 */
class Orchestrator {
  tracker: FakeTracker;
  runtime: FakeRuntime;
  name: string;

  constructor(tracker: FakeTracker, runtime: FakeRuntime, name: string) {
    this.tracker = tracker;
    this.runtime = runtime;
    this.name = name;
  }

  /** Drain, ack, reconcile, act. The witness is what makes the first half unskippable. */
  cycle(): { findings: ReconcileFinding[]; actions: ReconcileAction[] } {
    const drained: MailboxDrained = { receipt: `receipt-${(seq += 1)}`, acked: true };

    const out = reconcile({
      drained,
      tracker: this.tracker.read(),
      runtime: this.runtime.sweep(),
      quietThresholdMs: QUIET_MS,
      at: tick(),
    });

    for (const action of out.actions) {
      if (action.kind === "raise-attention") {
        this.tracker.setAttention(action.key, {
          needs: true,
          reason: action.reason,
          raisedAt: tick(),
        });
      } else {
        this.tracker.setAttention(action.key, { needs: false });
      }
    }

    return out;
  }

  /** Rule 4: the intent lands on the ticket before the runtime call that carries it out. */
  killWithIntent(key: TicketKey, attemptId: string, why: string): void {
    this.tracker.recordIntent(key, { kind: "killing", attemptId, why, at: tick(), closedAt: null });
    this.runtime.kill(attemptId);
    this.tracker.closeIntent(key, (i) => i.kind === "killing" && i.attemptId === attemptId, tick());
  }
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

const line = (s = "") => console.log(s);
const step = (n: string, title: string) => {
  line();
  line("─".repeat(76));
  line(`${n}  ${title}`);
  line("─".repeat(76));
};
const show = (label: string, value: unknown) =>
  line(`   ${label.padEnd(20)} ${typeof value === "string" ? value : JSON.stringify(value)}`);
const note = (s: string) => line(`   ${s}`);

const report = (out: { findings: ReconcileFinding[]; actions: ReconcileAction[] }) => {
  if (out.findings.length === 0) note("no findings");
  for (const f of out.findings) line(`   ${f.code.padEnd(23)} ${f.reason}`);
  line(`   ${"→ actions".padEnd(23)} ${out.actions.map((a) => `${a.kind}(${a.key})`).join(", ") || "none"}`);
};

const phases = (t: FakeTracker) =>
  [...t.rows.values()].map((r) => `${r.key}:${r.phase}${r.attention.needs ? "!" : ""}`);

// ---------------------------------------------------------------------------
// Walkthrough
// ---------------------------------------------------------------------------

function main() {
  const tracker = new FakeTracker();
  const runtime = new FakeRuntime();
  let orc = new Orchestrator(tracker, runtime, "orchestrator-1");

  // --- 1. the healthy case --------------------------------------------------

  step("1.", "Two tickets in flight, both with a live worker");

  tracker.add("GRE-201", "implementing");
  tracker.add("GRE-202", "planning");
  const implA = runtime.launch("GRE-201", "implementer");
  runtime.launch("GRE-202", "planner");

  report(orc.cycle());
  line();
  note("The two sides were never compared as opinions about one fact. The tracker was asked");
  note("where each ticket stands, the runtime was asked what is alive, and they joined on the");
  note("Ticket Key. Nothing agreed or disagreed — they answered different questions.");

  // --- 2. the silent finisher, and the wedged worker ------------------------

  step("2.", "A worker goes quiet — finished without saying so, or stuck");

  runtime.goQuiet(implA, 11 * 60 * 1000);
  report(orc.cycle());
  show("phase after", tracker.must("GRE-201").phase);
  line();
  note("One finding, not two, because the runtime reports one state. `idle` cannot tell a");
  note("worker that finished silently from one that is wedged, and a reconciler that guessed");
  note("would be inventing a distinction its own source refused to make. The phase did not");
  note("move: `implementing` is still where this ticket stands until a step actually completes.");

  // --- 3. idempotence -------------------------------------------------------

  step("3.", "The same cycle again, with nothing changed");

  const second = orc.cycle();
  report(second);
  line();
  note("The finding is regenerated from scratch — it always will be, for as long as the worker");
  note("stays quiet — but the ticket already says exactly this, so there is nothing to write.");
  note("Writing unconditionally would bury the ticket and make `listNeedingAttention` useless,");
  note("which is the one query GRE-153 asked a provider to answer server-side.");

  // --- 4. the cause goes away ----------------------------------------------

  step("4.", "The worker comes back");

  runtime.goQuiet(implA, 0);
  report(orc.cycle());
  show("attention", tracker.must("GRE-201").attention);
  line();
  note("Cleared, because this reconciler raised it and its cause is gone. A reviewer's");
  note("rejection would not be cleared here — it is nobody's to clear but the user's, and the");
  note("only thing separating the two is which sentence is sitting in the reason field.");

  // --- 5. a ticket in flight with nothing running ---------------------------

  step("5.", "The Planner's context dies without a result");

  const planB = [...runtime.attempts.values()].find((a) => a.ticketKey === "GRE-202")!.attemptId;
  runtime.complete(planB); // gone from the sweep, and no result ever arrived
  report(orc.cycle());
  show("phase after", tracker.must("GRE-202").phase);
  line();
  note("The mailbox was empty and stayed empty — the message that would have told us is the");
  note("one that never comes. This is why reconciliation runs after the drain rather than on");
  note("its own timer: an empty mailbox means nothing until it has been opened, and it means");
  note("something the moment after. `planning` was left standing; the user chooses what next.");

  // --- 6. a live worker on a ticket that is not in flight -------------------

  step("6.", "A worker still running while its ticket waits at a gate");

  tracker.setAttention("GRE-202", { needs: false });
  tracker.setPhase("GRE-202", "awaiting-plan-approval");
  runtime.launch("GRE-202", "planner");
  report(orc.cycle());
  line();
  note("Reported as a worker that never stopped rather than as a stray, because a gate and a");
  note("merged ticket are different sentences for the user even though both are 'not in");
  note("flight'. Nothing was killed. Reconciliation has no way to express killing anything.");

  // --- 7. the DAG constraint ------------------------------------------------

  step("7.", "A ticket started before its predecessor merged");

  tracker.add("GRE-203", "implementing", false);
  runtime.launch("GRE-203", "implementer");
  report(orc.cycle());
  line();
  note("Reported, never repaired. Killing live work over a graph edge is destructive, and the");
  note("Orchestrator cannot see what is half-written underneath it — the same reason GRE-150");
  note("refused automatic workspace deletion on the unhappy path.");

  // --- 8. a half-finished action, seen from the far side of a crash ---------

  step("8.", "The Orchestrator dies between the kill and the relaunch");

  tracker.setAttention("GRE-201", { needs: false });
  orc.killWithIntent("GRE-201", implA, "wedged for twenty minutes");
  runtime.complete(implA);

  // It meant to relaunch. It wrote that down first — and then it died.
  tracker.recordIntent("GRE-201", {
    kind: "relaunching",
    role: "implementer",
    supersedes: implA,
    why: "replacing the wedged implementer",
    at: tick(),
    closedAt: null,
  });

  step("8b.", "A brand new Orchestrator wakes up");
  orc = new Orchestrator(tracker, runtime, "orchestrator-2");
  show("knows about", "nothing — it was constructed one line ago");
  report(orc.cycle());
  line();
  note("It did not have to infer intent, and it could not have: a dead attempt with no");
  note("successor looks identical whether it was killed deliberately or fell over. The");
  note("difference is only knowable because the plan was written to the ticket before the");
  note("kill, in a form a human reading the ticket understands without being told what it is.");
  note("This is the whole of what rule 3 costs, and rule 4 is the whole of what pays it.");

  // --- 9. finishing the interrupted work -----------------------------------

  step("9.", "The successor finishes what its predecessor started");

  tracker.setAttention("GRE-201", { needs: false });
  runtime.launch("GRE-201", "implementer");
  tracker.closeIntent("GRE-201", (i) => i.kind === "relaunching", tick());
  report(orc.cycle());
  line();
  note("Not a recovery path. It is the same cycle that has run eight times already, reading");
  note("the same two sides. There is no emergency code here to be wrong, because there is no");
  note("emergency code.");

  // --- 10. a question that was recorded and never answered -----------------

  step("10.", "A pending question outlives the Orchestrator that received it");

  tracker.recordIntent("GRE-202", {
    kind: "question-pending",
    token: "tok-9f2",
    body: "Should the migration be reversible?",
    at: tick(),
    closedAt: null,
  });
  report(orc.cycle());
  line();
  note("Recorded before the event carrying it was acknowledged, so a crash in between costs a");
  note("duplicate rather than the question — GRE-150 redelivers what is unacknowledged, and");
  note("its question tokens are single-use and unanswerable once dropped. Nothing in the");
  note("runtime can confirm an answer, so only the Orchestrator closing the intent clears it.");

  // --- 11. work under a key the tracker has never heard of -----------------

  step("11.", "A context running under a key that is not an open ticket");

  runtime.launch("GRE-999", "implementer");
  const stray = orc.cycle();
  report(stray);
  show("tickets", phases(tracker));
  line();
  note("The one finding with nowhere durable to go — there is no ticket to flag, so no action");
  note("is produced and it is shown to the user directly. That gap is worth seeing rather than");
  note("papering over: it is what a mistyped Ticket Key in a launch string looks like, and the");
  note("Ticket Key is the only thing tying a running process back to work at all.");

  line();
  line("─".repeat(76));
  line("done");
  line("─".repeat(76));
}

main();
