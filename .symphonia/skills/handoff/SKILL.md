---
name: handoff
description: Write the baton when a session hits the context gate. Use when told the context gate fired, or when handing work to a fresh session.
---

# /handoff

Read and follow — all content lives there, none here:

1. `.symphonia/hooks/README.md` — when and how the gate fires.
2. `.symphonia/config.json` — the context gate numbers (`context_gate_used_fraction`, `stop_check_from_stop`).

The invariant: new Attempt, same ticket, same Workspace, plan intact,
Delivery Phase unchanged.
