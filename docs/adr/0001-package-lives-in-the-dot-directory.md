# The whole package — src/ included — lives inside .symphonia/, not at the repo root

The workflow package is designed to be imported into a target repo by copying one directory: `.symphonia/` travels as a unit into codebases that already have their own root layout (their own `src/`, their own tooling), so nothing of ours may claim a root-level name. When the Source × Resource × Artifact split was made structural (GRE-182), the conventional choice — a root-level `src/` — was rejected for exactly that reason: it breaks the "one copyable folder" property and collides with the host repo's own layout. So Source lives in `.symphonia/src/`, Resources (`roles/`, `skills/`, `config.json`) live beside it, and Artifacts live outside every checkout (`~/.symphonia/`, `~/orca/.context`).

## Consequences

- Python cannot `import` through a dot-directory name, so the package needs one explicit bootstrap (`sys.path` set once by the entrypoints) — this is the deliberate price of portability, not an accident to be "fixed" by moving code to the root.
- Any tool that assumes root-level `src/` conventions (editors, linters, packaging defaults) must be pointed at `.symphonia/src/` explicitly.
