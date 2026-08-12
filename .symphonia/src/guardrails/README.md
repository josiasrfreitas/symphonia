# Guardrails

**TLDR: every guardrail here is a script reading a declared field — never model judgment. `review_budget.py` is implemented; the other two are skeletons with their own ticket for the real implementation. The numbers they read live in `.symphonia/config.json`.**

| Script | Reads | Enforces |
|---|---|---|
| `write_scope.py` | Declared Write Scope lists; `git diff --name-only` | No path intersection between parallel tickets before dispatch; audit of actual diff against declaration at the end. |
| `review_budget.py` | `review_budget_lines`; `git diff -M --numstat` against the base commit; exclusions via `git check-attr` (`linguist-generated`, `symphonia-budget-exempt`) | Changed lines ≤ 400 (GRE-156's rules exactly); overflow flips the verdict but is never auto-approved — a human approves a split or records a waiver via `.gitattributes`. |
| `context_gate.py` | `context_gate_used_fraction`, `stop_check_from_stop`; window size recorded in the Workspace | From the 3rd/4th Stop of a session, checks fraction of context used; at 40% the session hands off to a new Attempt. Always fraction + window resolution, never absolute tokens (GRE-166). |

The Stop hook wiring lives in `.symphonia/hooks/`.
