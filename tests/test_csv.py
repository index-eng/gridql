"""Reading networks from CSV -- the shape a utility will actually hand you."""

import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute, load_network, save_network
from gridql.cli import main
from gridql.ingest import CsvError, read_csv, write_csv
from gridql.model import Device, Switch, Transformer

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project


def setUpModule():
    unittest.enterModuleContext(sample_project())


EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "csv"


def snapshot(network):
    return (
        {o.mrid: (type(o).__name__, dataclasses.asdict(o)) for o in network.objects.values()},
        {mrid: sorted(network.neighbors(mrid)) for mrid in network.objects},
        {feeder.mrid: feeder.head for feeder in network.feeders},
    )


class CsvTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text.lstrip("\n"), encoding="utf-8")
        return path

    def load(self, **overrides):
        return read_csv(self.directory, **overrides)


class RoundTripTests(CsvTestCase):
    def test_a_network_written_and_read_back_is_identical(self):
        original = build_sample_network()
        write_csv(original, self.directory)
        self.assertEqual(snapshot(read_csv(self.directory).network), snapshot(original))

    def test_queries_agree_across_the_round_trip(self):
        original = build_sample_network()
        write_csv(original, self.directory)
        back = read_csv(self.directory).network
        for source in (
            "FIND reclosers",
            'FIND devices DOWNSTREAM OF "REC-001"',
            "FIND transformers WHERE kva >= 500",
            "FIND switches WHERE state != normal_state",
            "FIND loads SELECT SUM(kw)",
        ):
            with self.subTest(query=source):
                self.assertEqual(
                    execute(back, source).rows(), execute(original, source).rows()
                )

    def test_the_bundled_example_loads_and_validates(self):
        from gridql import validate

        document = read_csv(EXAMPLES)
        self.assertEqual(len(document.network.devices), 77)
        self.assertEqual(len(document.network.feeders), 2)
        self.assertEqual(document.report.problems, [])
        self.assertEqual(validate(document.network).findings, [])

    def test_extras_survive_the_round_trip_as_their_own_columns(self):
        network = Network()
        feeder = network.add_feeder("FDR-9", voltage="12.47kV")
        feeder.add_breaker("BRK-9", extras={"install_year": 1998, "pole": "00412"})
        write_csv(network, self.directory)
        self.assertIn("install_year", (self.directory / "devices.csv").read_text())
        back = read_csv(self.directory).network
        self.assertEqual(back.get("BRK-9").extras, {"install_year": 1998, "pole": "00412"})

    def test_feeder_and_substation_extras_survive_the_round_trip(self):
        self.write("substations.csv", "mrid,region\nSUB-1,North\n")
        self.write("feeders.csv", "mrid,substation,miles\nFDR-1,SUB-1,12.5\n")
        self.write("devices.csv", "mrid,type,feeder\nBRK-1,breaker,FDR-1\n")
        network = self.load().network
        out = self.directory / "out"
        write_csv(network, out)
        back = read_csv(out).network
        self.assertEqual(back.get("SUB-1").extras, {"region": "North"})
        self.assertEqual(back.get("FDR-1").extras, {"miles": 12.5})

    def test_an_extra_sharing_a_column_name_is_written_once(self):
        # The widget is generic equipment, so its kva is an extra -- the same
        # name as the transformer's own column.
        self.write("devices.csv", "mrid,type,feeder,kva\nX1,transformer,F1,500\nG1,widget,F1,25\n")
        network = self.load().network
        out = self.directory / "out"
        write_csv(network, out)
        header = (out / "devices.csv").read_text().splitlines()[0].split(",")
        self.assertEqual(header.count("kva"), 1)
        back = read_csv(out).network
        self.assertEqual(back.get("X1").kva, 500.0)
        self.assertEqual(back.get("G1").extras["kva"], 25)


class HeaderTests(CsvTestCase):
    def test_real_world_column_names_are_understood(self):
        self.write("devices.csv", """
OBJECTID,Description,Device Type,Circuit,Phase,kV,Normal Position,Position
BKR-1,Cedar Breaker,breaker,FDR-12,ABC,12.47,CLOSED,CLOSED
SW-1,Cedar Switch,switch,FDR-12,ABC,12.47,CLOSED,OPEN
""")
        network = self.load().network
        breaker = network.get("BKR-1")
        self.assertEqual(breaker.name, "Cedar Breaker")
        self.assertEqual(breaker.feeder, "FDR-12")
        self.assertEqual(breaker.phases, "ABC")
        self.assertEqual(breaker.voltage, 12.47)
        self.assertEqual(network.get("SW-1").state, "OPEN")

    def test_a_byte_order_mark_does_not_break_the_first_column(self):
        (self.directory / "devices.csv").write_text(
            "mrid,type\nBRK-1,breaker\n", encoding="utf-8-sig"
        )
        self.assertEqual(self.load().network.get("BRK-1").TYPE, "breaker")

    def test_units_in_cells_are_converted(self):
        self.write("devices.csv", """
mrid,type,kv,rating kVA
XF-1,transformer,12470 V,0.5MVA
""")
        transformer = self.load().network.get("XF-1")
        self.assertEqual(transformer.voltage, 12.47)
        self.assertEqual(transformer.kva, 500.0)

    def test_unrecognised_columns_become_queryable_attributes(self):
        self.write("devices.csv", """
mrid,type,install_year,pole_number
BRK-1,breaker,1998,00412
BRK-2,breaker,2015,00500
""")
        network = self.load().network
        self.assertEqual(
            execute(network, "FIND breakers WHERE install_year < 2000").mrids, ["BRK-1"]
        )
        self.assertEqual(
            execute(network, "FIND breakers SELECT mrid, install_year ORDER BY install_year").rows(),
            [{"mrid": "BRK-1", "install_year": 1998}, {"mrid": "BRK-2", "install_year": 2015}],
        )

    def test_an_identifier_with_a_leading_zero_stays_text(self):
        self.write("devices.csv", "mrid,type,pole\nBRK-1,breaker,00412\n")
        self.assertEqual(self.load().network.get("BRK-1").extras["pole"], "00412")


class TypeTests(CsvTestCase):
    def test_gridql_type_names(self):
        self.write("devices.csv", """
mrid,type
A,recloser
B,reclosers
C,xfmr
""")
        network = self.load().network
        self.assertEqual(network.get("A").TYPE, "recloser")
        self.assertEqual(network.get("B").TYPE, "recloser")
        self.assertIsInstance(network.get("C"), Transformer)

    def test_a_capacitor_keeps_its_recorded_position(self):
        self.write(
            "devices.csv",
            "mrid,type,feeder,state,normal_state,kvar\nCAP-1,capacitor,F1,open,CLOSED,600\n",
        )
        capacitor = self.load().network.get("CAP-1")
        self.assertEqual((capacitor.state, capacitor.normal_state), ("OPEN", "CLOSED"))
        self.assertNotIn("state", capacitor.extras)

    def test_cim_class_names_from_a_gis_export(self):
        self.write("devices.csv", """
mrid,type
A,LoadBreakSwitch
B,Disconnector
C,PowerTransformer
D,ACLineSegment
E,ConformLoad
""")
        network = self.load().network
        self.assertIsInstance(network.get("A"), Switch)
        self.assertIsInstance(network.get("B"), Switch)
        self.assertIsInstance(network.get("C"), Transformer)
        self.assertEqual(network.get("D").TYPE, "line")
        self.assertEqual(network.get("E").TYPE, "load")

    def test_an_unknown_type_is_kept_as_generic_equipment_and_reported(self):
        self.write("devices.csv", "mrid,type\nSVC-1,SERVICE_POINT\n")
        document = self.load()
        device = document.network.get("SVC-1")
        self.assertIs(type(device), Device)
        self.assertEqual(device.extras["source_type"], "SERVICE_POINT")
        self.assertTrue(any("SERVICE_POINT" in p for p in document.report.problems))


class ContainerTests(CsvTestCase):
    def test_feeders_are_created_from_what_the_devices_refer_to(self):
        self.write("devices.csv", """
mrid,type,feeder,substation
BRK-1,breaker,FDR-1,SUB-1
SW-1,switch,FDR-1,SUB-1
""")
        network = self.load().network
        self.assertEqual([f.mrid for f in network.feeders], ["FDR-1"])
        self.assertEqual([s.mrid for s in network.substations], ["SUB-1"])

    def test_a_head_is_inferred_from_the_only_breaker(self):
        self.write("devices.csv", """
mrid,type,feeder
BRK-1,breaker,FDR-1
SW-1,switch,FDR-1
""")
        self.write("connections.csv", "from,to\nBRK-1,SW-1\n")
        document = self.load()
        self.assertEqual(document.network.feeders[0].head, "BRK-1")
        self.assertTrue(any("inferred BRK-1" in n for n in document.report.notes))
        self.assertEqual(
            execute(document.network, 'FIND devices DOWNSTREAM OF "BRK-1"').mrids, ["SW-1"]
        )

    def test_an_explicit_head_is_used(self):
        self.write("devices.csv", """
mrid,type,feeder
BRK-1,breaker,FDR-1
BRK-2,breaker,FDR-1
""")
        self.write("feeders.csv", "mrid,name,kv,head\nFDR-1,Cedar,12.47,BRK-2\n")
        network = self.load().network
        self.assertEqual(network.feeders[0].head, "BRK-2")
        self.assertEqual(network.feeders[0].voltage, 12.47)

    def test_an_ambiguous_head_is_reported_rather_than_guessed(self):
        self.write("devices.csv", """
mrid,type,feeder
BRK-1,breaker,FDR-1
BRK-2,breaker,FDR-1
""")
        document = self.load()
        self.assertIsNone(document.network.feeders[0].head)
        self.assertTrue(any("none could be inferred" in n for n in document.report.notes))

    def test_a_head_naming_a_device_that_is_absent(self):
        self.write("devices.csv", "mrid,type,feeder\nSW-1,switch,FDR-1\n")
        self.write("feeders.csv", "mrid,head\nFDR-1,GHOST\n")
        document = self.load()
        self.assertTrue(any("GHOST" in n for n in document.report.notes))

    def test_a_substation_only_a_feeder_names_is_created(self):
        # Otherwise the feeder points at nothing and saving it fails.
        self.write("feeders.csv", "mrid,substation\nFDR-1,SUB-X\n")
        self.write("devices.csv", "mrid,type,feeder\nBRK-1,breaker,FDR-1\n")
        network = self.load().network
        self.assertIn("SUB-X", network)
        database = self.directory / "grid.sqlite"
        save_network(network, database)
        self.assertEqual(load_network(database).get("FDR-1").substation, "SUB-X")


class BadRowTests(CsvTestCase):
    def test_rows_without_an_mrid_are_skipped(self):
        self.write("devices.csv", "mrid,type\n,breaker\nBRK-1,breaker\n")
        document = self.load()
        self.assertEqual([d.mrid for d in document.network.devices], ["BRK-1"])
        self.assertTrue(any("no mRID" in p for p in document.report.problems))

    def test_duplicate_mrids_are_skipped(self):
        self.write("devices.csv", "mrid,type\nBRK-1,breaker\nBRK-1,switch\n")
        document = self.load()
        self.assertEqual(len(document.network.devices), 1)
        self.assertTrue(any("duplicate" in p for p in document.report.problems))

    def test_an_unreadable_number_is_reported_and_left_empty(self):
        self.write("devices.csv", "mrid,type,kv\nBRK-1,breaker,not-a-voltage\n")
        document = self.load()
        self.assertIsNone(document.network.get("BRK-1").voltage)
        self.assertTrue(any("not-a-voltage" in p for p in document.report.problems))

    def test_connections_to_unknown_devices_are_skipped(self):
        self.write("devices.csv", "mrid,type\nA,switch\nB,switch\n")
        self.write("connections.csv", "from,to\nA,B\nA,GHOST\n")
        document = self.load()
        self.assertEqual(document.report.connections, 1)
        self.assertTrue(any("GHOST" in p for p in document.report.problems))

    def test_a_self_connection_is_refused(self):
        self.write("devices.csv", "mrid,type\nA,switch\n")
        self.write("connections.csv", "from,to\nA,A\n")
        document = self.load()
        self.assertEqual(document.report.connections, 0)
        self.assertTrue(any("itself" in p for p in document.report.problems))

    def test_a_connection_to_a_container_is_refused(self):
        self.write("devices.csv", "mrid,type,feeder\nA,switch,FDR-1\n")
        self.write("connections.csv", "from,to\nA,FDR-1\n")
        document = self.load()
        self.assertEqual(document.report.connections, 0)
        self.assertTrue(any("not equipment" in p for p in document.report.problems))

    def test_a_duplicate_feeder_or_substation_row_is_skipped(self):
        self.write("substations.csv", "mrid\nSUB-1\nSUB-1\n")
        self.write("feeders.csv", "mrid\nFDR-1\nFDR-1\nSUB-1\n")
        self.write("devices.csv", "mrid,type,feeder\nBRK-1,breaker,FDR-1\n")
        document = self.load()
        self.assertEqual(len(document.network.feeders), 1)
        self.assertEqual(len(document.network.substations), 1)
        self.assertEqual(sum("duplicate" in p for p in document.report.problems), 3)

    def test_blank_rows_are_ignored(self):
        self.write("devices.csv", "mrid,type\nA,switch\n\n\nB,switch\n")
        self.assertEqual(len(self.load().network.devices), 2)

    def test_the_report_truncates_a_flood_of_problems(self):
        rows = "\n".join(",breaker" for _ in range(40))
        self.write("devices.csv", f"mrid,type\n{rows}\n")
        summary = self.load().report.summary()
        self.assertIn("and 20 more", summary)


class SourceTests(CsvTestCase):
    def test_a_single_file_is_taken_as_the_equipment_list(self):
        path = self.write("equipment.csv", "mrid,type\nBRK-1,breaker\n")
        self.assertEqual(len(read_csv(path).network.devices), 1)

    def test_paths_can_be_given_explicitly(self):
        devices = self.write("a.csv", "mrid,type\nA,switch\nB,switch\n")
        links = self.write("b.csv", "from,to\nA,B\n")
        document = read_csv(self.directory, devices=devices, connections=links)
        self.assertEqual(document.report.connections, 1)

    def test_an_unknown_override_is_refused(self):
        with self.assertRaises(CsvError):
            read_csv(self.directory, widgets="x.csv")

    def test_a_missing_device_file(self):
        with self.assertRaises(CsvError) as raised:
            read_csv(self.directory)
        self.assertIn("no device file", str(raised.exception))

    def test_connections_are_optional(self):
        self.write("devices.csv", "mrid,type\nA,switch\n")
        self.assertEqual(self.load().report.connections, 0)

    def test_an_empty_device_file(self):
        self.write("devices.csv", "mrid,type\n")
        self.assertEqual(len(self.load().network.devices), 0)


class CommandLineTests(CsvTestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def sample_files(self):
        write_csv(build_sample_network(), self.directory)
        return str(self.directory)

    def test_querying_csv_directly(self):
        code, out, _ = self.run_cli(["--csv", self.sample_files(), "FIND reclosers"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", out)

    def test_a_gridql_file_against_csv(self):
        query = Path(__file__).resolve().parent.parent / "queries" / "open_devices.gridql"
        code, out, _ = self.run_cli(["run", str(query), "--csv", self.sample_files()])
        self.assertEqual(code, 0)
        self.assertIn("SW-002", out)

    def test_validating_csv(self):
        code, out, _ = self.run_cli(["validate", "--csv", self.sample_files()])
        self.assertEqual(code, 0)
        self.assertIn("no problems found", out)

    def test_import_csv_reports_and_saves(self):
        database = self.directory / "grid.sqlite"
        code, out, _ = self.run_cli(
            ["import-csv", self.sample_files(), "--db", str(database)]
        )
        self.assertEqual(code, 0)
        self.assertIn("loaded 11 devices", out)
        self.assertIn("validation: no problems found", out)
        self.assertEqual(len(load_network(database).devices), 11)

    def test_import_csv_will_not_clobber_a_database(self):
        database = self.directory / "grid.sqlite"
        source = self.sample_files()
        self.run_cli(["import-csv", source, "--db", str(database)])
        code, _, err = self.run_cli(["import-csv", source, "--db", str(database)])
        self.assertEqual(code, 1)
        self.assertIn("--force", err)

    def test_export_csv(self):
        out_dir = self.directory / "out"
        code, out, _ = self.run_cli(["export-csv", str(out_dir)])
        self.assertEqual(code, 0)
        self.assertIn("devices.csv", out)
        self.assertEqual(len(read_csv(out_dir).network.devices), 11)

    def test_db_and_csv_together_are_refused(self):
        code, _, err = self.run_cli(
            ["--db", "x.sqlite", "--csv", self.sample_files(), "FIND feeders"]
        )
        self.assertEqual(code, 1)
        self.assertIn("not both", err)

    def test_a_missing_csv_source_exits_non_zero(self):
        code, _, err = self.run_cli(["--csv", str(self.directory / "nope"), "FIND feeders"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class LoadWarningTests(CsvTestCase):
    """A query shows only its answer, so a bad load has to say so on the side."""

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*argv, "--no-config"])
        return code, out.getvalue(), err.getvalue()

    def test_a_file_that_loads_nothing_says_so_and_why(self):
        path = self.write("TRANSFORMER.csv", "FACILITY_ID,KVA_RATING\nX1,500\nX2,75\n")
        code, out, err = self.run_cli(["--csv", str(path), "FIND devices"])
        self.assertEqual(code, 0)  # the query still runs
        self.assertIn("no devices matched", out)
        self.assertIn(
            f"warning: no equipment loaded from {path}: 2 rows skipped or incomplete, "
            "starting with TRANSFORMER.csv:2: no mRID",
            err,
        )
        self.assertIn(f"run 'gridql import-csv {path}' for the full report", err)

    def test_skipped_rows_are_counted_and_the_answer_still_shown(self):
        self.write("devices.csv", "mrid,type\n,breaker\nB1,breaker\n")
        code, out, err = self.run_cli(["--csv", str(self.directory), "FIND breakers"])
        self.assertEqual(code, 0)
        self.assertIn("B1", out)
        self.assertIn(f"1 row skipped or incomplete in {self.directory}", err)

    def test_the_report_command_carries_the_mapping(self):
        mapping = self.write("m.toml", '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\n')
        self.write("SW.csv", "ID,X\n,1\nA,2\n")
        _, _, err = self.run_cli(
            ["--csv", str(self.directory), "--mapping", str(mapping), "FIND devices"]
        )
        self.assertIn(f"import-csv {self.directory} --mapping {mapping}", err)

    def test_a_clean_load_is_quiet(self):
        write_csv(build_sample_network(), self.directory)
        _, _, err = self.run_cli(["--csv", str(self.directory), "FIND devices"])
        self.assertEqual(err, "")


class QueryAsPathTests(CsvTestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*argv, "--no-config"])
        return code, out.getvalue(), err.getvalue()

    def test_a_query_given_to_csv_explains_the_order(self):
        code, _, err = self.run_cli(["--csv", "find transformer where install_year > 1990"])
        self.assertEqual(code, 1)
        self.assertIn("--csv takes the folder holding your CSV files, but was given the query", err)
        self.assertIn("gridql --csv ./gis-export 'find transformer where install_year > 1990'", err)

    def test_the_same_for_db_and_mapping(self):
        for option, what in (("--db", "a database file"), ("--mapping", "a mapping file")):
            with self.subTest(option=option):
                code, _, err = self.run_cli([option, "FIND feeders"])
                self.assertEqual(code, 1)
                self.assertIn(f"{option} takes {what}, but was given the query", err)

    def test_a_folder_that_really_starts_with_find_is_read(self):
        write_csv(build_sample_network(), self.directory / "find results")
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        code, out, _ = self.run_cli(["--csv", "find results", "FIND reclosers"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", out)


if __name__ == "__main__":
    unittest.main()
