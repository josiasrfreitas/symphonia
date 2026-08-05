/**
 * PROTOTYPE — runnable harness for the Execution DAG schema (GRE-154). Throwaway.
 *
 *   node docs/contracts/execution-dag.harness.prototype.ts
 *
 * A small real-shaped graph — the brownfield pilot, roughly — pushed through the cases
 * that are hard to judge from the types alone: two nodes the graph says may run together
 * that would fight over the same files, an edge that changes nothing, a cycle, work that
 * has to agree on something and therefore is not two nodes, a constraint pointing at an
 * argument still open, and a brief rendered from the graph with nothing added by hand.
 *
 * State is printed after every step. Nothing is persisted.
 */

import { toBrief, toMermaid, validate, waves } from "./execution-dag.schema.prototype.ts";
import type { Edge, ExecutionDag, Finding, Node } from "./execution-dag.schema.prototype.ts";

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
const report = (findings: Finding[]) => {
  if (findings.length === 0) {
    line("   clean");
    return;
  }
  for (const f of findings) line(`   ${f.code}  ${f.message}`);
};

// ---------------------------------------------------------------------------
// The decisions this map has closed
// ---------------------------------------------------------------------------

const CLOSED = new Set([
  "GRE-135", // vertical TDD and mutation testing
  "GRE-140", // one Implementation Ticket is one delivery unit
  "GRE-144", // the user merges, and the merge unlocks successors
  "GRE-149", // control plane and ticket boundary
  "GRE-150", // Runtime Adapter contract
  "GRE-153", // Tracker Adapter contract
]);

// ---------------------------------------------------------------------------
// A graph
// ---------------------------------------------------------------------------

const node = (
  id: string,
  title: string,
  touches: string[],
  constraints: string[],
  acceptance: string[],
): Node => ({
  id,
  title,
  intent: `${title}. Written for the harness; the real intent is a paragraph.`,
  acceptance,
  touches,
  constraints,
});

const base = (): ExecutionDag => ({
  name: "symphonia brownfield pilot",
  nodes: [
    node(
      "tracker-adapter",
      "Implement the Linear Tracker Adapter",
      ["src/adapters/tracker"],
      ["GRE-153", "GRE-135"],
      ["Every operation in the contract is implemented", "A claim is verified by re-reading"],
    ),
    node(
      "runtime-adapter",
      "Implement the Orca Runtime Adapter",
      ["src/adapters/runtime"],
      ["GRE-150", "GRE-135"],
      ["A second writer in one workspace is refused", "Tier evidence is never invented"],
    ),
    node(
      "coordinator",
      "Implement the coordinator loop",
      ["src/core/coordinator"],
      ["GRE-149", "GRE-135"],
      ["A silent finisher is surfaced by the sweep", "Events are idempotent per attempt"],
    ),
    node(
      "brief-render",
      "Render Execution Briefs from the graph",
      ["src/core/brief"],
      ["GRE-140"],
      ["A brief is byte-identical across two runs of the same graph"],
    ),
    node(
      "pilot-slice",
      "Deliver one brownfield feature end to end",
      ["src/pilot"],
      ["GRE-135"],
      ["The feature ships through the whole pipeline without manual intervention"],
    ),
  ],
  edges: [
    { from: "tracker-adapter", to: "coordinator" },
    { from: "runtime-adapter", to: "coordinator" },
    { from: "coordinator", to: "pilot-slice" },
    { from: "brief-render", to: "pilot-slice" },
  ],
});

/** Structural edit helpers, so each step shows one change and nothing else. */
const withEdges = (dag: ExecutionDag, edges: Edge[]): ExecutionDag => ({
  ...dag,
  edges: [...dag.edges, ...edges],
});
const patchNode = (dag: ExecutionDag, id: string, patch: Partial<Node>): ExecutionDag => ({
  ...dag,
  nodes: dag.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)),
});

// ---------------------------------------------------------------------------
// Walkthrough
// ---------------------------------------------------------------------------

// --- 1 -----------------------------------------------------------------

step("1.", "A graph that passes");

const dag = base();
report(validate(dag, CLOSED));
line();
show("waves", waves(dag));
line();
note("Waves are what the topology allows, not a schedule. Three nodes sit in wave one");
note("because nothing orders them — how many actually run at once is core policy, and the");
note("schema has no field that could answer it.");

// --- 2 -----------------------------------------------------------------

step("2.", "The collision the graph cannot see");

const collide = patchNode(dag, "brief-render", { touches: ["src/core/coordinator/brief"] });
report(validate(collide, CLOSED));
line();
note("Nothing orders these two, so the graph says they may run at once — and they would be");
note("editing the same directory in two workspaces, with the second merge paying for it.");
note("No dependency exists to catch this; it is only visible because both declare a Write");
note("Scope. This is the rule that field exists for.");

step("2b.", "Ordering them, and what that costs");

const halfFixed = withEdges(collide, [{ from: "coordinator", to: "brief-render" }]);
report(validate(halfFixed, CLOSED));
line();
note("The collision is gone and an edge went redundant in its place: the coordinator now");
note("reaches the pilot slice through the brief. The validator will not let the fix leave");
note("that behind.");

step("2c.", "The fix, finished");

const ordered: ExecutionDag = {
  ...halfFixed,
  edges: halfFixed.edges.filter((e) => !(e.from === "coordinator" && e.to === "pilot-slice")),
};
report(validate(ordered, CLOSED));
line();
show("waves", waves(ordered));
line();
note("The edge is honest: one waits for the other's merge. A wave that held three now holds");
note("two, and the graph stopped claiming parallelism the workspaces could not deliver.");

// --- 3 -----------------------------------------------------------------

step("3.", "An edge that changes nothing");

const redundant = withEdges(dag, [{ from: "tracker-adapter", to: "pilot-slice" }]);
report(validate(redundant, CLOSED));
line();
note("The pilot slice already waits for the coordinator, which already waits for the tracker");
note("adapter. The extra edge orders nothing and costs every reader the time to work that");
note("out. Reported as an error rather than a style note — the graph is reviewed as a diff.");

// --- 4 -----------------------------------------------------------------

step("4.", "A cycle");

const cyclic = withEdges(dag, [{ from: "pilot-slice", to: "tracker-adapter" }]);
report(validate(cyclic, CLOSED));
line();
note("The path is printed, not just the fact. In a graph of thirty nodes 'there is a cycle'");
note("is an afternoon of work; the path is a one-line fix. Validation stops here — every");
note("later rule reads reachability, which means nothing while the graph is cyclic.");

// --- 5 -----------------------------------------------------------------

step("5.", "Work that has to agree on something");

show("node kinds", 1);
line();
note("There is no planning node, and nothing in this schema can say 'plan these two");
note("together'. When two pieces of work have to agree on something neither can settle");
note("alone, there are two shapes and both are already expressible:");
line();
note("  one node        the agreement is settled by building it, in one branch, reviewed");
note("                  once. Use this while it fits the Context Budget and the Review");
note("                  Budget.");
note("  a node others   the agreement is itself deliverable work, so it is an ordinary node");
note("  depend on       with a workspace, a branch, a pull request and acceptance criteria.");
line();
note("The dropped alternative was a shared plan written once for two implementers to keep");
note("separately, in two branches. That is a promise, not a fact: if one reads it");
note("differently nothing notices until the second merge, which is the failure it existed");
note("to prevent.");

// --- 6 -----------------------------------------------------------------

step("6.", "A constraint pointing at an argument still open");

const premature = patchNode(dag, "coordinator", {
  constraints: ["GRE-149", "GRE-135", "GRE-155"],
});
report(validate(premature, CLOSED).filter((f) => f.code === "DAG-009"));
line();
note("GRE-155 is still open, so nothing it says is settled and a node cannot be bound by it.");
note("This is what stops the DAG from quietly becoming the place decisions get made — the");
note("graph may only cite arguments the map has already finished.");

// --- 7 -----------------------------------------------------------------

step("7.", "A node that writes nothing and promises nothing");

const empty = patchNode(dag, "brief-render", { touches: [], acceptance: [] });
report(validate(empty, CLOSED).filter((f) => f.code === "DAG-008" || f.code === "DAG-010"));
line();
note("An empty Write Scope means no collision involving this node can ever be seen — the");
note("rule in step 2 goes quiet rather than failing, which is worse than failing. Empty");
note("acceptance means the reviewers have nothing to check against.");

// --- 8 -----------------------------------------------------------------

step("8.", "The brief, rendered from the graph and nothing else");

line();
line(
  toBrief(dag, "coordinator")
    .split("\n")
    .map((l) => `   ${l}`)
    .join("\n"),
);
line();
note("Nothing here was written by hand and nothing came from the tracker. If this ever needs");
note("a field the graph does not have, one of two things is true: the schema is short a");
note("field, or the brief is reaching for state that belongs to the ticket.");

// --- 9 -----------------------------------------------------------------

step("9.", "What cannot be written down here at all");

show("status field", "none");
show("assignee field", "none");
show("workspace/branch", "none");
show("ticket key", "none");
line();
note("There is no way to record that a node is in progress, who has it, or which ticket it");
note("became. That is not an omission to fill in later: the tracker answers all four, and a");
note("second copy here would be a second answer that drifts. The pointer runs one way —");
note("the ticket carries the node id, the node knows nothing about the ticket.");

// --- 10 ----------------------------------------------------------------

step("10.", "Where the plan stops");

const terminal = dag.nodes
  .filter((n) => !dag.edges.some((e) => e.from === n.id))
  .map((n) => n.id)
  .sort();
show("terminal nodes", terminal);
line();
note("The graph simply ends. There is no checkpoint node asking whether to continue — a");
note("graph is authored only as far as the next open question, and ending is how it says");
note("the rest is undecided. Re-planning past that point produces a new graph, not a longer");
note("one. What that costs is parked as GRE-165: a stop condition has no machine-readable");
note("home and lives in prose until that ticket says otherwise.");

// --- 11 ----------------------------------------------------------------

step("11.", "The picture, generated");

line();
line(
  toMermaid(dag)
    .split("\n")
    .map((l) => `   ${l}`)
    .join("\n"),
);
line();
note("Generated every time, because a hand-drawn diagram is out of date within a week and");
note("then the review is of a picture nothing runs on.");

line();
line("─".repeat(76));
line("done");
line("─".repeat(76));
