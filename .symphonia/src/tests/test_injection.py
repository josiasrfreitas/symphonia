"""Tests for `injection` — the single Context Injection refusal format.
Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_injection
    python3 .symphonia/src/tests/test_injection.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION


def refusal(**over):
    fields = {
        "blocked": "no verb was given",
        "accepted": "one of: frontier, ticket",
        "example": "map frontier --map SYM-8",
        "kind": INJECTION.INCOMPLETE,
    }
    fields.update(over)
    return INJECTION.Refusal(**fields)


class TheFourFields(unittest.TestCase):
    def test_render_shows_all_four(self):
        text = INJECTION.render(refusal())
        self.assertIn("no verb was given", text)
        self.assertIn("one of: frontier, ticket", text)
        self.assertIn("map frontier --map SYM-8", text)
        self.assertIn(INJECTION.INCOMPLETE, text)

    def test_render_keeps_the_designed_order(self):
        text = INJECTION.render(refusal())
        labels = [line.split(":", 1)[0] for line in text.splitlines()]
        self.assertEqual(labels, ["Blocked", "Accepted", "Example", "Kind"])

    def test_as_dict_has_the_four_keys(self):
        self.assertEqual(
            sorted(INJECTION.as_dict(refusal())),
            ["accepted", "blocked", "example", "kind"],
        )


class EmptinessIsAProgrammingError(unittest.TestCase):
    def test_an_empty_field_raises(self):
        for name in ("blocked", "accepted", "example"):
            with self.subTest(field=name):
                with self.assertRaises(ValueError) as caught:
                    refusal(**{name: "   "})
                self.assertIn(name, str(caught.exception))

    def test_an_unknown_kind_raises(self):
        with self.assertRaises(ValueError) as caught:
            refusal(kind="maybe")
        self.assertIn("maybe", str(caught.exception))

    def test_both_kinds_are_accepted(self):
        for kind in (INJECTION.INCOMPLETE, INJECTION.REFUSED):
            with self.subTest(kind=kind):
                self.assertEqual(refusal(kind=kind).kind, kind)


class TheExceptionCarriesTheRefusal(unittest.TestCase):
    def test_refused_keeps_the_object_and_reads_as_the_blocker(self):
        item = refusal()
        error = INJECTION.Refused(item)
        self.assertIs(error.refusal, item)
        self.assertEqual(str(error), item.blocked)


if __name__ == "__main__":
    unittest.main()
