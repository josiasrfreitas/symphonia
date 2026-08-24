"""Tests for `intake` — the pure half of the gate: what a briefing is,
what an intake handoff is, and the fog parser both this vertical and
SYM-11 read the end of a map with. No tracker, no network, no filesystem
outside `tempfile`. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_intake
    python3 .symphonia/src/tests/test_intake.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import injection as INJECTION
import intake as INTAKE

DOOR = f"""{INTAKE.DOOR_HEADING}

{INTAKE.DOOR_FIELD} meio
{INTAKE.FACT_FIELD} o módulo já existe, falta o verbo
"""

CRITERIA = f"""{INTAKE.CRITERIA_HEADING}

- {INTAKE.CRITERION_PREFIX}um briefing sem registro da porta é recusado
"""

BRIEFING = f"# Portaria do intake\n\nProsa do agente.\n\n{DOOR}\n{CRITERIA}"


def briefing(*, title=True, door=True, criteria=True):
    parts = []
    if title:
        parts.append("# Portaria do intake\n")
    parts.append("Prosa do agente.\n")
    if door:
        parts.append(DOOR)
    if criteria:
        parts.append(CRITERIA)
    return "\n".join(parts)


def refusal_of(call, *args, **kwargs):
    with unittest.TestCase().assertRaises(INJECTION.Refused) as caught:
        call(*args, **kwargs)
    return caught.exception.refusal


# --- the door record --------------------------------------------------------


class DoorRecord(unittest.TestCase):
    def test_both_fields_come_back(self):
        self.assertEqual(
            INTAKE.door_record(BRIEFING),
            ("meio", "o módulo já existe, falta o verbo"),
        )

    def test_a_missing_section_is_incomplete_and_names_the_section(self):
        refusal = refusal_of(INTAKE.door_record, "# Só o título\n\nprosa\n")
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)
        self.assertIn(INTAKE.DOOR_HEADING, refusal.blocked)

    def test_one_field_alone_is_refused_by_the_name_of_the_other(self):
        text = f"{INTAKE.DOOR_HEADING}\n\n{INTAKE.DOOR_FIELD} grande\n"
        refusal = refusal_of(INTAKE.door_record, text)
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)
        self.assertIn(INTAKE.FACT_FIELD, refusal.blocked)
        self.assertNotIn(INTAKE.DOOR_FIELD, refusal.blocked)

    def test_an_unknown_door_is_refused_not_incomplete(self):
        text = DOOR.replace("meio", "média")
        refusal = refusal_of(INTAKE.door_record, text)
        # Nothing is missing: a fourth door is a typo, and supplying more
        # of the same does not fix it.
        self.assertEqual(refusal.kind, INJECTION.REFUSED)
        for door in INTAKE.DOORS:
            self.assertIn(door, refusal.accepted)

    def test_the_section_stops_at_the_next_heading(self):
        text = f"{INTAKE.DOOR_HEADING}\n\n{INTAKE.DOOR_FIELD} meio\n\n## Outra\n\n{INTAKE.FACT_FIELD} tarde demais\n"
        self.assertEqual(refusal_of(INTAKE.door_record, text).kind, INJECTION.INCOMPLETE)


# --- checkable criteria -----------------------------------------------------


class CheckableCriteria(unittest.TestCase):
    def test_an_absent_section_has_none(self):
        self.assertEqual(INTAKE.checkable_criteria(briefing(criteria=False)), [])

    def test_prose_that_is_not_the_fixed_shape_does_not_count(self):
        text = f"{INTAKE.CRITERIA_HEADING}\n\n- funciona bem e o usuário fica feliz\n"
        self.assertEqual(INTAKE.checkable_criteria(text), [])

    def test_the_prefix_with_nothing_after_it_does_not_count(self):
        text = f"{INTAKE.CRITERIA_HEADING}\n\n- {INTAKE.CRITERION_PREFIX}\n"
        self.assertEqual(INTAKE.checkable_criteria(text), [])

    def test_one_of_each_keeps_only_the_checkable_one(self):
        text = (f"{INTAKE.CRITERIA_HEADING}\n\n- fica bonito\n"
                f"- {INTAKE.CRITERION_PREFIX}o card é criado\n")
        self.assertEqual(INTAKE.checkable_criteria(text),
                         [f"{INTAKE.CRITERION_PREFIX}o card é criado"])


# --- the briefing as a whole ------------------------------------------------


class ValidateBriefing(unittest.TestCase):
    def test_a_complete_briefing_returns_the_title_and_the_door(self):
        self.assertEqual(
            INTAKE.validate_briefing(BRIEFING),
            ("Portaria do intake", "meio", "o módulo já existe, falta o verbo"),
        )

    def test_without_a_title_there_is_nothing_to_name_the_card(self):
        refusal = refusal_of(INTAKE.validate_briefing, briefing(title=False))
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)
        self.assertIn("title", refusal.blocked)

    def test_without_the_door_record_it_is_refused(self):
        refusal = refusal_of(INTAKE.validate_briefing, briefing(door=False))
        self.assertIn(INTAKE.DOOR_HEADING, refusal.blocked)

    def test_without_a_checkable_criterion_it_is_refused(self):
        refusal = refusal_of(INTAKE.validate_briefing, briefing(criteria=False))
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)
        self.assertIn(INTAKE.CRITERIA_HEADING, refusal.blocked)


# --- the fog of a map body --------------------------------------------------


class FogItems(unittest.TestCase):
    EMPTY = (f"## Destination\n\nchegar lá\n\n{INTAKE.FOG_HEADING}\n\n"
             "<!-- see \"Fog of war\" -->\n\n## Out of scope\n\n- nada disso\n")

    def test_the_skills_own_empty_section_reads_as_empty(self):
        # The wayfinder template leaves an HTML comment in the section; a
        # parser that counted it would call every fresh map unfinished.
        # The bullet under `## Out of scope` is not fog either.
        self.assertEqual(INTAKE.fog_items(self.EMPTY), [])

    def test_bullets_under_the_heading_are_the_items(self):
        body = self.EMPTY.replace(
            f"{INTAKE.FOG_HEADING}\n\n",
            f"{INTAKE.FOG_HEADING}\n\n- como paginar\n* como versionar\n\n",
        )
        self.assertEqual(INTAKE.fog_items(body), ["como paginar", "como versionar"])

    def test_the_heading_is_the_literal_the_wayfinder_skill_writes(self):
        # SYM-11 declares the twin as `verbs/_shared.FOG`; the two strings
        # must be the same one, in English, exactly as the skill writes it.
        self.assertEqual(INTAKE.FOG_HEADING, "## Not yet specified")


# --- the handoff ------------------------------------------------------------


HANDOFF = f"{INTAKE.HANDOFF_HEADER.format(ticket='SYM-8')}\n\nProsa do agente.\n\n{DOOR}"


class Handoff(unittest.TestCase):
    def test_a_framed_handoff_validates(self):
        self.assertEqual(INTAKE.validate_handoff(HANDOFF, "SYM-8"),
                         ("meio", "o módulo já existe, falta o verbo"))

    def test_the_wrong_header_is_refused(self):
        refusal = refusal_of(INTAKE.validate_handoff, HANDOFF.replace("# Handoff", "# Notas"), "SYM-8")
        self.assertEqual(refusal.kind, INJECTION.INCOMPLETE)
        self.assertIn("SYM-8", refusal.example)

    def test_the_header_carries_this_tickets_key(self):
        refusal_of(INTAKE.validate_handoff, HANDOFF, "SYM-9")

    def test_the_header_alone_is_not_enough(self):
        refusal = refusal_of(
            INTAKE.validate_handoff,
            INTAKE.HANDOFF_HEADER.format(ticket="SYM-8") + "\n\nprosa\n",
            "SYM-8",
        )
        self.assertIn(INTAKE.DOOR_HEADING, refusal.blocked)


class WriteHandoff(unittest.TestCase):
    def test_a_valid_handoff_lands_beside_the_role_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = INTAKE.write_handoff("SYM-8", HANDOFF, handoff_dir=tmp)
            self.assertEqual(path.name, "sym-8-intake.md")
            self.assertEqual(path.read_text(encoding="utf-8"), HANDOFF)
            # The role handoff `spawn` moves is a different file.
            self.assertFalse((Path(tmp) / "sym-8.md").exists())

    def test_the_refusal_happens_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = INTAKE.HANDOFF_HEADER.format(ticket="SYM-8") + "\n\nsó prosa\n"
            refusal_of(INTAKE.write_handoff, "SYM-8", broken, handoff_dir=tmp)
            # The whole point of validating on the write: nothing exists to
            # be discovered at the end of the chain.
            self.assertFalse(INTAKE.handoff_path("SYM-8", handoff_dir=tmp).exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


# --- the entrypoint ---------------------------------------------------------


def call(argv, handoff_dir):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = INTAKE.main(argv, handoff_dir=handoff_dir)
    return code, out.getvalue(), err.getvalue()


class Main(unittest.TestCase):
    def test_a_good_call_writes_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "draft.md"
            draft.write_text(HANDOFF, encoding="utf-8")
            code, out, err = call(["handoff", "--ticket", "SYM-8", "--file", str(draft)], tmp)
            self.assertEqual(code, 0, err)
            self.assertIn("sym-8-intake.md", out)

    def test_a_refusal_prints_the_four_lines_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "draft.md"
            draft.write_text(INTAKE.HANDOFF_HEADER.format(ticket="SYM-8") + "\n\nprosa\n",
                             encoding="utf-8")
            code, out, err = call(["handoff", "--ticket", "SYM-8", "--file", str(draft)], tmp)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(
                [line.split(":", 1)[0] for line in err.strip().splitlines()],
                ["Blocked", "Accepted", "Example", "Kind"],
            )

    def test_a_missing_file_is_a_refusal_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = call(
                ["handoff", "--ticket", "SYM-8", "--file", str(Path(tmp) / "nope.md")], tmp)
            self.assertEqual(code, 2)
            self.assertIn("Kind: refused", err)

    def test_a_missing_parameter_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = call(["handoff", "--ticket", "SYM-8"], tmp)
            self.assertEqual(code, 2)
            self.assertIn("--file", err)
            self.assertIn("Kind: incomplete", err)

    def test_an_unknown_verb_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = call(["briefing", "--ticket", "SYM-8"], tmp)
            self.assertEqual(code, 2)
            self.assertIn("Kind: refused", err)


if __name__ == "__main__":
    unittest.main()
