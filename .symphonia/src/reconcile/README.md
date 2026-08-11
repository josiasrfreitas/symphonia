# Reconciliation

**TLDR: every `/orchestrate` run starts here. Reconciliation compares what the tracker says about each Implementation Ticket with what the runtime says is running for it, and acts only on the difference. The tracker is the only state, which is what makes `/orchestrate` idempotent.**

The specification is `docs/contracts/progress-reconciliation.contract.prototype.ts`. Its promotion target is a pure `reconcile()` function in Python with idempotence tests.

Findings carry the structured codes defined in `.symphonia/adapters/attention.py` — the substring matching of the prototype is replaced by declared codes (reopens the GRE-153 note on structured Attention).

The only outward effects of a reconcile pass are raising or clearing Needs Attention flags and closing intents. Clearing a flag a human or another writer raised is never done here; clearing is human.
