# Hooks

**TLDR: the harness `Stop` hook (already installed by Orca in both CLIs) drives the Context Budget gate: from the configured stop it runs the context check, and at the gate it makes the session write a `/handoff` and stop.**

- The session opener records the window size in the Workspace (this covers Claude Code, which does not record the window in the transcript).
- The check itself has its own ticket; it builds on `docs/research/gre-166/context-left.py` and reads `context_gate_used_fraction`/`stop_check_from_stop` from `.symphonia/config.json`.
- On Claude Code the hook holds the agent and injects the instruction to write the handoff; on Codex the request comes from outside via Orca.
- At the gate: new Attempt, same ticket, same Workspace, plan intact, Delivery Phase unchanged.
