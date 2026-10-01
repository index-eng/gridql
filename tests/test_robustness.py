"""Files as utilities have them, and networks at a utility's size."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gridql import Network, build_sample_network, read_csv, validate
from gridql.cim import CimImportError, export_network, loads_cim, read_cim
from gridql.cli import main
from gridql.config import load_config
from gridql.ingest import CsvError

LATIN = "mrid,name,type,feeder,from_node,to_node\nBKR,Caf\xe9 Breaker,breaker,F1,N0,N1\n"


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        Path(self.directory.name, "devices.csv").write_bytes(LATIN.encode("cp1252"))

    def tearDown(self):
        self.directory.cleanup()

    def test_a_file_that_is_not_utf8_is_refused_with_its_line(self):
        with self.assertRaisesRegex(CsvError, r"devices\.csv:2: not UTF-8 text.*cp1252"):
            read_csv(self.directory.name)

    def test_naming_the_encoding_reads_it(self):
        network = read_csv(self.directory.name, encoding="cp1252").network
        self.assertEqual(network.objects["BKR"].name, "Café Breaker")

    def test_an_unknown_encoding_is_refused(self):
        with self.assertRaisesRegex(CsvError, "unknown encoding"):
            read_csv(self.directory.name, encoding="nonsense")

    def test_the_command_line_reports_it_without_a_traceback(self):
        code, _, err = run_cli(["--no-config", "--csv", self.directory.name, "FIND devices"])
        self.assertEqual(code, 1)
        self.assertIn("not UTF-8 text", err)
        self.assertNotIn("Traceback", err)
        code, out, _ = run_cli(
            ["--no-config", "--csv", self.directory.name, "--encoding", "cp1252", "FIND devices"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Café Breaker", out)

    def test_a_project_can_name_its_encoding(self):
        config = Path(self.directory.name, "project.gridqlconfig")
        config.write_text('csv = "."\nencoding = "cp1252"\n', encoding="utf-8")
        self.assertEqual(load_config(config).encoding, "cp1252")
        code, out, _ = run_cli(["--config", str(config), "FIND devices"])
        self.assertEqual(code, 0)
        self.assertIn("Café Breaker", out)


class CimEncodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def test_the_xml_declaration_says_how_the_file_is_read(self):
        network = build_sample_network()
        network.objects["LOAD-001"].name = "Café Élan"
        text = export_network(network).replace("encoding='utf-8'", "encoding='iso-8859-1'")
        path = Path(self.directory.name, "latin.xml")
        path.write_bytes(text.encode("iso-8859-1"))
        self.assertEqual(read_cim(path).network.objects["LOAD-001"].name, "Café Élan")

    def test_a_doctype_is_refused_in_utf16_too(self):
        path = Path(self.directory.name, "doctype.xml")
        path.write_bytes(
            '<?xml version="1.0" encoding="utf-16"?>\n'
            '<!DOCTYPE x [<!ENTITY a "b">]><rdf:RDF/>'.encode("utf-16")
        )
        with self.assertRaisesRegex(CimImportError, "DOCTYPE"):
            read_cim(path)


def many_feeders(count, per_feeder=8):
    network = Network()
    for index in range(count):
        feeder = network.add_feeder(f"F{index}")
        feeder.add_breaker(f"F{index}-B")
        for device in range(per_feeder):
            feeder.add_line(f"F{index}-L{device}")
    return network


class ScaleTests(unittest.TestCase):
    """Work that once grew with feeders times devices, counted rather than timed."""

    def count_feeder_scans(self, action):
        calls = 0
        original = Network.feeders.fget

        def counted(network):
            nonlocal calls
            calls += 1
            return original(network)

        with mock.patch.object(Network, "feeders", property(counted)):
            action()
        return calls

    def test_cim_import_does_not_scan_the_feeders_per_record(self):
        text = export_network(many_feeders(30))
        self.assertLess(self.count_feeder_scans(lambda: loads_cim(text)), 20)

    def test_members_are_indexed_once_while_heads_are_inferred(self):
        network = many_feeders(200)
        for feeder in network.feeders:
            feeder.head = None
        rebuilds = 0
        original = Network.members

        def counted(network_, feeder):
            nonlocal rebuilds
            if network_._members_revision != network_._revision:
                rebuilds += 1
            return original(network_, feeder)

        with mock.patch.object(Network, "members", counted):
            network.infer_heads()
            validate(network)
        self.assertLessEqual(rebuilds, 3)
        self.assertEqual(network.objects["F7"].head, "F7-B")


if __name__ == "__main__":
    unittest.main()
