/**
 * PROTOTYPE — Execution DAG schema and validation rules (GRE-154). Throwaway; react to
 * it, don't build on it.
 *
 * The Execution DAG is the only explicit build graph. It says what has to be built, what
 * has to be built before what, and what must not be built at the same time. It says
 * nothing about where any of it stands.
 *
 * Four rules shape everything below:
 *
 *   1. The DAG holds stable topology only. There is no status, no assignee, no workspace,
 *      no branch, no pull request, no timestamp, no progress of any kind — those live in
 *      the tracker, and a field here that mirrored one would immediately be a second
 *      answer to a question that already has one.
 *   2. Nothing points from the DAG into the tracker. The DAG is written before any ticket
 *      exists, so a ticket key on a node would be a hole waiting to be filled — which is
 *      mutable state. The tracker item carries the node id instead. One direction only.
 *   3. Every rule below is decidable by reading the file. No rule asks anyone to judge
 *      whether a plan is good; a validator that needed judgement would be a reviewer, and
 *      would fail differently every time it ran.
 *   4. Parallelism is derived, never declared. A field saying "these two can run together"
 *      would be a claim the graph could contradict.
 *
 * Written against settled decisions, none of which is reopened:
 *
 *   GRE-138  the Decision Map and the Execution DAG are different artifacts.
 *   GRE-139  the DAG owns stable dependencies and constraints; the tracker owns status,
 *            assignment, workspace, commits, and pull requests.
 *   GRE-140  one Implementation Ticket is one delivery unit — one workspace, one branch,
 *            one reviewable pull request — and dependent work waits for merge.
 *   GRE-144  the user merges, and the merge unlocks successors.
 *   GRE-149  at most two tickets in flight, human gates serialized. Policy, not topology.
 *   GRE-153  the Ticket Key is what ties tracker items to running work.
 */

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

/**
 * A node's identity: chosen by the author, stable for the life of the graph, and
 * meaningful to a human reading a diff.
 *
 * Not generated. A generated id makes every re-plan look like a rewrite, and the graph is
 * reviewed as a diff before anything is built.
 */
export type NodeId = string;

/**
 * The Ticket Key of a *closed Decision Ticket* — a decision this node must honour.
 *
 * The DAG never restates a decision. It names the ones that bind, and the Execution Brief
 * follows the pointer. A restated decision is a copy that drifts, and the map already had
 * the argument.
 */
export type DecisionRef = string;

/**
 * A path prefix inside the repository. A node's Write Scope is a set of these.
 *
 * This is the load-bearing field of the whole schema, and the one most likely to be
 * argued with. It exists because two nodes with no dependency between them may still be
 * unable to run at the same time — not because of ordering, but because they would be
 * editing the same files in two workspaces and one of them would lose the merge. Nothing
 * else in the graph can see that, so without this field "these may run in parallel" is a
 * guess.
 *
 * Prefixes rather than globs, on purpose: prefix overlap is decidable by string
 * comparison, and a glob language would put a matcher's edge cases between the plan and
 * the answer.
 */
export type PathPrefix = string;

// ---------------------------------------------------------------------------
// Nodes
// ---------------------------------------------------------------------------

/**
 * One node, and there is only one kind: an Implementation Ticket that does not exist yet.
 *
 * A second kind was drafted and dropped. It was a planning node — one shared plan written
 * once for several tickets that had to agree on something none of them could settle
 * alone. Three things killed it:
 *
 *   - A shared plan is a promise two implementers then have to keep separately, in two
 *     branches. If one of them reads it differently, nothing notices until the second
 *     merge — which is the failure the shared plan existed to prevent. It does not
 *     prevent it; it postpones it.
 *   - Whatever the tickets had to agree on is *work*. Someone has to write it. Writing it
 *     is a delivery unit, so it is an ordinary node with a workspace, a branch, a pull
 *     request and acceptance criteria — all four of which a planning node lacked, which
 *     is why it did not fit GRE-140 in the first place.
 *   - Every rule that tried to police it — is this plan serving more than one ticket? —
 *     had to infer intent from graph shape, and the shape moves for unrelated reasons. An
 *     edge added because two nodes wrote the same directory silently changed the verdict
 *     on whether a plan was worth writing. There is no correct version of that rule.
 *
 * What replaces it is a choice made when the graph is authored, not a node kind: work
 * that has to agree on something is either **one node**, or a node the others depend on.
 * Which one is a question of Context Budget and Review Budget — how much a single Role
 * Context can hold, and how much a person can review in one pass. Both are judgement, so
 * neither appears below; a split is justified by exceeding one of them, never by the work
 * feeling like separate things.
 */
export type Node = {
  id: NodeId;
  title: string;
  /** What this delivers and why, in a paragraph. Becomes the Execution Brief's opening. */
  intent: string;
  /**
   * What has to be true for this to be done. Each entry stands on its own and is checkable
   * without reading the others — a criterion that needs the rest of the list for context is
   * one criterion split in half.
   */
  acceptance: string[];
  /** The Write Scope: everything this node is expected to write. Empty is invalid. */
  touches: PathPrefix[];
  constraints: DecisionRef[];
};

// ---------------------------------------------------------------------------
// Edges
// ---------------------------------------------------------------------------

/**
 * The only relationship in the graph: `from` unlocks `to`, and `to` may not start until
 * `from` has merged.
 *
 * There is exactly one edge kind, and no field on it. The merge gate is therefore not a
 * node and not a flag — it is what an edge *means*, everywhere, with no way to express a
 * weaker one.
 *
 * A softer edge — "may start once the branch exists" — is what stacked pull requests need,
 * and stacking is out of scope for v0.
 */
export type Edge = {
  from: NodeId;
  to: NodeId;
};

export type ExecutionDag = {
  /** Names the effort. Not an id; nothing references it. */
  name: string;
  nodes: Node[];
  edges: Edge[];
};

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

/**
 * Every rule has a stable code, and every failure names the nodes involved.
 *
 * Codes rather than prose matching because the thing that consumes findings first is a
 * script, and a script keyed on a message string breaks the day someone improves the
 * wording. Retired codes are never reused — a gap is cheaper than a code that means two
 * things in two versions of a file people keep copies of.
 */
export type FindingCode =
  | "DAG-001" // two nodes share an id
  | "DAG-002" // an edge names a node that does not exist
  | "DAG-003" // a node depends on itself
  | "DAG-004" // a cycle
  | "DAG-005" // the same edge twice
  | "DAG-006" // a redundant edge the graph already implies
  // DAG-007 and DAG-013 policed the planning node and retired with it.
  | "DAG-008" // a node with an empty Write Scope
  | "DAG-009" // a constraint naming a decision that is not closed, or does not exist
  | "DAG-010" // a node with no acceptance criteria
  // DAG-011 was "a checkpoint that gates nothing" and retired with the checkpoint node.
  | "DAG-012"; // two nodes free to run together, sharing a Write Scope

export type Finding = {
  code: FindingCode;
  nodes: NodeId[];
  message: string;
};

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/**
 * Every finding is an error. There is no warning level.
 *
 * A severity ladder is a way of shipping a graph that fails its own rules, and the graph
 * is the input to everything downstream — briefs, ordering, the parallelism the runtime
 * will actually attempt. A rule worth writing is worth stopping for; a rule not worth
 * stopping for should be deleted rather than demoted.
 *
 * `closedDecisions` is passed in rather than read from the tracker, because validation has
 * to run offline, in review, on a diff. The caller fetches; this function decides.
 */
export function validate(dag: ExecutionDag, closedDecisions: Set<string>): Finding[] {
  const findings: Finding[] = [];
  const add = (code: FindingCode, nodes: NodeId[], message: string) =>
    findings.push({ code, nodes, message });

  // --- ids ---

  const byId = new Map<NodeId, Node>();
  const seen = new Set<NodeId>();
  for (const node of dag.nodes) {
    if (seen.has(node.id)) {
      add("DAG-001", [node.id], `two nodes share the id "${node.id}"`);
      continue;
    }
    seen.add(node.id);
    byId.set(node.id, node);
  }

  // --- edges refer to real nodes, and not to themselves ---

  const edges: Edge[] = [];
  const edgeSeen = new Set<string>();
  for (const edge of dag.edges) {
    const key = `${edge.from} ${edge.to}`;
    if (!byId.has(edge.from) || !byId.has(edge.to)) {
      const missing = !byId.has(edge.from) ? edge.from : edge.to;
      add("DAG-002", [edge.from, edge.to], `edge names unknown node "${missing}"`);
      continue;
    }
    if (edge.from === edge.to) {
      add("DAG-003", [edge.from], `"${edge.from}" depends on itself`);
      continue;
    }
    if (edgeSeen.has(key)) {
      add("DAG-005", [edge.from, edge.to], `the edge ${edge.from} → ${edge.to} appears twice`);
      continue;
    }
    edgeSeen.add(key);
    edges.push(edge);
  }

  const successors = new Map<NodeId, NodeId[]>();
  for (const id of byId.keys()) successors.set(id, []);
  for (const edge of edges) successors.get(edge.from)!.push(edge.to);

  // --- cycles ---
  //
  // Reported as the path around the cycle, not as "a cycle exists". A cycle in a graph of
  // thirty nodes that only says it is there costs an afternoon to find by hand.

  const cycle = findCycle(byId, successors);
  if (cycle) {
    add("DAG-004", cycle, `cycle: ${cycle.join(" → ")} → ${cycle[0]}`);
    // Everything below assumes reachability is well-founded. Stop while it isn't.
    return findings;
  }

  const unlocks = transitiveClosure(byId, successors);

  // --- redundant edges ---
  //
  // A → C is redundant when A already unlocks C some other way. It is not harmless: the
  // graph is read by humans to decide what may run when, and an edge that changes nothing
  // costs a reader the time to work out that it changes nothing.

  for (const edge of edges) {
    const otherPaths = successors
      .get(edge.from)!
      .filter((mid) => mid !== edge.to)
      .some((mid) => unlocks.get(mid)!.has(edge.to));
    if (otherPaths) {
      add(
        "DAG-006",
        [edge.from, edge.to],
        `${edge.from} → ${edge.to} is already implied by a longer path`,
      );
    }
  }

  // --- per-node rules ---

  for (const node of byId.values()) {
    for (const ref of node.constraints) {
      if (!closedDecisions.has(ref)) {
        add(
          "DAG-009",
          [node.id],
          `${node.id} is constrained by "${ref}", which is not a closed decision`,
        );
      }
    }
    if (node.touches.length === 0) {
      add("DAG-008", [node.id], `${node.id} declares no Write Scope, so no collision can be seen`);
    }
    if (node.acceptance.length === 0) {
      add("DAG-010", [node.id], `${node.id} has no acceptance criteria`);
    }
  }

  // --- collisions between nodes the graph leaves free to run together ---
  //
  // Two nodes are concurrent when neither unlocks the other. Concurrent nodes sharing a
  // Write Scope are the one failure this schema exists to catch: the graph promises
  // parallelism the workspaces cannot deliver, and the cost lands at the second merge,
  // after both are built.

  const nodes = [...byId.values()];
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i];
      const b = nodes[j];
      if (unlocks.get(a.id)!.has(b.id) || unlocks.get(b.id)!.has(a.id)) continue;
      const shared = overlappingPrefixes(a.touches, b.touches);
      if (shared.length > 0) {
        add(
          "DAG-012",
          [a.id, b.id],
          `${a.id} and ${b.id} may run at the same time and both write ${shared.join(", ")}`,
        );
      }
    }
  }

  return findings;
}

/** Prefix overlap in either direction. `src/api` and `src/api/routes` collide. */
function overlappingPrefixes(a: PathPrefix[], b: PathPrefix[]): string[] {
  const hits: string[] = [];
  for (const x of a) {
    for (const y of b) {
      if (x === y || x.startsWith(`${y}/`) || y.startsWith(`${x}/`)) {
        hits.push(x.length <= y.length ? x : y);
      }
    }
  }
  return [...new Set(hits)].sort();
}

function transitiveClosure(
  byId: Map<NodeId, Node>,
  successors: Map<NodeId, NodeId[]>,
): Map<NodeId, Set<NodeId>> {
  const closure = new Map<NodeId, Set<NodeId>>();

  const walk = (id: NodeId): Set<NodeId> => {
    const cached = closure.get(id);
    if (cached) return cached;
    const out = new Set<NodeId>();
    closure.set(id, out);
    for (const next of successors.get(id) ?? []) {
      out.add(next);
      for (const far of walk(next)) out.add(far);
    }
    return out;
  };

  for (const id of byId.keys()) walk(id);
  return closure;
}

function findCycle(
  byId: Map<NodeId, Node>,
  successors: Map<NodeId, NodeId[]>,
): NodeId[] | null {
  const state = new Map<NodeId, "open" | "done">();
  const stack: NodeId[] = [];

  const walk = (id: NodeId): NodeId[] | null => {
    if (state.get(id) === "done") return null;
    if (state.get(id) === "open") return stack.slice(stack.indexOf(id));
    state.set(id, "open");
    stack.push(id);
    for (const next of successors.get(id) ?? []) {
      const found = walk(next);
      if (found) return found;
    }
    stack.pop();
    state.set(id, "done");
    return null;
  };

  for (const id of byId.keys()) {
    const found = walk(id);
    if (found) return found;
  }
  return null;
}

// ---------------------------------------------------------------------------
// What the graph implies
// ---------------------------------------------------------------------------

/**
 * The structural waves: everything in wave N is unlocked only by earlier waves.
 *
 * This is the maximum parallelism the topology allows, and deliberately not a schedule.
 * How many tickets actually run at once is core policy — two, per GRE-149 — and how many
 * *should* is a question about the user's attention, not about the graph. A schema that
 * carried a concurrency number would be answering a question it cannot see.
 *
 * Ordering within a wave is by node id so the output is stable across runs and reviewable
 * as a diff.
 */
export function waves(dag: ExecutionDag): NodeId[][] {
  const remaining = new Map<NodeId, Set<NodeId>>();
  for (const node of dag.nodes) remaining.set(node.id, new Set());
  for (const edge of dag.edges) remaining.get(edge.to)?.add(edge.from);

  const out: NodeId[][] = [];
  const done = new Set<NodeId>();

  while (done.size < remaining.size) {
    const ready = [...remaining.entries()]
      .filter(([id, deps]) => !done.has(id) && [...deps].every((d) => done.has(d)))
      .map(([id]) => id)
      .sort();
    if (ready.length === 0) throw new Error("waves() called on a cyclic graph; validate first");
    for (const id of ready) done.add(id);
    out.push(ready);
  }
  return out;
}

/**
 * Human-readable output, generated and never hand-edited.
 *
 * Generated because the diagram is the artifact people actually argue over, and a
 * hand-drawn one drifts from the graph within a week — at which point the review is of a
 * picture nothing runs on.
 */
export function toMermaid(dag: ExecutionDag): string {
  const lines = ["flowchart TD"];
  const label = (s: string) => s.replace(/"/g, "'");

  for (const node of [...dag.nodes].sort((a, b) => a.id.localeCompare(b.id))) {
    lines.push(`  ${node.id}["${label(node.title)}"]`);
  }

  const edges = [...dag.edges].sort((a, b) =>
    a.from === b.from ? a.to.localeCompare(b.to) : a.from.localeCompare(b.from),
  );
  for (const edge of edges) lines.push(`  ${edge.from} --> ${edge.to}`);

  return lines.join("\n");
}

/**
 * The Execution Brief for one node, generated from the graph alone.
 *
 * Here to prove a claim the schema makes rather than to be the real template: everything
 * an Implementation Ticket needs before local planning is already in the graph, so the
 * brief is a rendering and not a document someone writes. The moment it needs a field the
 * graph does not have, either the schema is short one field or the brief is carrying
 * something that belongs to the tracker.
 */
export function toBrief(dag: ExecutionDag, id: NodeId): string {
  const node = dag.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node "${id}"`);

  const upstream = dag.edges.filter((e) => e.to === id).map((e) => e.from).sort();

  return [
    `# ${node.title}`,
    "",
    "## Intent",
    "",
    node.intent,
    "",
    "## Acceptance",
    "",
    ...node.acceptance.map((a) => `- ${a}`),
    "",
    "## Write Scope",
    "",
    `This ticket writes: ${node.touches.join(", ")}. Anything outside belongs to another node.`,
    "",
    "## Unlocked by (all merged before this starts)",
    "",
    upstream.length > 0 ? upstream.map((u) => `- ${u}`).join("\n") : "- nothing",
    "",
    "## Decisions that bind this work",
    "",
    ...node.constraints.map((c) => `- ${c}`),
  ].join("\n");
}

// ---------------------------------------------------------------------------
// Left to other tickets, on purpose
// ---------------------------------------------------------------------------

/**
 * - How a node and its tracker item find each other once tickets exist, who wins when they
 *   disagree, and how a re-plan lands on a graph with work already merged under it. That is
 *   "Define progress authority, discovery, and reconciliation" (GRE-155), which this ticket
 *   blocks. The schema deliberately carries no tracker pointer so that the policy has
 *   somewhere to put one.
 *
 * - What the Review Budget actually is, counted in changed lines, and what may exceed it.
 *   That is "Define changed-line counting and delivery-budget exceptions" (GRE-156). One
 *   of the two ceilings that decide whether work is one node or several; neither is
 *   checkable here, because both are judgement.
 *
 * - How many Implementation Tickets run at once and how human gates are serialized. Core
 *   policy from GRE-149; `waves()` reports what the topology allows and stops there.
 *
 * - Where a stop condition lives, now that the graph has no checkpoint node to hang one on.
 *   Parked at low priority as GRE-165.
 */
