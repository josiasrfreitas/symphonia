# Symphonia Package

**TLDR: this directory is the whole workflow package. It is versioned inside the target repo — not a plugin — so it works with Claude Code and Codex alike. Content lives here in folders; the harness skills are thin pointers into these folders; `install.py` at the repo root installs the pointers idempotently.**

## Source × Resource × Artifact

Every folder here is 100% one of three things:

- **Source** — what is written, reviewed, and imported or executed. Lives under `src/`: the Python package (`adapters/`, `guardrails/`, `dag/`, `reconcile/`, `workflow/`) and the two scripts (`spawn.py`, `setup_worktree.py`) behind the thin `bin/` entrypoints. `bin/` itself is Source too — the two thin executable entrypoints, kept outside `src/` as the stable CLI surface (per ADR-0001), with their body living in `src/`.
- **Resource** — content Source reads but never executes: `roles/`, `skills/`, `config.json`, `DEPENDENCIES.md`. Siblings of `src/`, not inside it.
- **Artifact** — what a run produces: the spawn registry, handoff documents, transcripts, an instantiated DAG. Never committed, and lives outside every checkout (`~/.symphonia/runtime/`, `~/orca/.context`).

READMEs are the one exception: documentation of the folder they sit in, not content of any of the three categories.

`src/` lives inside `.symphonia/`, not at the repo root — see `docs/adr/0001-package-lives-in-the-dot-directory.md` for why, and what that costs: one production `sys.path` bootstrap, shared by the two `bin/` entrypoints; the test files repeat the same one-line insertion to import from that same root.

## Layout

| Path | What lives here |
|---|---|
| `config.json` | The calibration numbers guardrail scripts read, plus (under the `"linear"` key) the Linear Tracker Adapter's provider identifiers. Nothing else configures the workflow. |
| `DEPENDENCIES.md` | Third-party skills the workflow depends on (grilling, code-review, tdd). Declared, not bundled. |
| `roles/` | Role templates (Orchestrator, Planner, Implementer, Spec Reviewer, Standards Reviewer) as markdown; the four spawnable roles each declare `capability_tier` and `access` in their frontmatter — the source, not a mirror, read by `src/workflow/roles.py`. |
| `bin/` | Thin entrypoints (`spawn`, `setup-worktree`) — the CLI surface. Their bodies live in `src/`. |
| `src/` | All Source. See below. |
| `src/dag/` | Home of the Execution DAG tooling (`dag validate` / `dag brief` / `dag graph`). Placeholder for now. |
| `src/guardrails/` | Guardrail scripts: Write Scope collision/audit, Review Budget meter, Context Budget gate. Skeletons for now. |
| `src/reconcile/` | Reconciliation: how a run compares tracker vs runtime and acts only on the difference. |
| `src/adapters/` | The shared Python interfaces: Tracker Adapter contract, Runtime Adapter contract, structured Needs Attention codes, role I/O parsing (`reports.py`), and the plan gate state machine (`plan_gate.py`). |
| `src/workflow/` | The gate loop: turns typed gate events into gate actions and executes them, over `src/adapters/plan_gate.py`'s pure state machine. Dependency-injected, so it is driven by a test without loading the CLI. |
| `src/spawn.py` | The spawn interface's implementation — verbs, worktree policy, the plan gate wiring. `bin/spawn` just imports it. |
| `src/setup_worktree.py` | Copies the env files a fresh worktree never gets. `bin/setup-worktree` just imports it. |
| `hooks/` | Harness hooks (the `Stop` hook that drives the context gate). |
| `skills/` | Source content of the three shipped skills: `/orchestrate`, `/wayfinder`, `/handoff`. The installed skill files only point here. |

## Rules the package encodes

- **Agentic only where there is prediction or synthesis; every enforcement is a script reading a declared field; every final decision is human.**
- The tracker is the only state. Every `/orchestrate` run starts with Reconciliation.
- `CLAUDE.md` is always only a link to `AGENTS.md`.
- Skills never duplicate content; they point into `.symphonia/`.
- **A role's return path is a script, like its launch path.** No role types `orca orchestration` by hand: `spawn submit` and `spawn done` build, check and send what it reports. The live registry that makes this possible lives at `~/.symphonia/runtime/spawns.json` (override with `SYMPHONIA_RUNTIME`) — outside every checkout, because both the Orchestrator and the role read it from different worktrees.
- **Secrets come from the environment, and `.env` only fills the gaps.** `LINEAR_API_KEY` is read from `os.environ`; if it is absent, `src/adapters/env.py` loads the first `.env` it finds — `$SYMPHONIA_ENV`, then `~/.symphonia/.env`, then the repo's own. A variable already exported always wins. Prefer `~/.symphonia/.env`: a role runs in the ticket's worktree, a different checkout, where the repo's `.env` does not exist. Copy `.env.example` to start.
- **Payload × body:** if a script decides on a field, it travels as payload (or a fixed-position token, for messages with no payload); if a human or the next phase reads it, it is a body section. The body may repeat a payload value for a human to read, but it is never the source of truth for automation. Role templates and `src/adapters/reports.py` point here instead of restating it.

## Installing

From the repo root:

```
python3 install.py           # install / update
python3 install.py --check   # report differences, write nothing
```

Running it twice produces the same result.
