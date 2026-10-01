"""Saying what is known to be wrong with the data, rather than answering quietly.

A query's answer looks the same whether the data behind it was read cleanly
or not, so the choices made reading it, and what validation knows, are said
on stderr -- and a header that could mean two things is not guessed at.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from gridql import build_sample_network, read_csv, save_network, validate
from gridql.cli import main
from gridql.ingest import CsvError


def write(directory, text):
    Path(directory, "devices.csv").write_text(text, encoding="utf-8")


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CollidingHeaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def test_two_headers_for_one_field_with_neither_named_for_it_are_refused(self):
        write(
            self.directory.name,
            "mrid,type,feeder,position,status\nSW-1,switch,F1,OPEN,IN SERVICE\n",
        )
        with self.assertRaisesRegex(CsvError, "position, status all mean state"):
            read_csv(self.directory.name)

    def test_the_header_named_for_the_field_wins_and_the_other_is_kept(self):
        write(
            self.directory.name,
            "mrid,type,feeder,state,status,name,description\n"
            "SW-1,switch,F1,OPEN,IN SERVICE,,Lateral switch\n",
        )
        document = read_csv(self.directory.name)
        switch = document.network.objects["SW-1"]
        self.assertEqual(switch.state, "OPEN")
        self.assertEqual(switch.extras["status"], "IN SERVICE")
        self.assertEqual(switch.extras["description"], "Lateral switch")
        self.assertEqual(switch.name, "SW-1")
        self.assertTrue(any("status means the same field" in c for c in document.report.cautions))

    def test_a_header_used_once_is_read_as_before(self):
        write(self.directory.name, "mrid,type,feeder,status\nSW-1,switch,F1,OPEN\n")
        self.assertEqual(read_csv(self.directory.name).network.objects["SW-1"].state, "OPEN")


class MagnitudeTests(unittest.TestCase):
    def test_a_voltage_in_volts_is_reported_once_for_all_of_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        write(
            directory.name,
            "mrid,type,feeder,voltage,from_node,to_node\n"
            "BKR,breaker,F1,12470,N0,N1\nTX,transformer,F1,12470,N1,N2\n",
        )
        findings = [
            f for f in validate(read_csv(directory.name).network) if f.code == "implausible-value"
        ]
        self.assertEqual(len(findings), 1)
        self.assertIn("written in volts", findings[0].message)
        self.assertEqual(set(findings[0].objects), {"BKR", "TX"})

    def test_a_believable_network_has_no_such_finding(self):
        codes = {f.code for f in validate(build_sample_network())}
        self.assertNotIn("implausible-value", codes)


class QueryWarningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        write(
            self.directory.name,
            "mrid,type,feeder,from_node,to_node,state\n"
            "BKR,breaker,F1,N0,N1,CLOSED\n"
            "SW-1,switch,F1,N1,N2,IN SERVICE\n"
            "LD-1,load,F1,N2,,\n",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_a_query_says_what_validation_and_the_loader_know(self):
        code, out, err = run_cli(["--no-config", "--csv", self.directory.name, "FIND loads"])
        self.assertEqual(code, 0)
        self.assertIn("LD-1", out)
        self.assertIn("1 error from validation, starting with invalid-state", err)
        self.assertIn("inferred BKR", err)
        self.assertIn("gridql validate --csv", err)

    def test_validate_does_not_say_it_twice(self):
        _, out, err = run_cli(["validate", "--no-config", "--csv", self.directory.name])
        self.assertIn("invalid-state", out)
        self.assertNotIn("from validation", err)

    def test_a_clean_database_is_answered_quietly(self):
        database = Path(self.directory.name, "grid.sqlite")
        save_network(build_sample_network(), database)
        code, _, err = run_cli(["--no-config", "--db", str(database), "FIND loads"])
        self.assertEqual((code, err), (0, ""))


class SampleNoticeTests(unittest.TestCase):
    def test_a_query_answered_from_the_sample_says_so(self):
        code, out, err = run_cli(["--no-config", "FIND reclosers"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", out)
        self.assertIn("answering from the bundled sample network FDR-104", err)


if __name__ == "__main__":
    unittest.main()
