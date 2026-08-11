# Symphonia Package

**TLDR: this directory is the whole workflow package. It is versioned inside the target repo — not a plugin — so it works with Claude Code and Codex alike. Content lives here in folders; the harness skills are thin pointers into these folders; `install.py` at the repo root installs the pointers idempotently.**

## Layout

| Path | What lives here |
|---|---|
| `config.json` | The calibration numbers guardrail scripts read. Nothing else configures the workflow. |
| `DEPENDENCIES.md` | Third-party skills the workflow depends on (grilling, code-review, tdd). Declared, not bundled. |
| `roles/` | Role templates (Orchestrator, Planner, Implementer, Spec Reviewer, Standards Reviewer) as markdown with a declared `capability_tier` field. |
| `dag/` | Home of the Execution DAG tooling (`dag validate` / `dag brief` / `dag graph`). Placeholder for now. |
| `guardrails/` | Guardrail scripts: Write Scope collision/audit, Review Budget meter, Context Budget gate. Skeletons for now. |
| `reconcile/` | Reconciliation: how a run compares tracker vs runtime and acts only on the difference. |
| `adapters/` | The shared Python interfaces: Tracker Adapter contract, Runtime Adapter contract, structured Needs Attention codes, role I/O parsing (`reports.py`), and the plan gate state machine (`plan_gate.py`). |
| `hooks/` | Harness hooks (the `Stop` hook that drives the context gate). |
| `skills/` | Source content of the three shipped skills: `/orchestrate`, `/wayfinder`, `/handoff`. The installed skill files only point here. |

## Rules the package encodes

- **Agentic only where there is prediction or synthesis; every enforcement is a script reading a declared field; every final decision is human.**
- The tracker is the only state. Every `/orchestrate` run starts with Reconciliation.
- `CLAUDE.md` is always only a link to `AGENTS.md`.
- Skills never duplicate content; they point into `.symphonia/`.
- **Payload × body:** if a script decides on a field, it travels as payload (or a fixed-position token, for messages with no payload); if a human or the next phase reads it, it is a body section. The body may repeat a payload value for a human to read, but it is never the source of truth for automation. Role templates and `adapters/reports.py` point here instead of restating it.

## Installing

From the repo root:

```
python3 install.py           # install / update
python3 install.py --check   # report differences, write nothing
```

Running it twice produces the same result.
