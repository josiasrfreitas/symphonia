"""Linear Tracker Adapter (GRE-174).

TLDR: ``LinearTracker`` implements the ``TrackerAdapter`` protocol against
Linear's GraphQL API; the ``"linear"`` key of ``.symphonia/config.json``
carries the SYM team identifiers and the phase→status map (including the
GRE-164 deviation: ``planning`` and ``plan-gate`` both map to ``Planning``).
"""
from .adapter import LinearTracker, PatchError
from .client import LinearClient, LinearError

__all__ = ["LinearTracker", "LinearClient", "LinearError", "PatchError"]
