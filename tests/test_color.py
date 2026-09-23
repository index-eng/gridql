"""Terminal colour: when it is used, what it marks, and that it changes nothing else."""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from gridql import build_sample_network, execute, render, validate
from gridql.cli import main
from gridql.color import Palette, wanted
from gridql.formats import render_script

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project


def setUpModule():
    unittest.enterModuleContext(sample_project())


ESCAPE = re.compile(r"\033\[[0-9;]*m")
ATTENTION = "\033[1;33m"
COLOR = Palette(True)


def plain(text):
    return ESCAPE.sub("", text)


class Terminal(io.StringIO):
    def isatty(self):
        return True


def environment(**values):
    cleared = {key: "" for key in ("NO_COLOR", "FORCE_COLOR")}
    return mock.patch.dict(os.environ, {**cleared, "TERM": "xterm", **values})


class WhenTests(unittest.TestCase):
    def test_a_terminal_is_coloured_and_a_pipe_is_not(self):
        with environment():
            self.assertTrue(wanted(Terminal()))
            self.assertFalse(wanted(io.StringIO()))

    def test_no_color_turns_it_off(self):
        with environment(NO_COLOR="1"):
            self.assertFalse(wanted(Terminal()))

    def test_force_color_turns_it_on_for_a_pipe(self):
        with environment(FORCE_COLOR="1"):
            self.assertTrue(wanted(io.StringIO()))

    def test_a_dumb_terminal_is_not_coloured(self):
        with environment(TERM="dumb"):
            self.assertFalse(wanted(Terminal()))

    def test_an_explicit_choice_wins_over_the_environment(self):
        with environment(NO_COLOR="1"):
            self.assertTrue(wanted(io.StringIO(), "always"))
        with environment(FORCE_COLOR="1"):
            self.assertFalse(wanted(Terminal(), "never"))


class TableTests(unittest.TestCase):
    QUERIES = (
        "FIND switches",
        "FIND devices WHERE NOT energized",
        "FIND devices SELECT mrid, kva, state",
        "FIND loads SELECT feeder, SUM(kw) GROUP BY feeder",
        "FIND devices WHERE kva > 100000",
    )

    def setUp(self):
        self.network = build_sample_network()

    def table(self, query, palette=COLOR):
        return render(execute(self.network, query), "table", palette)

    def row(self, table, mrid):
        return next(line for line in table.splitlines() if plain(line).startswith(mrid))

    def test_colour_changes_nothing_but_colour(self):
        # Alignment is measured on the text, so stripping the escapes must
        # give back the plain table exactly, trailing whitespace included.
        for query in self.QUERIES:
            with self.subTest(query=query):
                self.assertEqual(plain(self.table(query)), self.table(query, Palette(False)))

    def test_a_switch_out_of_its_normal_position_is_marked(self):
        table = self.table("FIND switches SELECT mrid, state, normal_state")
        self.assertIn(f"{ATTENTION}OPEN", self.row(table, "SW-002"))  # normally closed

    def test_a_switch_in_its_normal_position_is_not(self):
        table = self.table("FIND switches SELECT mrid, state, normal_state")
        self.assertNotIn(ATTENTION, self.row(table, "TIE-001"))  # normally open, and open
        self.assertNotIn(ATTENTION, self.row(table, "SW-001"))

    def test_off_normal_is_judged_even_when_normal_state_is_not_shown(self):
        table = self.table("FIND switches SELECT mrid, state")
        self.assertIn(ATTENTION, self.row(table, "SW-002"))
        self.assertNotIn(ATTENTION, self.row(table, "TIE-001"))

    def test_dead_equipment_is_marked(self):
        table = self.table("FIND devices SELECT mrid, energized")
        self.assertIn(f"{ATTENTION}false", self.row(table, "LOAD-002"))
        self.assertNotIn(ATTENTION, self.row(table, "LOAD-001"))

    def test_the_header_is_bold_and_the_rule_dim(self):
        header, rule = self.table("FIND switches").splitlines()[:2]
        self.assertTrue(header.startswith("\033[1m"))
        self.assertTrue(rule.startswith("\033[2m"))

    def test_machine_formats_are_never_coloured(self):
        result = execute(self.network, "FIND switches")
        for output_format in ("json", "csv", "cim"):
            with self.subTest(output_format=output_format):
                self.assertNotIn("\033[", render(result, output_format, COLOR))
        self.assertNotIn("\033[", render_script([result, result], "json", COLOR))

    def test_validation_severities_are_coloured(self):
        self.assertEqual(validate(self.network).summary(COLOR), "\033[32mno problems found\033[0m")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv, stdout=None):
        out, err = stdout or io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_output_to_a_pipe_is_plain_by_default(self):
        with environment():
            _, out, _ = self.run_cli(["FIND switches"])
        self.assertNotIn("\033[", out)

    def test_output_to_a_terminal_is_coloured_by_default(self):
        with environment():
            _, out, _ = self.run_cli(["FIND switches"], stdout=Terminal())
        self.assertIn(ATTENTION, out)

    def test_color_always_and_never(self):
        with environment():
            self.assertIn("\033[", self.run_cli(["--color", "always", "FIND switches"])[1])
            out = self.run_cli(["--color", "never", "FIND switches"], stdout=Terminal())[1]
            self.assertNotIn("\033[", out)

    def test_run_takes_color_as_its_own_option_not_a_parameter(self):
        code, out, err = self.run_cli(["run", "open_devices", "--color=always"])
        self.assertEqual(code, 0, err)
        self.assertIn("\033[", out)

    def test_errors_are_coloured_on_stderr(self):
        _, _, err = self.run_cli(["--color", "always", "--db", "missing.sqlite", "FIND loads"])
        self.assertTrue(err.startswith("\033[1;31merror:\033[0m no such database"))

    def test_a_syntax_error_colours_its_message_and_caret(self):
        _, _, err = self.run_cli(["--color", "always", "FIND switches WHERE"])
        message, query, caret = err.rstrip("\n").split("\n")
        self.assertTrue(message.startswith("\033[1;31m"))
        self.assertEqual(query, "  FIND switches WHERE")
        self.assertTrue(caret.endswith("\033[1;31m^\033[0m"))

    def test_validate_takes_the_option(self):
        _, out, _ = self.run_cli(["validate", "--color", "always"])
        self.assertIn("\033[32mno problems found", out)


if __name__ == "__main__":
    unittest.main()
