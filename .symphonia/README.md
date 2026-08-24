# Symphonia Package

**TLDR: this directory is the whole workflow package. It is versioned inside the target repo — not a plugin — so it works with Claude Code and Codex alike. Content lives here in folders; the harness skills are thin pointers into these folders; `install.py` at the repo root installs the pointers idempotently.**

## Source × Resource × Artifact

Every folder here is 100% one of three things:

- **Source** — what is written, reviewed, and imported or executed. Lives under `src/`: a flat set of modules (`spawn.py`, `orca.py`, `claude.py`, `linear.py`, `gate.py`, `map.py`, `injection.py`, `roles.py`, `journal.py`, `env.py`, `setup_worktree.py`) plus `verbs/` and `tests/`. `bin/` itself is Source too — the three thin executable entrypoints, kept outside `src/` as the stable CLI surface (per ADR-0001), with their body living in `src/`.
- **Resource** — content Source reads but never executes: `roles/`, `skills/`, `config.json`, `DEPENDENCIES.md`. Siblings of `src/`, not inside it.
- **Artifact** — what a run produces: the spawn registry, handoff documents, transcripts, an instantiated DAG. Never committed, and lives outside every checkout (`~/.symphonia/runtime/`, `~/orca/.context`).

READMEs are the one exception: documentation of the folder they sit in, not content of any of the three categories.

`src/` lives inside `.symphonia/`, not at the repo root — see `docs/adr/0001-package-lives-in-the-dot-directory.md` for why, and what that costs: one production `sys.path` bootstrap, repeated by each `bin/` entrypoint; the test files repeat the same one-line insertion to import from that same root.

## Layout

| Path | What lives here |
|---|---|
| `config.json` | The calibration numbers, the Linear label names, and `handoff_dir` for where a role's baton document is written. |
| `DEPENDENCIES.md` | Third-party skills the workflow depends on (grilling, code-review, tdd). Declared, not bundled. |
| `roles/` | Role templates (Orchestrator, Planner, Implementer, Spec Reviewer, Standards Reviewer) as markdown; the four spawnable roles each declare `capability_tier` and `access` in their frontmatter — the source, not a mirror, read by `src/roles.py`. |
| `bin/` | Thin entrypoints (`spawn`, `setup-worktree`, `map`) — the CLI surface. Their bodies live in `src/`. |
| `src/spawn.py` | The spawn interface — the verbs, worktree policy, the Execution Brief, the registry. `bin/spawn` just imports it. |
| `src/gate.py` | The whole plan gate: report formats and parsers, the pure state machine, and the loop that executes gate actions. Dependency-injected, so a test drives it with a plain dict. |
| `src/orca.py` | Every `orca` CLI call this package makes, and the parsing of what the orchestration mailbox answers. |
| `src/claude.py` | The one place that writes an agent command line: tier → model/effort, permission flags, structural read-only, transcript-based tier evidence. |
| `src/map.py` | The `map` dispatcher: verb discovery over `verbs/`, argument parsing, and the stateless guided mode. `bin/map` just imports it. |
| `src/verbs/` | One module per `map` verb, discovered by convention. The contract a verb module must expose is in its `__init__.py`. Empty for now. |
| `src/injection.py` | The one Context Injection refusal format: what blocked it, what would be accepted, an example, and the kind. |
| `src/linear.py` | Every Linear call: the GraphQL client and the twelve tracker operations the workflow performs. |
| `src/roles.py` | The role vocabulary and the frontmatter loader for each role's declared tier/access. |
| `src/journal.py` | The event journal and delivery receipt `spawn wait` persists. |
| `src/env.py` | The `.env` loader — the shell always wins over a file. |
| `src/setup_worktree.py` | Copies the env files a fresh worktree never gets. `bin/setup-worktree` just imports it. |
| `src/tests/` | Deterministic tests of the pure logic above. Nothing here simulates Orca; end-to-end verification runs the real workflow against a Hello World ticket (own ticket). |
| `hooks/` | Harness hooks (the `Stop` hook that drives the context gate). |
| `skills/` | Source content of the three shipped skills: `/orchestrate`, `/wayfinder`, `/handoff`. The installed skill files only point here. |

## Rules the package encodes

- **Agentic only where there is prediction or synthesis; every enforcement is a script reading a declared field; every final decision is human.**
- The tracker is the only state. Every `/orchestrate` run starts with Reconciliation.
- `CLAUDE.md` is always only a link to `AGENTS.md`.
- Skills never duplicate content; they point into `.symphonia/`.
- **A role's return path is a script, like its launch path.** No role types `orca orchestration` by hand: `spawn submit` and `spawn done` build, check and send what it reports. The live registry that makes this possible lives at `~/.symphonia/runtime/spawns.json` (override with `SYMPHONIA_RUNTIME`) — outside every checkout, because both the Orchestrator and the role read it from different worktrees.
- **Secrets come from the environment, and `.env` only fills the gaps.** `LINEAR_API_KEY` is read from `os.environ`; if it is absent, `src/env.py` loads the first `.env` it finds — `$SYMPHONIA_ENV`, then `~/.symphonia/.env`, then the repo's own. A variable already exported always wins. Prefer `~/.symphonia/.env`: a role runs in the ticket's worktree, a different checkout, where the repo's `.env` does not exist. Copy `.env.example` to start.
- **`map` refuses, it never prompts.** `bin/map` is the Wayfinder tool's entrypoint; it has no verbs yet, only the dispatcher that will register them (one module per verb, by convention). Every call it cannot run — no verb, unknown verb, a required parameter missing — comes back in the single Context Injection refusal format from `src/injection.py`: what blocked it, what would be accepted, one example, and whether the call was *incomplete* or *refused*. Never a traceback, and never an interactive question: the direct mode and the guided mode run the same validation, and the guided mode keeps no state between calls.
- **Payload × body:** if a script decides on a field, it travels as payload (or a fixed-position token, for messages with no payload); if a human or the next phase reads it, it is a body section. The body may repeat a payload value for a human to read, but it is never the source of truth for automation. Role templates and `src/gate.py` point here instead of restating it.

## Installing

From the repo root:

```
python3 install.py           # install / update
python3 install.py --check   # report differences, write nothing
```

Running it twice produces the same result.
