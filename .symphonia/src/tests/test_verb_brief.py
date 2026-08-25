"""Tests for the `brief` verb — the gate between the two halves of the
flow. Offline: the tracker arrives as a factory and is a fake, so nothing
here needs `LINEAR_API_KEY` and nothing reaches the network. Run either
way:

    cd .symphonia/src && python3 -m unittest tests.test_verb_brief
    python3 .symphonia/src/tests/test_verb_brief.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import intake as INTAKE
import map as MAP
from verbs import brief as BRIEF

BRIEFING = f"""# Portaria do intake

Prosa do agente, que continua sendo prosa.

{INTAKE.DOOR_HEADING}

{INTAKE.DOOR_FIELD} meio
{INTAKE.FACT_FIELD} o repo já tem o módulo, falta o verbo

{INTAKE.CRITERIA_HEADING}

- {INTAKE.CRITERION_PREFIX}o card de construção existe
"""

CLOSED_MAP = f"""## Destination

fechar a portaria

{INTAKE.FOG_HEADING}

<!-- see "Fog of war" -->

## Out of scope

- reescrever o tracker
"""


class FakeTracker:
    """Stands in for `LinearTracker`, recording what the verb asked of it.
    Same shape as the one in `test_map.py`, with the three operations this
    verb uses and nothing else."""

    def __init__(self, children=(), body=CLOSED_MAP):
        self.children = list(children)
        self.body = body
        self.calls: list[tuple] = []

    def list_children(self, id):
        self.calls.append(("list_children", id))
        return self.children

    def get_item(self, id):
        self.calls.append(("get_item", id))
        return SimpleNamespace(
            ref=SimpleNamespace(id=id, key=id, url=f"https://linear.app/{id}"),
            title="Decision Map",
            body=self.body,
        )

    def create_item(self, title, body, *, parent=None, labels=(), team=None):
        self.calls.append(("create_item", title, body, parent))
        return SimpleNamespace(id="uuid", key="SYM-42", url="https://linear.app/x/SYM-42")


def child(key, state_type):
    return SimpleNamespace(key=key, state_type=state_type)


def run(params, tracker):
    return BRIEF.run(params, tracker=lambda: tracker)


class BriefingOnDisk(unittest.TestCase):
    """The verb reads its briefing off disk, so every test here needs a
    file. One temporary directory per test, removed with the test — the
    rest of this package writes temporaries the same way."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.written = 0

    def write(self, text):
        self.written += 1
        path = self.directory / f"briefing-{self.written}.md"
        path.write_text(text, encoding="utf-8")
        return str(path)


class ValidBriefing(BriefingOnDisk):
    def test_the_card_is_created_with_the_briefing_as_its_body(self):
        tracker = FakeTracker()
        out = run({"file": self.write(BRIEFING), "parent": "SYM-9"}, tracker)
        self.assertEqual(tracker.calls, [("create_item", "Portaria do intake", BRIEFING, "SYM-9")])
        self.assertIn("SYM-42", out)
        self.assertIn("https://linear.app/x/SYM-42", out)
        self.assertIn("Porta: meio", out)
        self.assertIn("Fato: o repo já tem o módulo", out)


class RefusedBriefing(BriefingOnDisk):
    def _refuse(self, text):
        tracker = FakeTracker()
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": self.write(text), "parent": "SYM-9"}, tracker)
        # The tracker is never reached: a refused briefing costs no
        # network call and needs no API key.
        self.assertEqual(tracker.calls, [])
        return caught.exception.refusal

    def test_without_the_door_record_nothing_is_created(self):
        refusal = self._refuse(BRIEFING.split(INTAKE.DOOR_HEADING)[0]
                               + INTAKE.CRITERIA_HEADING
                               + f"\n\n- {INTAKE.CRITERION_PREFIX}algo acontece\n")
        self.assertIn(INTAKE.DOOR_HEADING, refusal.blocked)
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)

    def test_without_a_checkable_criterion_nothing_is_created(self):
        text = BRIEFING.replace(f"- {INTAKE.CRITERION_PREFIX}o card de construção existe",
                                "- fica bom")
        self.assertIn(INTAKE.CRITERIA_HEADING, self._refuse(text).blocked)

    def test_a_missing_file_is_a_refusal_not_a_traceback(self):
        tracker = FakeTracker()
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": "/nao/existe/briefing.md", "parent": "SYM-9"}, tracker)
        self.assertEqual(caught.exception.refusal.kind, INJECTION.REFUSED)
        self.assertEqual(tracker.calls, [])

    def test_an_empty_file_is_a_refusal(self):
        self.assertEqual(self._refuse("\n   \n").kind, INJECTION.INCOMPLETE)


class TheOptionalMapCheck(BriefingOnDisk):
    def test_an_open_child_blocks_and_the_refusal_names_it(self):
        tracker = FakeTracker(children=[child("SYM-40", "started"), child("SYM-41", "completed")])
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": "SYM-8"}, tracker)
        refusal = caught.exception.refusal
        self.assertEqual(refusal.kind, INJECTION.REFUSED)
        self.assertIn("SYM-40", refusal.blocked)
        self.assertIn("frontier", refusal.blocked)
        self.assertNotIn("create_item", [c[0] for c in tracker.calls])

    def test_a_cancelled_child_is_closed_not_frontier(self):
        # A ticket closed for being out of scope left the map by a
        # decision, not by being unfinished.
        tracker = FakeTracker(children=[child("SYM-40", "canceled")])
        run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": "SYM-8"}, tracker)
        self.assertIn("create_item", [c[0] for c in tracker.calls])

    def test_fog_left_in_the_map_body_blocks(self):
        body = CLOSED_MAP.replace(f"{INTAKE.FOG_HEADING}\n",
                                  f"{INTAKE.FOG_HEADING}\n\n- como paginar\n")
        tracker = FakeTracker(body=body)
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": "SYM-8"}, tracker)
        refusal = caught.exception.refusal
        self.assertIn("como paginar", refusal.blocked)
        self.assertNotIn("create_item", [c[0] for c in tracker.calls])

    def test_fog_written_as_prose_blocks_too(self):
        # The form the wayfinder skill recommends: no bullets, just
        # prose. A bullets-only fog parser opened the card over this.
        body = CLOSED_MAP.replace(
            f"{INTAKE.FOG_HEADING}\n",
            f"{INTAKE.FOG_HEADING}\n\nAinda não sabemos como o gate conversa com o mapa.\n",
        )
        tracker = FakeTracker(body=body)
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": "SYM-8"}, tracker)
        self.assertIn("gate conversa", caught.exception.refusal.blocked)
        self.assertNotIn("create_item", [c[0] for c in tracker.calls])

    def test_both_empty_creates_the_card(self):
        tracker = FakeTracker()
        run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": "SYM-8"}, tracker)
        self.assertEqual(
            [c[0] for c in tracker.calls],
            ["list_children", "get_item", "create_item"],
        )

    def test_without_map_the_check_does_not_run_at_all(self):
        # An open map is no obstacle to a briefing that never claimed to
        # come from one.
        tracker = FakeTracker(children=[child("SYM-40", "started")], body=CLOSED_MAP)
        run({"file": self.write(BRIEFING), "parent": "SYM-9"}, tracker)
        self.assertEqual([c[0] for c in tracker.calls], ["create_item"])

    def test_map_with_no_value_is_refused_rather_than_ignored(self):
        tracker = FakeTracker(children=[child("SYM-40", "started")])
        with self.assertRaises(INJECTION.Refused) as caught:
            run({"file": self.write(BRIEFING), "parent": "SYM-9", "map": True}, tracker)
        self.assertEqual(caught.exception.refusal.kind, INJECTION.REFUSED)


class ThroughTheDispatcher(BriefingOnDisk):
    """The V1 guided mode, exercised by a real verb for the first time."""

    def call(self, argv, tracker=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MAP.main(argv, registry={"brief": BRIEF}, tracker=tracker)
        return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())

    def test_a_bare_call_names_both_required_parameters(self):
        result = self.call(["brief"])
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertIn("--file", result.err)
        self.assertIn("--parent", result.err)
        self.assertIn("Kind: incomplete", result.err)

    def test_a_good_call_prints_the_injection_and_exits_zero(self):
        tracker = FakeTracker()
        result = self.call(
            ["brief", "--file", self.write(BRIEFING), "--parent", "SYM-9"],
            tracker=lambda: tracker,
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("SYM-42", result.out)

    def test_a_refusal_from_inside_the_verb_reaches_the_one_format(self):
        result = self.call(["brief", "--file", self.write("# só título\n"), "--parent", "SYM-9"],
                           tracker=lambda: FakeTracker())
        self.assertEqual(result.code, MAP.REFUSED_EXIT)
        self.assertEqual(
            [line.split(":", 1)[0] for line in result.err.strip().splitlines()],
            ["Blocked", "Accepted", "Example", "Kind"],
        )

    def test_the_verb_is_discovered_by_convention(self):
        self.assertIn("brief", MAP.discover())


if __name__ == "__main__":
    unittest.main()
