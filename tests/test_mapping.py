"""Mapping files: a utility's own CSV schema, translated into the model."""

import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path

from gridql import build_sample_network, execute, validate
from gridql.cli import main, network_for, split_parameters
from gridql.config import CONFIG_NAME, load_config
from gridql.errors import GridQLError
from gridql.ingest import CsvError, read_csv, write_csv
from gridql.ingest.mapping import MappingError, load_mapping, parse_mapping

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "mapped"
EXAMPLE_MAPPING = EXAMPLE / "mapping.toml"


def snapshot(network, extras=True):
    def fields(obj):
        values = dataclasses.asdict(obj)
        if not extras:
            values.pop("extras")
        return values

    return (
        {o.mrid: (type(o).__name__, fields(o)) for o in network.objects.values()},
        {mrid: sorted(network.neighbors(mrid)) for mrid in network.objects},
        {feeder.mrid: feeder.head for feeder in network.feeders},
    )


class MappingTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text.lstrip("\n"), encoding="utf-8")
        return path

    def mapping(self, text):
        return self.write("mapping.toml", text)

    def load(self, mapping_text):
        return read_csv(self.directory, self.mapping(mapping_text))

    def refused(self, mapping_text):
        with self.assertRaises(MappingError) as raised:
            self.load(mapping_text)
        return str(raised.exception)


class ExampleTests(unittest.TestCase):
    """examples/mapped is examples/csv as a utility's GIS would export it."""

    def setUp(self):
        self.document = read_csv(EXAMPLE, EXAMPLE_MAPPING)
        self.network = self.document.network

    def rows(self, source):
        return execute(self.network, source).rows()

    def test_it_is_the_csv_example(self):
        plain = read_csv(EXAMPLE.parent / "csv").network
        self.assertEqual(snapshot(self.network), snapshot(plain))
        self.assertEqual(self.network.nodes, plain.nodes)

    def test_it_validates_cleanly(self):
        self.assertEqual(validate(self.network).findings, [])
        self.assertEqual(self.document.report.problems, [])

    def test_connectivity_comes_from_the_nodes_in_each_file(self):
        self.assertEqual(self.network.links(), [])
        self.assertIn("94 connections through 61 nodes", self.document.report.counts())

    def test_an_unmapped_column_stays_queryable(self):
        found = execute(self.network, "FIND transformers WHERE install_year < 1980").mrids
        self.assertEqual(found, ["TX-40331", "TX-40416"])
        self.assertIn(
            "TRANSFORMER.csv: unmapped columns kept as attributes: mounting, install_year",
            self.document.report.notes,
        )

    def test_the_blown_fuse_is_the_only_outage(self):
        self.assertEqual(
            execute(self.network, "FIND devices WHERE state != normal_state").mrids,
            ["FU-1201-04"],
        )
        self.assertEqual(
            self.rows("FIND loads WHERE NOT energized SELECT COUNT(*), SUM(customer_count)"),
            [{"COUNT(*)": 2, "SUM(customer_count)": 9}],
        )

    def test_topology_follows_the_feeder_as_built(self):
        self.assertEqual(
            execute(self.network, 'FIND devices UPSTREAM OF "SP-40335" LIMIT 4').mrids,
            ["TX-40335", "OH-1201-25", "OH-1201-24", "FU-1201-04"],
        )
        self.assertEqual(
            execute(self.network, 'FIND reclosers UPSTREAM OF "SP-40335"').mrids,
            ["REC-1201-01"],
        )
        self.assertEqual(
            execute(self.network, 'FIND loads DOWNSTREAM OF "SEC-1201-01"').mrids,
            ["SP-40340", "SP-40318", "SP-40331", "SP-40322", "SP-40335"],
        )

    def test_the_tie_joins_the_two_feeders_but_feeds_neither(self):
        tie = self.network.get("TIE-1201-1202")
        self.assertTrue(tie.is_tie)
        self.assertEqual(
            sorted(execute(self.network, 'FIND devices CONNECTED TO "TIE-1201-1202"').mrids),
            ["OH-1201-07", "OH-1202-03"],
        )
        self.assertNotIn(
            "OH-1202-03",
            execute(self.network, 'FIND devices DOWNSTREAM OF "OH-1201-07"').mrids,
        )

    def test_written_back_out_it_reads_without_the_mapping(self):
        with tempfile.TemporaryDirectory() as out:
            write_csv(self.network, out)
            back = read_csv(out).network
            self.assertEqual(snapshot(back), snapshot(self.network))
            self.assertEqual(back.nodes, self.network.nodes)


class FieldTests(MappingTestCase):
    def test_a_template_builds_an_identifier_from_columns(self):
        self.write("SW.csv", "OBJECTID,CKT\n7,104\n")
        network = self.load('[[devices]]\nfile = "SW.csv"\nmrid = "SW-{OBJECTID}"\nfeeder = "FDR-{CKT}"\n').network
        self.assertEqual(network.get("SW-7").feeder, "FDR-104")
        self.assertIn("FDR-104", network)

    def test_a_template_with_an_empty_column_is_empty_not_half_an_id(self):
        self.write("SW.csv", "OBJECTID,NAME\n7,a\n,b\n")
        document = self.load('[[devices]]\nfile = "SW.csv"\nmrid = "SW-{OBJECTID}"\n')
        self.assertEqual([d.mrid for d in document.network.devices], ["SW-7"])
        self.assertTrue(any("no mRID" in p for p in document.report.problems))

    def test_a_constant_types_the_whole_file(self):
        self.write("XF.csv", "ID,RATING\nX1,50\n")
        network = self.load(
            '[[devices]]\nfile = "XF.csv"\nmrid = "ID"\nkva = "RATING"\n'
            'type = { value = "transformer" }\n'
        ).network
        self.assertEqual(network.get("X1").kva, 50.0)

    def test_codes_are_translated_case_insensitively(self):
        self.write("SW.csv", "ID,POS,KIND\nA,o,rcl\nB,C,BKR\n")
        network = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\n'
            'state = { column = "POS", values = { O = "OPEN", C = "CLOSED" } }\n'
            'type = { column = "KIND", values = { RCL = "recloser", BKR = "breaker" } }\n'
        ).network
        self.assertEqual((network.get("A").TYPE, network.get("A").state), ("recloser", "OPEN"))
        self.assertEqual((network.get("B").TYPE, network.get("B").state), ("breaker", "CLOSED"))

    def test_an_untranslated_code_is_counted_and_used_as_written(self):
        self.write("SW.csv", "ID,POS\nA,X\nB,X\nC,O\n")
        document = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\ntype = { value = "switch" }\n'
            'state = { column = "POS", values = { O = "OPEN" } }\n'
        )
        self.assertEqual(document.network.get("A").state, "X")
        self.assertIn(
            "SW.csv: POS value 'X' has no translation for state (2 rows); used as written",
            document.report.notes,
        )

    def test_a_unit_says_what_bare_cells_are_written_in(self):
        self.write("LD.csv", "ID,VOLTS,DEMAND\nL1,480,0.31\nL2,0.24kV,48kW\n")
        network = self.load(
            '[[devices]]\nfile = "LD.csv"\nmrid = "ID"\ntype = { value = "load" }\n'
            'voltage = { column = "VOLTS", unit = "V" }\n'
            'kw = { column = "DEMAND", unit = "MW" }\n'
        ).network
        self.assertEqual((network.get("L1").voltage, network.get("L1").kw), (0.48, 310.0))
        # A cell that carries its own unit is taken at its word.
        self.assertEqual((network.get("L2").voltage, network.get("L2").kw), (0.24, 48.0))

    def test_a_default_fills_empty_cells(self):
        self.write("SW.csv", "ID,PH\nA,\nB,A\n")
        network = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\nphases = { column = "PH", default = "ABC" }\n'
        ).network
        self.assertEqual((network.get("A").phases, network.get("B").phases), ("ABC", "A"))

    def test_headers_match_case_insensitively(self):
        self.write("SW.csv", " Facility_Id \nA\n")
        network = self.load('[[devices]]\nfile = "SW.csv"\nmrid = "FACILITY_ID"\n').network
        self.assertIn("A", network)


class UnmappedColumnTests(MappingTestCase):
    def test_unmapped_columns_are_kept_as_attributes(self):
        self.write("SW.csv", "ID,Install Year,Mfr\nA,1998,S&C\n")
        network = self.load('[[devices]]\nfile = "SW.csv"\nmrid = "ID"\n').network
        self.assertEqual(network.get("A").extras, {"install_year": 1998, "mfr": "S&C"})

    def test_extras_false_keeps_none(self):
        self.write("SW.csv", "ID,Install Year\nA,1998\n")
        network = self.load('[[devices]]\nfile = "SW.csv"\nmrid = "ID"\nextras = false\n').network
        self.assertEqual(network.get("A").extras, {})

    def test_extras_can_name_the_ones_to_keep(self):
        self.write("SW.csv", "ID,Install Year,Mfr\nA,1998,S&C\n")
        network = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\nextras = ["INSTALL YEAR"]\n'
        ).network
        self.assertEqual(network.get("A").extras, {"install_year": 1998})

    def test_a_mapping_never_guesses_from_a_header(self):
        # STATUS is the alias reader's guess for a switch's position, but a
        # utility's STATUS is as likely to mean "in service". Unmapped, it is
        # just an attribute.
        self.write("SW.csv", "ID,STATUS\nA,OPEN\n")
        network = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\ntype = { value = "switch" }\n'
        ).network
        self.assertEqual(network.get("A").state, "CLOSED")
        self.assertEqual(network.get("A").extras, {"status": "OPEN"})

    def test_a_column_named_like_a_field_is_reported_not_hidden(self):
        # Kept as 'length' it would sit behind the model's own length field.
        self.write("LN.csv", "ID,LENGTH\nA,300\n")
        document = self.load(
            '[[devices]]\nfile = "LN.csv"\nmrid = "ID"\ntype = { value = "line" }\n'
        )
        self.assertIsNone(document.network.get("A").length)
        self.assertEqual(document.network.get("A").extras, {})
        self.assertTrue(any("LENGTH" in note and "not kept" in note for note in document.report.notes))

    def test_a_mapped_field_the_type_lacks_is_kept_as_an_attribute(self):
        self.write("SW.csv", "ID,RATING\nA,600\n")
        network = self.load(
            '[[devices]]\nfile = "SW.csv"\nmrid = "ID"\ntype = { value = "switch" }\nkva = "RATING"\n'
        ).network
        self.assertEqual(network.get("A").extras, {"kva": 600})


class StructureTests(MappingTestCase):
    def test_several_files_of_devices_and_a_connection_table(self):
        self.write("BKR.csv", "ID,CKT\nB1,1\n")
        self.write("XF.csv", "ID,CKT\nX1,1\n")
        self.write("LINKS.csv", "A_END,B_END\nB1,X1\n")
        network = self.load(
            '[[devices]]\nfile = "BKR.csv"\nmrid = "ID"\nfeeder = "CKT"\ntype = { value = "breaker" }\n'
            '[[devices]]\nfile = "XF.csv"\nmrid = "ID"\nfeeder = "CKT"\ntype = { value = "transformer" }\n'
            '[connections]\nfile = "LINKS.csv"\nfrom_device = "A_END"\nto_device = "B_END"\n'
        ).network
        self.assertEqual(execute(network, 'FIND devices DOWNSTREAM OF "B1"').mrids, ["X1"])

    def test_a_file_the_mapping_names_must_exist(self):
        with self.assertRaises(CsvError) as raised:
            self.load('[[devices]]\nfile = "NOPE.csv"\nmrid = "ID"\n')
        self.assertIn("NOPE.csv", str(raised.exception))

    def test_a_missing_column_names_the_mapping_and_suggests(self):
        self.write("SW.csv", "FACILITY_ID\nA\n")
        message = self.refused('[[devices]]\nfile = "SW.csv"\nmrid = "FACILTY_ID"\n')
        self.assertIn("mapping.toml", message)
        self.assertIn("did you mean FACILITY_ID?", message)

    def test_a_listed_extra_must_exist(self):
        self.write("SW.csv", "ID\nA\n")
        message = self.refused('[[devices]]\nfile = "SW.csv"\nmrid = "ID"\nextras = ["MFR"]\n')
        self.assertIn("no column 'MFR'", message)

    def test_a_mapping_needs_a_directory(self):
        path = self.write("SW.csv", "ID\nA\n")
        with self.assertRaises(CsvError):
            read_csv(path, self.mapping('[[devices]]\nfile = "SW.csv"\nmrid = "ID"\n'))

    def test_file_overrides_do_not_mix_with_a_mapping(self):
        self.write("SW.csv", "ID\nA\n")
        with self.assertRaises(CsvError):
            read_csv(
                self.directory,
                self.mapping('[[devices]]\nfile = "SW.csv"\nmrid = "ID"\n'),
                devices=self.directory / "SW.csv",
            )


class MalformedMappingTests(unittest.TestCase):
    def refused(self, data):
        with self.assertRaises(MappingError) as raised:
            parse_mapping(data, "m.toml")
        return str(raised.exception)

    def devices(self, **fields):
        return {"devices": {"file": "SW.csv", "mrid": "ID", **fields}}

    def test_an_unknown_section_is_refused_with_a_suggestion(self):
        message = self.refused({"device": {"file": "SW.csv", "mrid": "ID"}})
        self.assertIn("did you mean devices?", message)

    def test_an_unknown_field_is_refused_with_a_suggestion(self):
        self.assertIn("did you mean kva?", self.refused(self.devices(kvaa="X")))

    def test_equipment_is_required(self):
        self.assertIn("no [[devices]]", self.refused({"feeders": {"file": "F.csv", "mrid": "ID"}}))

    def test_every_section_needs_a_file_and_an_mrid(self):
        self.assertIn("needs file", self.refused({"devices": {"mrid": "ID"}}))
        self.assertIn("nothing maps mrid", self.refused({"devices": {"file": "SW.csv"}}))
        message = self.refused({**self.devices(), "connections": {"file": "C.csv", "from_device": "A"}})
        self.assertIn("nothing maps to_device", message)

    def test_a_field_takes_exactly_one_source(self):
        message = self.refused(self.devices(name={"column": "A", "value": "B"}))
        self.assertIn("exactly one of column, template or value", message)

    def test_a_broken_template_is_refused(self):
        self.assertIn("single braces", self.refused(self.devices(name="SW-{ID")))
        self.assertIn("empty {}", self.refused(self.devices(name="SW-{}")))

    def test_units_must_fit_the_field(self):
        self.assertIn("takes no unit", self.refused(self.devices(name={"column": "N", "unit": "V"})))
        self.assertIn("unknown unit", self.refused(self.devices(kva={"column": "K", "unit": "furlong"})))
        message = self.refused(self.devices(voltage={"column": "V", "unit": "kW"}))
        self.assertIn("'kW' is real power, not voltage", message)

    def test_extras_on_connections_means_nothing(self):
        message = self.refused(
            {**self.devices(), "connections": {"file": "C.csv", "from_device": "A",
                                               "to_device": "B", "extras": True}}
        )
        self.assertIn("connections carry no attributes", message)

    def test_broken_toml_names_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.toml"
            path.write_text("[[devices]\n")
            with self.assertRaises(MappingError) as raised:
                load_mapping(path)
            self.assertIn("m.toml", str(raised.exception))

    def test_toml_numbers_and_booleans_read_as_the_text_a_cell_would_hold(self):
        mapping = parse_mapping(
            self.devices(state={"column": "POS", "values": {"0": "OPEN", "1": "CLOSED"}},
                         is_tie={"value": True})
        )
        fields = mapping.of("devices")[0].fields
        self.assertEqual(fields["is_tie"].value, "true")
        self.assertEqual(fields["state"].values, {"0": "OPEN", "1": "CLOSED"})


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_import_csv_with_a_mapping_reports_what_it_made_of_the_files(self):
        code, out, _ = self.run_cli(
            ["import-csv", str(EXAMPLE), "--mapping", str(EXAMPLE_MAPPING), "--no-config"]
        )
        self.assertEqual(code, 0)
        self.assertIn("loaded 77 devices", out)
        self.assertIn("kept as attributes: mounting, install_year", out)
        self.assertIn("validation: no problems found", out)

    def test_a_query_against_mapped_files(self):
        code, out, _ = self.run_cli(
            ["--csv", str(EXAMPLE), "--mapping", str(EXAMPLE_MAPPING), "FIND reclosers"]
        )
        self.assertEqual(code, 0)
        self.assertIn("REC-1201-01", out)

    def test_run_keeps_the_mapping_for_itself_not_as_a_parameter(self):
        rest, values = split_parameters(["q.gridql", "--mapping", "m.toml", "--feeder", "F"])
        self.assertEqual(rest, ["q.gridql", "--mapping", "m.toml"])
        self.assertEqual(values, {"feeder": "F"})

    def test_a_mapping_without_csv_is_refused(self):
        code, _, err = self.run_cli(["--mapping", str(EXAMPLE_MAPPING), "--no-config", "FIND feeders"])
        self.assertEqual(code, 1)
        self.assertIn("use it with --csv", err)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def test_the_project_mapping_resolves_against_the_config(self):
        (self.root / CONFIG_NAME).write_text(f'csv = "{EXAMPLE}"\nmapping = "gis.toml"\n')
        config = load_config(self.root / CONFIG_NAME)
        self.assertEqual(config.mapping, str(self.root / "gis.toml"))
        self.assertIn("mapping: gis.toml", config.describe())

    def test_the_project_mapping_reads_the_project_csv(self):
        (self.root / CONFIG_NAME).write_text(
            f'csv = "{EXAMPLE}"\nmapping = "{EXAMPLE_MAPPING}"\n'
        )
        network = network_for(config=load_config(self.root / CONFIG_NAME))
        self.assertEqual(len(network.devices), 77)

    def test_the_project_mapping_applies_to_other_csv_too(self):
        # It describes the utility's export format, not one directory of it.
        (self.root / CONFIG_NAME).write_text(f'mapping = "{EXAMPLE_MAPPING}"\n')
        network = network_for(csv=str(EXAMPLE), config=load_config(self.root / CONFIG_NAME))
        self.assertEqual(len(network.devices), 77)

    def test_a_database_ignores_the_project_mapping(self):
        from gridql.storage import save_network

        save_network(build_sample_network(), self.root / "grid.sqlite")
        (self.root / CONFIG_NAME).write_text('db = "grid.sqlite"\nmapping = "gis.toml"\n')
        network = network_for(config=load_config(self.root / CONFIG_NAME))
        self.assertEqual(len(network.devices), 11)

    def test_an_explicit_mapping_with_a_database_is_refused(self):
        with self.assertRaises(GridQLError):
            network_for(db="grid.sqlite", mapping=str(EXAMPLE_MAPPING))


if __name__ == "__main__":
    unittest.main()
