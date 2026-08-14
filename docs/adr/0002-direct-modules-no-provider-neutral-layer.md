# 2. Direct modules, no provider-neutral layer

Date: 2026-08-13

## Status

Accepted

## Context

The package carried two provider-neutral Protocol contracts (`RuntimeAdapter`,
`TrackerAdapter`), a harness contract (`HarnessAdapter`), an in-memory fake
Orca runtime, a scripted CLI double, and a conformance suite holding the fake
and the real adapter to the same contract. An architecture review (2026-08-11)
found the runtime seam dead in production: `spawn.py` reached the CLI through
its own calls, `bind_control` was off, and most Protocol methods — `drain`,
`ack`, `respond`, `kill`, `message_worker`, the single-writer guard — only ever
ran inside the conformance suite. LinearTracker implemented 22 methods;
production called 5. Maintaining the runtime simulator made every change slow
and fragile, and the second provider both contracts existed for was never
built.

## Decision

Delete the contracts, the fake, the scripted CLI, the conformance suite and
the characterization tests. Keep one direct module per real dependency —
`orca.py`, `linear.py`, `claude.py` — containing only the calls production
makes, with the measured provider facts kept as comments where the code
encodes them. Gate logic lives whole in `gate.py`. Tests cover pure logic
only; end-to-end verification is a real run against a Hello World ticket,
checked from the outside (own ticket).

Abstractions are discovered, not created: a second runtime, tracker or
harness earns a seam when it exists, extracted from two working
implementations rather than speculated from one.

## Consequences

- Adding a gate operation touches one file (`gate.py` for judgment/actions,
  `spawn.py` for payload); adding a provider call is a function in the
  provider's module.
- There is no offline simulation of Orca; a behavior change against the CLI
  is verified against the real CLI.
- The prototype contracts for phases not yet built (Execution DAG,
  Reconciliation) remain in `docs/contracts/` as specification; the deleted
  runtime/tracker contract prototypes survive only in git history.
