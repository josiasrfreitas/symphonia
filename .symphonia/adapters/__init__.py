"""Shared Python interfaces of the Symphonia workflow.

TLDR: three modules, translated from the prototypes in ``docs/contracts/``:

- ``tracker_adapter`` — the Tracker Adapter contract (canonical tracker
  artifacts and mutable delivery state).
- ``runtime_adapter`` — the Runtime Adapter contract (isolated execution
  contexts, provider-neutral).
- ``attention`` — the structured Needs Attention codes.

The ``.prototype.ts`` files remain the specification; these interfaces are
their executable Python promotion. Stdlib only.
"""
