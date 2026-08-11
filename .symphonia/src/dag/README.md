# Execution DAG

**TLDR: this folder will hold the Execution DAG tooling — `dag validate`, `dag brief <node>`, `dag graph` — promoted to Python from the TypeScript prototype in `docs/contracts/execution-dag.*.prototype.ts`. Placeholder for now; the prototype remains the specification.**

The tooling itself is Source, which is why it lives under `src/`. The DAG it produces for a given run — the instantiated graph — is an Artifact: never committed, lives outside every checkout.

Planned commands:

- `dag validate` — cycle detection, Write Scope collision between unordered nodes, open constraints.
- `dag brief <node>` — render the Execution Brief for one node.
- `dag graph` — render the DAG as Mermaid, in waves.
