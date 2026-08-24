"""Tests for `map` — the dispatcher, the stateless guided mode, and the
seam that hands a verb its tracker factory. The registry is injected as a
dict of stand-in modules; the real `verbs/` package is touched only by the
one test that asserts it is still empty. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_map
    python3 .symphonia/src/tests/test_map.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import map as MAP


class FakeTracker:
    """Stands in for `LinearTracker`, recording what a verb asked of it."""

    def __init__(self):
        self.calls: list[tuple] = []

    def list_children(self, id):
        self.calls.append(("list_children", id))
        return []


def verb_module(name, required=("map",), run=None):
    """A stand-in verb module: the two names `verbs/__init__.py` requires."""

    return SimpleNamespace(
        SPEC={
            "name": name,
            "help": f"the {name} verb",
            "required": required,
            "example": f"map {name} --map SYM-8",
        },
        run=run or (lambda params, *, tracker: f"{name} ran"),
    )


def call(argv, registry=None, tracker=None):
    """Run `main` and capture what a caller would see."""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = MAP.main(argv, registry=registry or {}, tracker=tracker)
    return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())


def _restore_module(name, module):
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


def fields(text):
    return dict(
        (line.split(":", 1)[0].strip().lower(), line.split(":", 1)[1].strip())
        for line in text.splitlines() if ":" in line
    )


# --- the three refusal paths ------------------------------------------------


class NoVerb(unittest.TestCase):
    def test_bare_call_refuses_as_incomplete_with_all_four_fields(self):
        result = call([], {"frontier": verb_module("frontier")})
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        got = fields(result.err)
        self.assertEqual(got["kind"], INJECTION.INCOMPLETE)
        self.assertIn("no verb", got["blocked"])
        self.assertIn("frontier", got["accepted"])
        self.assertTrue(got["example"])

    def test_it_refuses_even_with_no_verb_registered_at_all(self):
        result = call([])
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertEqual(fields(result.err)["kind"], INJECTION.INCOMPLETE)


class UnknownVerb(unittest.TestCase):
    def test_it_is_refused_not_incomplete(self):
        result = call(["frontiar"], {"frontier": verb_module("frontier")})
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        got = fields(result.err)
        self.assertEqual(got["kind"], INJECTION.REFUSED)
        self.assertIn("frontiar", got["blocked"])

    def test_a_near_miss_is_offered_in_the_example(self):
        result = call(["frontiar"], {"frontier": verb_module("frontier")})
        self.assertIn("frontier", fields(result.err)["example"])


class MissingParameter(unittest.TestCase):
    def test_it_names_what_is_missing_instead_of_asking(self):
        result = call(["ticket"], {"ticket": verb_module("ticket", required=("map", "question"))})
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        got = fields(result.err)
        self.assertEqual(got["kind"], INJECTION.INCOMPLETE)
        self.assertIn("map", got["blocked"])
        self.assertIn("question", got["blocked"])
        self.assertEqual(got["example"], "map ticket --map SYM-8")

    def test_a_partial_call_still_names_only_the_gap(self):
        registry = {"ticket": verb_module("ticket", required=("map", "question"))}
        result = call(["ticket", "--map", "SYM-8"], registry)
        got = fields(result.err)
        self.assertIn("question", got["blocked"])
        self.assertNotIn("map,", got["blocked"])

    def test_the_guided_mode_keeps_no_state_between_calls(self):
        # The same incomplete call twice gives byte-identical output: there
        # is no session that remembers the first attempt.
        registry = {"ticket": verb_module("ticket", required=("map",))}
        first, second = call(["ticket"], registry), call(["ticket"], registry)
        self.assertEqual(first.err, second.err)


class ValuelessParameter(unittest.TestCase):
    """`--map` with nothing after it is not a value. Guessing `True` where
    a key was expected is the one thing this tool must never do."""

    def test_a_bare_flag_where_a_value_belongs_is_refused(self):
        registry = {"ticket": verb_module("ticket", required=("map",))}
        result = call(["ticket", "--map"], registry)
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        got = fields(result.err)
        self.assertEqual(got["kind"], INJECTION.INCOMPLETE)
        self.assertIn("map", got["blocked"])

    def test_the_verb_never_runs_with_true_where_a_value_belongs(self):
        seen: list = []
        registry = {"ticket": verb_module(
            "ticket",
            required=("map",),
            run=lambda params, *, tracker: seen.append(params) or "ran",
        )}
        call(["ticket", "--map"], registry)
        self.assertEqual(seen, [])


class EmptyParameter(unittest.TestCase):
    def test_empty_is_not_reported_as_absent(self):
        registry = {"ticket": verb_module("ticket", required=("map",))}
        got = fields(call(["ticket", "--map", ""], registry).err)
        self.assertEqual(got["kind"], INJECTION.INCOMPLETE)
        self.assertIn("empty", got["blocked"])
        self.assertNotIn("without", got["blocked"])


class EveryExampleIsACallThisToolAccepts(unittest.TestCase):
    """`injection.Refusal.example` promises "one concrete call that would
    work" — so it comes from the verb's own SPEC, never from a bare
    `map <verb>` this same tool would turn around and refuse."""

    def test_the_no_verb_example_is_a_registered_verbs_own(self):
        registry = {"frontier": verb_module("frontier")}
        self.assertEqual(
            fields(call([], registry).err)["example"],
            registry["frontier"].SPEC["example"],
        )

    def test_the_near_miss_example_is_the_neighbours_own(self):
        registry = {"frontier": verb_module("frontier")}
        self.assertEqual(
            fields(call(["frontiar"], registry).err)["example"],
            registry["frontier"].SPEC["example"],
        )

    def test_an_unknown_verb_with_no_near_miss_still_shows_a_working_call(self):
        registry = {"frontier": verb_module("frontier")}
        self.assertEqual(
            fields(call(["zzzzzz"], registry).err)["example"],
            registry["frontier"].SPEC["example"],
        )


class NoTracebackEverReachesTheCaller(unittest.TestCase):
    def test_none_of_the_refusals_print_a_traceback(self):
        registry = {"frontier": verb_module("frontier")}
        for argv in ([], ["nope"], ["frontier"]):
            with self.subTest(argv=argv):
                result = call(argv, registry)
                self.assertNotIn("Traceback", result.err)
                self.assertNotIn("File \"", result.err)

    def test_a_bug_inside_a_verb_is_not_dressed_up_as_a_refusal(self):
        def explode(params, *, tracker):
            raise KeyError("a real bug")

        registry = {"frontier": verb_module("frontier", run=explode)}
        with self.assertRaises(KeyError):
            call(["frontier", "--map", "SYM-8"], registry)


# --- the JSON shape ---------------------------------------------------------


class JsonOutput(unittest.TestCase):
    def test_json_carries_the_same_four_fields(self):
        result = call(["--json"], {"frontier": verb_module("frontier")})
        payload = json.loads(result.err)
        self.assertEqual(sorted(payload), ["accepted", "blocked", "example", "kind"])
        self.assertEqual(payload["kind"], INJECTION.INCOMPLETE)


# --- the tracker seam -------------------------------------------------------


class TrackerInjection(unittest.TestCase):
    def test_a_verb_receives_the_factory_and_reaches_the_tracker(self):
        seen = {}

        def run(params, *, tracker):
            seen["tracker"] = tracker()
            seen["params"] = params
            return "ok"

        fake = FakeTracker()
        registry = {"frontier": verb_module("frontier", run=run)}
        result = call(["frontier", "--map", "SYM-8"], registry, tracker=lambda: fake)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.out.strip(), "ok")
        self.assertIs(seen["tracker"], fake)
        self.assertEqual(seen["params"], {"map": "SYM-8"})

    def test_the_factory_is_not_called_when_the_verb_does_not_ask(self):
        # No LINEAR_API_KEY in the environment, and the real factory: a
        # verb that never calls it must still succeed.
        for name in ("LINEAR_API_KEY", "SYMPHONIA_ENV"):
            if name in os.environ:
                self.addCleanup(os.environ.__setitem__, name, os.environ[name])
                del os.environ[name]
        registry = {"graph": verb_module("graph", run=lambda params, *, tracker: "drawn")}
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MAP.main(["graph", "--map", "SYM-8"], registry=registry)
        self.assertEqual((code, out.getvalue().strip()), (0, "drawn"))

    def test_the_real_factory_builds_at_most_one_tracker(self):
        built = []

        def build():
            built.append(FakeTracker())
            return built[-1]

        real = sys.modules.get("linear")
        self.addCleanup(_restore_module, "linear", real)
        sys.modules["linear"] = SimpleNamespace(LinearTracker=build)

        factory = MAP._tracker_factory()
        self.assertIs(factory(), factory())
        self.assertEqual(len(built), 1)


# --- parsing ----------------------------------------------------------------


class Parse(unittest.TestCase):
    def test_values_and_flags(self):
        self.assertEqual(
            MAP.parse(["ticket", "--map", "SYM-8", "--user-requested"]),
            ("ticket", {"map": "SYM-8", "user-requested": True}),
        )

    def test_a_stray_value_is_refused_not_swallowed(self):
        # Nothing is missing from `map ticket SYM-8` — the shape is wrong,
        # and supplying more of the same does not fix it. That is REFUSED.
        with self.assertRaises(INJECTION.Refused) as caught:
            MAP.parse(["ticket", "SYM-8"])
        self.assertEqual(caught.exception.refusal.kind, INJECTION.REFUSED)

    def test_no_verb_leaves_the_verb_empty(self):
        self.assertEqual(MAP.parse(["--map", "SYM-8"]), ("", {"map": "SYM-8"}))


# --- discovery --------------------------------------------------------------


class Discovery(unittest.TestCase):
    def test_the_verbs_package_is_still_empty(self):
        # Deliberately brittle: this assertion is what breaks the day the
        # first real verb lands (SYM-11), forcing whoever adds it to say so
        # here rather than letting the dispatcher grow silently.
        self.assertEqual(MAP.discover(), {})


if __name__ == "__main__":
    unittest.main()
