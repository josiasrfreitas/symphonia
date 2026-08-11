"""Put the conformance suite inside the default test run.

`conformance.py` holds the harness's walkthrough plus `RealCliContract`,
the file that declares itself the authority on the CLI's response shapes.
None of it ran under the project's documented command:

    cd .symphonia && python3 -m unittest discover -s src -q

`discover` matches `test*.py`, and nothing imported the module from a file
that does — so its 14 tests sat outside every green number this project has
quoted, including the "174 green" that cleared GRE-184 PR-B. A fixture that
never executes cannot fail, which makes it decoration rather than a guard.

Importing the `TestCase` subclasses here is enough: discovery finds this
file, and the names it binds are collected like any other. The explicit
invocation the module docstring documents keeps working unchanged.
"""

from ..conformance import (  # noqa: F401  (imported for discovery, not use)
    FakeConformance,
    GateEventReading,
    OrcaConformance,
    RealCliContract,
)
