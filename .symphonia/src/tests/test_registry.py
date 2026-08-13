"""Tests for `registry` — the Spawn Registry module.

TLDR: the three properties the module exists for — paths resolve per call
(no reload after setting SYMPHONIA_RUNTIME), a transaction that raises
writes nothing, and the state file is never world-readable. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_registry
    python3 .symphonia/src/tests/test_registry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as REGISTRY


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)


class TestPathsResolvePerCall(RegistryCase):
    def test_the_env_var_takes_effect_without_a_reload(self):
        # The module was imported long before setUp exported the variable;
        # a path computed at import time would ignore it.
        self.assertEqual(REGISTRY.runtime_dir(), Path(self.tmp.name))
        with REGISTRY.transaction() as data:
            data["GRE-1/implementer"] = {"ticket": "GRE-1"}
        self.assertTrue((Path(self.tmp.name) / "spawns.json").exists())


class TestTransaction(RegistryCase):
    def test_a_clean_exit_persists_the_mutation(self):
        with REGISTRY.transaction() as data:
            data["GRE-1/planner"] = {"ticket": "GRE-1"}
        self.assertEqual(REGISTRY.read()["GRE-1/planner"], {"ticket": "GRE-1"})

    def test_a_raising_block_writes_nothing(self):
        with REGISTRY.transaction() as data:
            data["GRE-1/planner"] = {"ticket": "GRE-1"}
        with self.assertRaises(RuntimeError):
            with REGISTRY.transaction() as data:
                data["GRE-1/planner"]["retired"] = True
                raise RuntimeError("half-applied batch")
        self.assertNotIn(
            "retired", REGISTRY.read()["GRE-1/planner"],
            "a failed transaction must be replayed, never half-recorded",
        )

    def test_commit_persists_mid_transaction(self):
        with REGISTRY.transaction() as data:
            data["GRE-1/planner"] = {"gate_state": "verdict-approved"}
            REGISTRY.commit(data)
            # What another process would see between the commit and the
            # external effect that follows it.
            self.assertEqual(
                REGISTRY.read()["GRE-1/planner"]["gate_state"], "verdict-approved"
            )

    def test_the_state_file_is_not_world_readable(self):
        with REGISTRY.transaction() as data:
            data["GRE-1/planner"] = {"capability": "dcap-secret"}
        state = Path(self.tmp.name) / "spawns.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)


class TestKey(RegistryCase):
    def test_the_ticket_is_uppercased(self):
        self.assertEqual(REGISTRY.key("gre-1", "planner"), "GRE-1/planner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
