"""Concrete `HarnessAdapter` implementations (GRE-186 S2).

TLDR: one module per agent CLI this package knows how to launch —
``claude`` today. Nothing outside this package writes an agent command
line by hand; the contract each module implements is
``adapters.harness_adapter.HarnessAdapter``.
"""
