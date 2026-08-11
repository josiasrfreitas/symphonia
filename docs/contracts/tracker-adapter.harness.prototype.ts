/**
 * PROTOTYPE — runnable harness for the Tracker Adapter contract (GRE-153). Throwaway.
 *
 *   node docs/contracts/tracker-adapter.harness.prototype.ts
 *
 * An in-memory fake tracker that reproduces the pilot provider's hostile parts — no
 * compare-and-swap, anchors that must match exactly once, no server-side frontier — and a
 * walkthrough that pushes the contract through the cases that are hard to judge on paper:
 * a lost claim race, a stale anchor, two sessions appending to the map at once, a lossy
 * phase projection, and a ticket that fails and needs the user.
 *
 * The state is printed after every step. Nothing is persisted.
 */

import { frontier } from "./tracker-adapter.contract.prototype.ts";
import type {
  ActorId,
  Artifact,
  Attention,
  ClaimResult,
  CloseOutcome,
  Comment,
  DecisionType,
  Delivery,
  DeliveryPhase,
  Item,
  ItemKind,
  ItemRef,
  BodyOp,
  TrackerAdapter,
  TrackerCapabilities,
} from "./tracker-adapter.contract.prototype.ts";

// ---------------------------------------------------------------------------
// The fake provider — deliberately as unhelpful as the real one
// ---------------------------------------------------------------------------

/** What the provider actually stores. Note there is no `phase` column: adapters project. */
type Row = {
  id: string;
  key: string;
  parentId: string | null;
  title: string;
  body: string;
  /**
   * The provider's status vocabulary, extended so every delivery phase is its own column
   * on the board. `todo` and `in-progress` are what Decision Tickets use; the rest are the
   * delivery phases one to one. `coarse-*` are what the counterexample adapter in step 7
   * writes instead.
   */
  status:
    | "todo"
    | "in-progress"
    | "briefed"
    | "planning"
    | "awaiting-plan-approval"
    | "implementing"
    | "reviewing"
    | "awaiting-merge"
    | "coarse-started"
    | "coarse-review"
    | "done"
    | "canceled";
  assignee: ActorId | null;
  labels: string[];
  blockedBy: string[];
  comments: Comment[];
  artifacts: Artifact[];
  delivery: Delivery;
};

let seq = 0;
const now = () => new Date(Date.UTC(2026, 7, 3, 20, seq)).toISOString();

class FakeProvider {
  rows = new Map<string, Row>();

  insert(row: Omit<Row, "id" | "key">): Row {
    seq += 1;
    const id = `uuid-${seq}`;
    const full = { ...row, id, key: `GRE-${100 + seq}` };
    this.rows.set(id, full);
    return full;
  }

  get(id: string): Row {
    const row = this.rows.get(id);
    if (!row) throw new Error(`no such item: ${id}`);
    return row;
  }

  /** Last write wins, silently. There is no version, no If-Match, no conditional update. */
  update(id: string, fields: Partial<Row>): void {
    this.rows.set(id, { ...this.get(id), ...fields });
  }

  /**
   * Server-side patch. Anchors must match exactly once or the whole patch aborts.
   * This is the only precondition the provider offers.
   */
  patch(id: string, ops: BodyOp[]): void {
    let body = this.get(id).body;
    for (const op of ops) {
      if (op.op === "append") {
        body += op.text;
        continue;
      }
      const anchor = op.op === "insertBefore" ? op.anchor : op.find;
      const hits = body.split(anchor).length - 1;
      if (hits !== 1) {
        throw new Error(
          `patch aborted: anchor ${JSON.stringify(anchor)} matched ${hits} times, expected exactly 1`,
        );
      }
      body =
        op.op === "insertBefore"
          ? body.replace(anchor, op.text + anchor)
          : body.replace(op.find, op.text);
    }
    this.update(id, { body });
  }

  /** Label filtering is the one thing it does index. */
  byLabel(parentId: string, label: string): Row[] {
    return [...this.rows.values()].filter(
      (r) => r.parentId === parentId && r.labels.includes(label),
    );
  }
}

// ---------------------------------------------------------------------------
// The adapter
// ---------------------------------------------------------------------------

const ATTENTION_LABEL = "needs-attention";
const GATE_LABEL = "human-gate";

/** The phases that exist as their own status on the provider. One to one, by design. */
const PHASE_STATUSES = [
  "briefed",
  "planning",
  "awaiting-plan-approval",
  "implementing",
  "reviewing",
  "awaiting-merge",
] as const;

/**
 * `lossyPhase: true` folds several phases onto one status — what an adapter does on a
 * provider whose statuses it cannot extend. The walkthrough shows what that costs.
 */
type FakeAdapterOptions = {
  lossyPhase?: boolean;
  /** Fires after `claim` writes and before it verifies — lets the walkthrough interleave. */
  onBeforeClaimVerify?: (id: string) => void;
};

class FakeTrackerAdapter implements TrackerAdapter {
  capabilities: TrackerCapabilities;

  db: FakeProvider;
  opts: FakeAdapterOptions;

  constructor(db: FakeProvider, opts: FakeAdapterOptions = {}) {
    this.db = db;
    this.opts = opts;
    this.capabilities = {
      nativeBlocking: true,
      documents: true,
      nativePhases: !opts.lossyPhase,
    };
  }

  ref(row: Row): ItemRef {
    return { id: row.id, key: row.key, url: `https://tracker.example/${row.key}` };
  }

  /** Statuses collapse to open/closed. Everything finer is ours, not the provider's. */
  hydrate(row: Row, withRelations: boolean): Item {
    const kindLabel = row.labels.find((l) => l.startsWith("kind:"))?.slice(5);
    const typeLabel = row.labels.find((l) => l.startsWith("type:"))?.slice(5);
    const attentionComment = row.comments.find((c) => c.body.startsWith("[attention]"));

    return {
      ref: this.ref(row),
      kind: (kindLabel ?? "decision-ticket") as ItemKind,
      title: row.title,
      body: row.body,
      openness: row.status === "done" || row.status === "canceled" ? "closed" : "open",
      claimedBy: row.assignee,
      decisionType: (typeLabel ?? null) as DecisionType | null,
      phase: this.phaseFromStatus(row.status),
      attention: row.labels.includes(ATTENTION_LABEL)
        ? {
            needs: true,
            reason: attentionComment?.body.replace("[attention] ", "") ?? "",
            raisedAt: attentionComment?.createdAt ?? "",
          }
        : { needs: false },
      delivery: row.delivery,
      blockedBy: withRelations ? row.blockedBy.map((b) => this.ref(this.db.get(b))) : null,
    };
  }

  /** Each phase is its own status, so reading it back is a lookup and cannot be ambiguous. */
  phaseFromStatus(status: Row["status"]): DeliveryPhase | null {
    if ((PHASE_STATUSES as readonly string[]).includes(status)) return status as DeliveryPhase;
    if (status === "done") return "merged";
    if (status === "canceled") return "abandoned";
    // The folded statuses of the counterexample adapter: the read has to guess.
    if (status === "coarse-started") return "implementing"; // ...or "planning". Unknowable.
    if (status === "coarse-review") return "reviewing"; // ...or "awaiting-merge".
    return null;
  }

  statusForPhase(phase: DeliveryPhase): Row["status"] {
    if (phase === "merged") return "done";
    if (phase === "abandoned") return "canceled";
    if (!this.opts.lossyPhase) return phase;
    if (phase === "briefed") return "todo";
    if (phase === "planning" || phase === "implementing") return "coarse-started";
    return "coarse-review";
  }

  async getItem(id: string, opts?: { withRelations?: boolean }): Promise<Item> {
    return this.hydrate(this.db.get(id), opts?.withRelations ?? false);
  }

  async listChildren(mapId: string, opts?: { withRelations?: boolean }): Promise<Item[]> {
    return [...this.db.rows.values()]
      .filter((r) => r.parentId === mapId)
      .map((r) => this.hydrate(r, opts?.withRelations ?? false));
  }

  async listNeedingAttention(mapId: string): Promise<Item[]> {
    return this.db.byLabel(mapId, ATTENTION_LABEL).map((r) => this.hydrate(r, false));
  }

  async listComments(id: string): Promise<Comment[]> {
    return this.db.get(id).comments;
  }

  async listArtifacts(id: string): Promise<Artifact[]> {
    return this.db.get(id).artifacts;
  }

  async readArtifact(artifact: Artifact): Promise<string> {
    return artifact.kind === "repo-file"
      ? `<contents of ${artifact.path} from git>`
      : `<contents of document ${artifact.id}>`;
  }

  async createChild(
    parentId: string,
    spec: { kind: ItemKind; title: string; body: string; decisionType?: DecisionType },
  ): Promise<ItemRef> {
    const labels = [`kind:${spec.kind}`];
    if (spec.decisionType) labels.push(`type:${spec.decisionType}`);
    const row = this.db.insert({
      parentId,
      title: spec.title,
      body: spec.body,
      status: "todo",
      assignee: null,
      labels,
      blockedBy: [],
      comments: [],
      artifacts: [],
      delivery: { workspace: null, branch: null, pullRequest: null },
    });
    return this.ref(row);
  }

  async addBlocker(id: string, blockerId: string): Promise<void> {
    const row = this.db.get(id);
    if (!row.blockedBy.includes(blockerId)) {
      this.db.update(id, { blockedBy: [...row.blockedBy, blockerId] });
    }
  }

  async removeBlocker(id: string, blockerId: string): Promise<void> {
    const row = this.db.get(id);
    this.db.update(id, { blockedBy: row.blockedBy.filter((b) => b !== blockerId) });
  }

  async patchBody(id: string, ops: BodyOp[]): Promise<void> {
    this.db.patch(id, ops);
  }

  async claim(id: string, actor: ActorId): Promise<ClaimResult> {
    this.db.update(id, { assignee: actor, status: "in-progress" });
    this.opts.onBeforeClaimVerify?.(id);
    const held = this.db.get(id).assignee;
    return held === actor ? { ok: true } : { ok: false, heldBy: held! };
  }

  async release(id: string, actor: ActorId): Promise<void> {
    if (this.db.get(id).assignee === actor) this.db.update(id, { assignee: null });
  }

  async close(id: string, outcome: CloseOutcome): Promise<void> {
    this.db.update(id, { status: outcome === "resolved" ? "done" : "canceled" });
  }

  async setPhase(id: string, phase: DeliveryPhase): Promise<void> {
    this.db.update(id, { status: this.statusForPhase(phase) });
  }

  async setAttention(id: string, attention: Attention): Promise<void> {
    const row = this.db.get(id);
    const labels = row.labels.filter((l) => l !== ATTENTION_LABEL);
    if (attention.needs) {
      this.db.update(id, { labels: [...labels, ATTENTION_LABEL] });
      await this.postComment(id, `[attention] ${attention.reason}`);
    } else {
      this.db.update(id, { labels });
    }
  }

  async setDelivery(id: string, delivery: Partial<Delivery>): Promise<void> {
    this.db.update(id, { delivery: { ...this.db.get(id).delivery, ...delivery } });
  }

  async setGate(id: string, waiting: boolean): Promise<void> {
    const row = this.db.get(id);
    const labels = row.labels.filter((l) => l !== GATE_LABEL);
    this.db.update(id, { labels: waiting ? [...labels, GATE_LABEL] : labels });
  }

  async postComment(id: string, body: string): Promise<Comment> {
    seq += 1;
    const comment: Comment = {
      id: `c-${seq}`, author: "session", authorName: "session", createdAt: now(), body,
    };
    this.db.update(id, { comments: [...this.db.get(id).comments, comment] });
    return comment;
  }

  async postResolution(id: string, resolution: { tldr: string; body: string }): Promise<Comment> {
    return this.postComment(id, `## TLDR\n\n${resolution.tldr}\n\n${resolution.body}`);
  }

  async recordGate(ticket: string, gate: string, decision: string, evidence: string): Promise<Comment> {
    return this.postComment(ticket, `**TLDR: ${gate} — ${decision}.**\n\n## Evidence\n\n${evidence}`);
  }

  async attachArtifact(id: string, artifact: Artifact): Promise<void> {
    this.db.update(id, { artifacts: [...this.db.get(id).artifacts, artifact] });
  }

  renderRef(ref: ItemRef, label: string): string {
    // Angle brackets, so the provider does not turn this into a phantom relation.
    return `[${label}](<${ref.url}>)`;
  }
}

// ---------------------------------------------------------------------------
// Walkthrough
// ---------------------------------------------------------------------------

const line = (s = "") => console.log(s);
const step = (n: string, title: string) => {
  line();
  line(`${"─".repeat(76)}`);
  line(`${n}  ${title}`);
  line(`${"─".repeat(76)}`);
};
const show = (label: string, value: unknown) =>
  line(`   ${label.padEnd(22)} ${typeof value === "string" ? value : JSON.stringify(value)}`);

const summarise = (items: Item[]) =>
  items.map((i) => `${i.ref.key} ${i.openness}${i.claimedBy ? ` @${i.claimedBy}` : ""}`);

async function main() {
  const db = new FakeProvider();
  const tracker = new FakeTrackerAdapter(db);

  // --- 1. the map and its children -----------------------------------------

  step("1.", "Chart a small map");

  const map = db.insert({
    parentId: null,
    title: "Ship the pilot feature",
    body: [
      "## Destination",
      "",
      "The pilot feature delivered through the workflow end to end.",
      "",
      "## Decisions so far",
      "",
      "## Not yet specified",
      "",
      "* How failures are surfaced to the user.",
      "",
    ].join("\n"),
    status: "todo",
    assignee: null,
    labels: ["kind:decision-map"],
    blockedBy: [],
    comments: [],
    artifacts: [],
    delivery: { workspace: null, branch: null, pullRequest: null },
  });

  const shape = await tracker.createChild(map.id, {
    kind: "decision-ticket",
    title: "Decide the storage shape",
    body: "## Question\n\nWhat shape does the record take?",
    decisionType: "grilling",
  });
  const readFacts = await tracker.createChild(map.id, {
    kind: "decision-ticket",
    title: "Establish the provider's limits",
    body: "## Question\n\nWhat can the provider actually do?",
    decisionType: "research",
  });
  const build = await tracker.createChild(map.id, {
    kind: "decision-ticket",
    title: "Define the write path",
    body: "## Question\n\nHow are writes ordered?",
    decisionType: "prototype",
  });

  await tracker.addBlocker(shape.id, readFacts.id);
  await tracker.addBlocker(build.id, shape.id);

  show("map", map.key);
  show("children", [shape.key, readFacts.key, build.key]);
  show("blocking", `${readFacts.key} → ${shape.key} → ${build.key}`);

  // --- 2. the frontier ------------------------------------------------------

  step("2.", "Compute the frontier — the provider cannot be asked for it");

  let children = await tracker.listChildren(map.id, { withRelations: true });
  show("all children", summarise(children));
  show("frontier", summarise(frontier(children)));
  line();
  line(`   Only ${readFacts.key} is takeable. The other two carry live blockers.`);

  // --- 3. the claim race ----------------------------------------------------

  step("3.", "Two sessions claim the same ticket");

  const racy = new FakeTrackerAdapter(db, {
    // Session B lands between session A's write and its verification.
    onBeforeClaimVerify: (id) => db.update(id, { assignee: "session-b" }),
  });

  const resultA = await racy.claim(readFacts.id, "session-a");
  show("session-a result", resultA);
  show("holder now", (await tracker.getItem(readFacts.id)).claimedBy);
  line();
  line("   The provider accepted both writes and raised nothing. The adapter re-read and");
  line("   told session-a it lost. Without that read-back, two sessions work one ticket.");

  await tracker.claim(readFacts.id, "session-b"); // let B carry on

  // --- 4. resolving --------------------------------------------------------

  step("4.", "Resolve a ticket: comment, close, index — in that order");

  await tracker.attachArtifact(readFacts.id, {
    kind: "document",
    id: "doc-1",
    url: "https://tracker.example/doc-1",
    title: "Provider limits — findings",
  });
  await tracker.postResolution(readFacts.id, {
    tldr: "The provider has no compare-and-swap and no server-side dependency resolution.",
    body: "## Findings\n\n...",
  });
  await tracker.close(readFacts.id, "resolved");

  const indexLine = `* ${tracker.renderRef(readFacts, "Establish the provider's limits")} - No compare-and-swap; the frontier is computed client-side.\n`;
  await tracker.patchBody(map.id, [
    { op: "insertBefore", anchor: "## Not yet specified", text: indexLine + "\n" },
  ]);

  show("comment", (await tracker.listComments(readFacts.id))[0]?.body.split("\n")[0]);
  show("artifacts", (await tracker.listArtifacts(readFacts.id)).map((a) => a.kind));
  show("openness", (await tracker.getItem(readFacts.id)).openness);
  line();
  line("   The answer is written before the bookkeeping. A crash after the comment leaves");
  line("   the decision recorded and the index behind; the reverse order loses the answer.");

  children = await tracker.listChildren(map.id, { withRelations: true });
  show("frontier now", summarise(frontier(children)));
  line(`   ${shape.key} became takeable — its blocker closed, though the relation remains.`);

  // --- 5. concurrent map writes --------------------------------------------

  step("5.", "Two sessions append to the map index at once");

  await tracker.patchBody(map.id, [{ op: "append", text: "* Session C's line\n" }]);
  await tracker.patchBody(map.id, [{ op: "append", text: "* Session D's line\n" }]);
  show("both survived", db.get(map.id).body.includes("Session C") && db.get(map.id).body.includes("Session D"));
  line("   Each patch is resolved against the body the provider currently holds, so neither");
  line("   session had to read first and neither line was lost. A whole-body write would have");
  line("   destroyed one — which is why the contract has no operation that expresses it.");

  line();
  try {
    await tracker.patchBody(map.id, [
      { op: "replace", find: "## Decisions so far", text: "## Decisions" },
      { op: "insertBefore", anchor: "## Decisions so far", text: "x" },
    ]);
  } catch (err) {
    show("stale anchor", (err as Error).message);
  }
  show("body untouched", db.get(map.id).body.includes("## Decisions so far"));
  line("   The anchor is the only precondition available. It failed loudly and rolled back.");

  // --- 6. delivery ---------------------------------------------------------

  step("6.", "An Implementation Ticket moves through delivery");

  const impl = await tracker.createChild(map.id, {
    kind: "implementation-ticket",
    title: "Add the record writer",
  body: "## Execution Brief\n\n...",
  });
  await tracker.setDelivery(impl.id, {
    workspace: `/workspaces/pilot/${impl.key}`,
    branch: `feature/${impl.key.toLowerCase()}-record-writer`,
  });

  for (const phase of [
    "briefed",
    "planning",
    "awaiting-plan-approval",
    "implementing",
    "reviewing",
    "awaiting-merge",
  ] as const) {
    await tracker.setPhase(impl.id, phase);
    const read = await tracker.getItem(impl.id);
    show(phase, `status ${db.get(impl.id).status} · round-trips: ${read.phase === phase}`);
  }
  show("delivery", (await tracker.getItem(impl.id)).delivery);
  line();
  line("   Every phase is its own status, so the provider's board shows delivery as columns");
  line("   and a human watching two tickets in flight can read where each one is.");
  line("   The key travels outside the tracker — it is in the workspace path and the branch,");
  line("   and it is what a running worker's title carries. Nothing else binds the two.");

  // --- 7. the lossy projection ---------------------------------------------

  step("7.", "The same phases on a provider whose statuses cannot be extended");

  const lossy = new FakeTrackerAdapter(db, { lossyPhase: true });
  for (const phase of ["planning", "implementing", "reviewing", "awaiting-merge"] as const) {
    await lossy.setPhase(impl.id, phase);
    const read = await lossy.getItem(impl.id);
    show(phase, read.phase === phase ? "round-trips: true" : `reads back as ${read.phase}`);
  }
  line();
  line("   Folded onto fewer statuses, two phases become one and the read cannot tell them");
  line("   apart — the board loses the columns and the round-trip guarantee is violated. An");
  line("   adapter in this position has to carry the phase somewhere else and hand it back");
  line("   intact; it declares the loss of the board through nativePhases: false.");
  show("capabilities", lossy.capabilities);

  // --- 8. needs attention --------------------------------------------------

  step("8.", "The Implementer fails and the ticket needs the user");

  await tracker.setPhase(impl.id, "implementing");
  await tracker.setAttention(impl.id, {
    needs: true,
    reason: "Implementer exhausted its retries on the failing integration test.",
    raisedAt: now(),
  });

  const flagged = await tracker.listNeedingAttention(map.id);
  show("one query", flagged.map((i) => i.ref.key));
  show("phase kept", (await tracker.getItem(impl.id)).phase);
  show("reason", (await tracker.getItem(impl.id)).attention);
  line();
  line("   Attention is a flag, not a phase: the phase is what tells the user what they are");
  line("   deciding about. It is findable in one query rather than by scanning every ticket,");
  line("   and it carries why — a bare flag sends the user back to the transcripts.");

  await tracker.setAttention(impl.id, { needs: false });
  show("after the user acts", (await tracker.listNeedingAttention(map.id)).length);

  line();
  line(`${"═".repeat(76)}`);
  line("Open questions this raises are listed in the ticket, not here.");
  line(`${"═".repeat(76)}`);
  line();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
