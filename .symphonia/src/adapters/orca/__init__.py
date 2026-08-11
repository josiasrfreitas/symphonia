"""Orca Runtime Adapter package (GRE-175).

TLDR: the real implementation of the ``RuntimeAdapter`` contract on top of
the Orca CLI, plus everything needed to trust it:

- ``adapter`` — ``OrcaRuntimeAdapter``: every contract call is a small,
  deterministic ``orca`` CLI invocation. The Capability Tier translation
  table lives here, never in role templates. Also home of
  ``message_worker``, the only sanctioned way to message a worker.
- ``events`` — typed gate events (plan question, approval reply,
  worker_done) parsed from ``orca orchestration check --peek --json`` by a
  pure function. No model interpretation anywhere.
- ``fake`` — the prototype harness fake promoted to a reusable test double:
  an in-memory hostile runtime, a ``FakeRuntimeAdapter`` over it, and a
  scripted ``orca`` CLI so the real adapter runs without a live Orca.
- ``conformance`` — the harness's 11-step walkthrough as an assert-based
  unittest suite. The double and the real adapter pass the same suite:
  ``cd .symphonia/src && python3 -m unittest adapters.orca.conformance -v``

Stdlib only. Deterministic: no wall clock, no randomness.
"""
