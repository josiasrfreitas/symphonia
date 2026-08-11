/**
 * PROTOTYPE — Tracker Adapter contract (GRE-153). Throwaway; react to it, don't build on it.
 *
 * The Tracker Adapter is the boundary through which the workflow creates, relates,
 * queries and updates canonical tracker artifacts and mutable delivery state.
 *
 * Two rules shape everything below:
 *
 *   1. Nothing provider-specific crosses this boundary. No issue, label, status name,
 *      workspace, team, or project appears in any type here.
 *   2. Where the provider is weak, the contract makes the weakness unrepresentable
 *      rather than documented. A whole-body write cannot be expressed. A claim cannot
 *      report success without having verified it.
 *
 * Written against the facts established in "Establish Linear hierarchy and Wayfinding
 * operation semantics" (GRE-152): no optimistic locking anywhere, no server-side
 * "unblocked" query, dependency relations carry no resolved state, uploaded attachments
 * cannot be read back, comment threads cannot be resolved through the API.
 */

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

/**
 * Three identities for one item, because three different consumers need different things.
 *
 *   id   opaque, durable, provider-issued. The only value used to address the item.
 *   key  short and human-visible. This is the correlation token that travels outside
 *        the tracker — into runtime task titles and display names, into branch names,
 *        into commit messages. "Decide the Orca control plane..." (GRE-149) established
 *        that nothing in the runtime binds a task to a tracker item, so the key is the
 *        only thing tying a running worker back to the work it is doing.
 *   url  for humans only. Never parsed.
 *
 * A provider that has no short stable key must synthesise one and store it. Deriving the
 * key from the id at read time is not allowed — it has to survive the provider changing
 * its URL scheme.
 */
export type ItemRef = {
  id: string;
  key: string;
  url: string;
};

export type ActorId = string;

// ---------------------------------------------------------------------------
// What the tracker holds
// ---------------------------------------------------------------------------

export type ItemKind = "decision-map" | "decision-ticket" | "implementation-ticket";

/** The four Decision Ticket types. Only meaningful when kind is "decision-ticket". */
export type DecisionType = "research" | "prototype" | "grilling" | "task";

/**
 * The whole of what the contract asks of a provider's status model: is it still live?
 *
 * This is deliberate. Provider status models differ in size and in meaning, and every
 * finer distinction the workflow needs is carried by `phase` below, which the workflow
 * owns outright. Openness is the one thing the frontier calculation depends on, and it
 * is the one thing every tracker agrees on.
 */
export type Openness = "open" | "closed";

/**
 * Delivery phase of an Implementation Ticket. Workflow-owned vocabulary, from the
 * decisions that produced the pipeline: brief, plan, plan approval, implement, review,
 * pull request, merge.
 *
 * Each phase gets its own native status on the provider, one to one. The reason is the
 * board: a human watching delivery reads it as columns, and a coarse projection that puts
 * planning, implementing and review under one status makes the board useless exactly when
 * two tickets are in flight and it matters most.
 *
 * One to one is also what makes the round-trip guarantee free rather than something the
 * adapter has to work at. A provider that cannot add statuses must store the phase
 * elsewhere and still return what it was given — see `nativePhases` below.
 */
export type DeliveryPhase =
  | "briefed"
  | "planning"
  | "awaiting-plan-approval" // human gate
  | "implementing"
  | "reviewing"
  | "awaiting-merge" // human gate
  | "merged"
  | "abandoned";

/**
 * "Needs attention" — the concrete tracker representation that "Decide how model and
 * reasoning effort are selected per role" (GRE-161) deferred to this ticket.
 *
 * A failing role stops its Implementation Ticket and the user chooses what happens next.
 * That state has three requirements, and they are what make it its own field rather than
 * another phase:
 *
 *   - It is orthogonal to phase. A ticket needs attention *at* some phase, and the phase
 *     is what tells the user what they are deciding about.
 *   - It must be findable in one query, not by scanning every ticket. `listNeedingAttention`
 *     is the only query in this contract the adapter is required to answer server-side.
 *   - It must carry why. A flag with no reason sends the user back to the transcripts.
 */
export type Attention =
  | { needs: false }
  | { needs: true; reason: string; raisedAt: string };

/** Mutable delivery state. The tracker owns this; the Execution DAG owns topology only. */
export type Delivery = {
  workspace: string | null; // path of the isolated workspace
  branch: string | null;
  pullRequest: string | null; // url
};

export type Item = {
  ref: ItemRef;
  kind: ItemKind;
  title: string;
  body: string;
  openness: Openness;
  /** The claim. Null means unclaimed and therefore takeable. */
  claimedBy: ActorId | null;
  decisionType: DecisionType | null;
  phase: DeliveryPhase | null;
  attention: Attention;
  delivery: Delivery;
  /** Present only when the item was read with relations; null means "not fetched". */
  blockedBy: ItemRef[] | null;
};

// ---------------------------------------------------------------------------
// Body edits
// ---------------------------------------------------------------------------

/**
 * There is no operation that writes a whole body, and that absence is the point.
 *
 * The map's index grows one line per resolved decision and several sessions may write it
 * at once. With no optimistic locking, a whole-body write silently destroys a concurrent
 * edit. A patch is resolved against the body the provider currently holds, so the
 * concurrent edit is already there and survives.
 *
 * `insertBefore` and `replace` carry an implicit precondition: the anchor must match
 * exactly once. The adapter must fail loudly when it doesn't — that failure is the only
 * compare-and-swap available, and swallowing it turns a caught conflict into a lost write.
 * `append` has no anchor and therefore cannot fail this way; prefer it.
 */
export type BodyOp =
  | { op: "append"; text: string }
  | { op: "insertBefore"; anchor: string; text: string }
  | { op: "replace"; find: string; text: string };

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

/**
 * Two artifact homes, and no third.
 *
 *   repo-file  contracts, schemas, anything reviewed as a diff. Lives in git; the tracker
 *              holds a pointer. This is the only option with line diffs, review and blame.
 *   document   research findings and prototype write-ups that must be readable inside the
 *              tracker without leaving it.
 *
 * Uploaded file attachments are excluded on purpose: on the pilot provider nothing can
 * read their bytes back, so an artifact put there is invisible to every later session.
 * An adapter for a provider with no document surface must degrade to repo-file, never to
 * an upload.
 */
export type Artifact =
  | { kind: "repo-file"; path: string; url: string; title: string }
  | { kind: "document"; id: string; url: string; title: string };

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

/**
 * The little that genuinely varies between trackers. Everything else the adapter is
 * required to emulate.
 *
 * `nativeBlocking: false` means the provider has no dependency relation and the adapter
 * keeps one in the body. The core does not care which it is — it reads `blockedBy` either
 * way — but the human loses the visual frontier in the provider's own UI, which is a
 * reason to prefer a provider that has it.
 */
export type TrackerCapabilities = {
  nativeBlocking: boolean;
  documents: boolean;
  /**
   * True when every DeliveryPhase has its own native status, so the provider's own board
   * shows delivery as columns. False means the adapter carries the phase some other way
   * and the board only shows open versus closed — the round-trip guarantee holds either
   * way, but the human loses the view.
   */
  nativePhases: boolean;
};

// ---------------------------------------------------------------------------
// The contract
// ---------------------------------------------------------------------------

export type ClaimResult =
  | { ok: true }
  | { ok: false; heldBy: ActorId }; // someone else got there first

export type CloseOutcome = "resolved" | "out-of-scope" | "abandoned";

export type Comment = {
  id: string;
  author: ActorId;
  authorName: string;
  createdAt: string;
  body: string;
};

export interface TrackerAdapter {
  readonly capabilities: TrackerCapabilities;

  // --- reads ---

  getItem(id: string, opts?: { withRelations?: boolean }): Promise<Item>;

  /**
   * Every child of the map in one call where the provider allows it, plus one call per
   * open child when relations are requested — the frontier cannot be asked for as a
   * query, so it is computed from this (see `frontier` below).
   *
   * The cost is O(open children), and it shrinks as the map is resolved.
   */
  listChildren(mapId: string, opts?: { withRelations?: boolean }): Promise<Item[]>;

  /** The one query the adapter must answer server-side rather than by scanning. */
  listNeedingAttention(mapId: string): Promise<Item[]>;

  listComments(id: string): Promise<Comment[]>;

  listArtifacts(id: string): Promise<Artifact[]>;

  /** Must work for every artifact kind the adapter can produce. */
  readArtifact(artifact: Artifact): Promise<string>;

  // --- structure ---

  createChild(
    parentId: string,
    spec: {
      kind: ItemKind;
      title: string;
      body: string;
      decisionType?: DecisionType;
    },
  ): Promise<ItemRef>;

  addBlocker(id: string, blockerId: string): Promise<void>;
  removeBlocker(id: string, blockerId: string): Promise<void>;

  // --- writes ---

  patchBody(id: string, ops: BodyOp[]): Promise<void>;

  /**
   * Write, then read back, then answer. Never optimistic.
   *
   * Nothing in the pilot provider is compare-and-swap, so two sessions can both write a
   * claim and both believe they hold it. The adapter is required to re-read and report
   * the loss. A caller that gets `{ok: true}` may proceed; that is the whole point of
   * putting the verification behind the boundary instead of in front of it.
   */
  claim(id: string, actor: ActorId): Promise<ClaimResult>;
  release(id: string, actor: ActorId): Promise<void>;

  close(id: string, outcome: CloseOutcome): Promise<void>;

  setPhase(id: string, phase: DeliveryPhase): Promise<void>;
  setAttention(id: string, attention: Attention): Promise<void>;
  setDelivery(id: string, delivery: Partial<Delivery>): Promise<void>;

  /** Put or lift the sign that a Human Gate is waiting. Human Gate is a workflow
   * concept, not a provider one — every adapter must expose it. */
  setGate(id: string, waiting: boolean): Promise<void>;

  postComment(id: string, body: string): Promise<Comment>;

  /**
   * A resolution is a comment with a mandatory plain answer at the top.
   *
   * Taking `tldr` as its own argument rather than trusting a convention is the cheapest
   * possible enforcement: a resolution without one cannot be expressed.
   */
  postResolution(id: string, resolution: { tldr: string; body: string }): Promise<Comment>;

  /** The tracker half of automatic gate recording: one templated comment on the
   * ticket, TLDR first. */
  recordGate(
    ticket: string,
    gate: string,
    decision: string,
    evidence: string,
  ): Promise<Comment>;

  attachArtifact(id: string, artifact: Artifact): Promise<void>;

  /**
   * Render a reference for inclusion in a body.
   *
   * Not a formatting nicety. On the pilot provider a bare item URL dropped into a body
   * silently creates a real relation, so a map indexing twenty decisions would grow twenty
   * phantom relations — in the same relation graph the frontier calculation reads. Bodies
   * are built from this function; they never carry raw URLs.
   */
  renderRef(ref: ItemRef, label: string): string;
}

// ---------------------------------------------------------------------------
// Guarantees the adapter owes the core
// ---------------------------------------------------------------------------

/**
 * 1. ROUND-TRIP. Anything the core writes reads back equal. This is what lets `phase` be
 *    projected freely onto whatever the provider offers: an adapter may store it however
 *    it likes as long as `getItem` returns what `setPhase` was given. It rules out lossy
 *    projections — collapsing two phases onto one provider status is not allowed.
 *
 * 2. LOUD ANCHORS. An anchor that matches zero or several times is an error, never a
 *    best-effort write, and a multi-op patch that fails partway leaves the body untouched.
 *
 * 3. VERIFIED CLAIMS. `claim` never returns ok without having re-read the item.
 *
 * 4. NO VOCABULARY LEAKS. Provider status names, label names and ids never appear in any
 *    value returned from this interface.
 *
 * 5. DURABLE-FIRST ORDERING. A composite operation performs its append-only step first.
 *    Resolving is comment, then close, then map index: a crash after step one leaves the
 *    answer recorded and the bookkeeping behind, which is recoverable. The reverse order
 *    loses the answer.
 */

// ---------------------------------------------------------------------------
// Core-side logic — deliberately not in the adapter
// ---------------------------------------------------------------------------

/**
 * The frontier: open, unclaimed, and every blocker closed.
 *
 * A pure function over items the adapter already returned, because "unblocked" is a
 * workflow rule and not a provider capability — no tracker can express it as a query, and
 * putting it in the adapter would mean re-implementing it for every provider.
 *
 * The trap it exists to avoid: a non-empty blocker list does not mean blocked. Dependency
 * relations carry no resolved state, so a ticket whose only blocker closed months ago
 * still reports that blocker. Reading the relation without reading the blocker's openness
 * hides takeable work.
 */
export function frontier(children: Item[]): Item[] {
  const openness = new Map(children.map((c) => [c.ref.id, c.openness]));

  return children.filter((item) => {
    if (item.kind === "decision-map") return false;
    if (item.openness !== "open") return false;
    if (item.claimedBy !== null) return false;
    if (item.blockedBy === null) {
      throw new Error(`frontier() needs relations; ${item.ref.key} was read without them`);
    }
    return item.blockedBy.every((b) => openness.get(b.id) === "closed");
  });
}

// ---------------------------------------------------------------------------
// Left to other tickets, on purpose
// ---------------------------------------------------------------------------

/**
 * - Who is authoritative when the tracker and the runtime disagree, how a session
 *   discovers work in progress it did not start, and how a half-finished resolve is
 *   reconciled. That is "Define progress authority, discovery, and reconciliation"
 *   (GRE-155), which this ticket blocks. The contract deliberately exposes the pieces
 *   (append-only comments, durable-first ordering) without deciding the policy.
 *
 * - Notifying the user's phone that a ticket needs attention. Wanted, deferred, and
 *   sitting in the map's fog. `listNeedingAttention` is the hook it will need.
 */
