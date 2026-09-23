"""SQLite persistence: schema, round-trip fidelity, and the CLI wiring."""

import contextlib
import dataclasses
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute
from gridql.cli import main
from gridql.ingest import write_csv
from gridql.storage import (
    SCHEMA_VERSION,
    StorageError,
    connect,
    load_network,
    object_counts,
    open_database,
    save_network,
)


def snapshot(network):
    """Everything about a network that persistence must preserve."""
    return {
        "objects": {
            obj.mrid: (type(obj).__name__, dataclasses.asdict(obj))
            for obj in network.objects.values()
        },
        "edges": {mrid: sorted(network.neighbors(mrid)) for mrid in network.objects},
        "heads": {feeder.mrid: feeder.head for feeder in network.feeders},
    }


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "grid.sqlite"
        self.network = build_sample_network()


class RoundTripTests(StorageTestCase):
    def test_a_saved_network_loads_back_identical(self):
        save_network(self.network, self.path)
        self.assertEqual(snapshot(load_network(self.path)), snapshot(self.network))

    def test_topology_survives_the_round_trip(self):
        save_network(self.network, self.path)
        loaded = load_network(self.path)
        for mrid in self.network.objects:
            self.assertEqual(
                sorted(o.mrid for o in loaded.downstream_of(mrid)),
                sorted(o.mrid for o in self.network.downstream_of(mrid)),
                mrid,
            )
            self.assertEqual(loaded.is_energized(mrid), self.network.is_energized(mrid), mrid)

    def test_the_feeder_head_is_preserved_rather_than_re_guessed(self):
        save_network(self.network, self.path)
        self.assertEqual(load_network(self.path).feeders[0].head, "BRK-001")

    def test_the_milestone_queries_work_against_a_database(self):
        save_network(self.network, self.path)
        loaded = load_network(self.path)
        self.assertEqual(execute(loaded, "FIND feeders").mrids, ["FDR-104"])
        self.assertEqual(execute(loaded, "FIND reclosers").mrids, ["REC-001"])
        self.assertEqual(
            sorted(execute(loaded, 'FIND devices DOWNSTREAM OF "REC-001"').mrids),
            ["LN-002", "LOAD-001", "LOAD-002", "SW-001", "SW-002", "TIE-001",
             "XFMR-001", "XFMR-002"],
        )
        self.assertEqual(execute(loaded, "FIND transformers WHERE kva >= 500").mrids, ["XFMR-001"])
        self.assertEqual(
            execute(loaded, "FIND switches WHERE state != normal_state").mrids, ["SW-002"]
        )

    def test_subclass_attributes_survive(self):
        save_network(self.network, self.path)
        loaded = load_network(self.path)
        self.assertEqual(loaded.get("XFMR-001").kva, 500.0)
        self.assertEqual(loaded.get("XFMR-001").secondary_voltage, 0.48)
        self.assertEqual(loaded.get("LN-001").conductor, "336 ACSR")
        self.assertEqual(loaded.get("LOAD-001").kw, 310.0)
        self.assertIs(loaded.get("TIE-001").is_tie, True)
        self.assertIs(loaded.get("SW-001").is_tie, False)

    def test_device_classes_survive(self):
        save_network(self.network, self.path)
        loaded = load_network(self.path)
        self.assertEqual(type(loaded.get("REC-001")).__name__, "Recloser")
        self.assertEqual(type(loaded.get("BRK-001")).__name__, "Breaker")
        self.assertEqual(loaded.get("REC-001").CIM_CLASS, "ProtectedSwitch")

    def test_extras_round_trip_as_json(self):
        network = Network()
        feeder = network.add_feeder("FDR-9", voltage="12.47kV", extras={"owner": "East"})
        feeder.add_breaker("BRK-9", extras={"install_year": 1998, "notes": "rebuilt"})
        save_network(network, self.path)
        loaded = load_network(self.path)
        self.assertEqual(loaded.get("BRK-9").extras, {"install_year": 1998, "notes": "rebuilt"})
        self.assertEqual(loaded.get("FDR-9").extras, {"owner": "East"})
        self.assertEqual(execute(loaded, "FIND breakers WHERE install_year < 2000").mrids,
                         ["BRK-9"])

    def test_an_empty_network_round_trips(self):
        save_network(Network(), self.path)
        self.assertEqual(len(load_network(self.path)), 0)

    def test_saving_twice_replaces_rather_than_duplicates(self):
        save_network(self.network, self.path)
        save_network(self.network, self.path)
        self.assertEqual(object_counts(self.path),
                         {"substations": 1, "feeders": 1, "devices": 11, "connections": 10})

    def test_a_modified_network_overwrites_cleanly(self):
        save_network(self.network, self.path)
        smaller = Network()
        smaller.add_feeder("FDR-2").add_breaker("BRK-2")
        save_network(smaller, self.path)
        loaded = load_network(self.path)
        self.assertEqual(sorted(loaded.objects), ["BRK-2", "FDR-2"])


class SchemaTests(StorageTestCase):
    def test_each_edge_is_stored_once(self):
        save_network(self.network, self.path)
        with open_database(self.path) as connection:
            rows = connection.execute("SELECT from_device, to_device FROM connections").fetchall()
        self.assertEqual(len(rows), 10)
        for row in rows:
            self.assertLess(row["from_device"], row["to_device"])

    def test_mirrored_edges_are_rejected_by_the_schema(self):
        save_network(self.network, self.path)
        with self.assertRaises(sqlite3.IntegrityError), open_database(self.path) as connection:
            connection.execute(
                "INSERT INTO connections(from_device, to_device) VALUES ('SW-001', 'REC-001')"
            )

    def test_extension_rows_line_up_with_their_devices(self):
        save_network(self.network, self.path)
        with open_database(self.path) as connection:
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("switches", "transformers", "lines", "loads", "capacitors")
            }
        self.assertEqual(counts,
                         {"switches": 5, "transformers": 2, "lines": 2, "loads": 2,
                          "capacitors": 0})

    def test_the_schema_records_its_version(self):
        save_network(self.network, self.path)
        with open_database(self.path) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        self.assertEqual(row["value"], SCHEMA_VERSION)

    def test_an_existing_connection_can_be_used(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        save_network(self.network, connection)
        self.assertEqual(len(load_network(connection).devices), 11)
        connection.close()


class FailureTests(StorageTestCase):
    def test_a_missing_database(self):
        with self.assertRaises(StorageError):
            load_network(self.directory / "nope.sqlite")

    def test_a_file_that_is_not_a_gridql_database(self):
        path = self.directory / "random.sqlite"
        other = sqlite3.connect(path)
        other.execute("CREATE TABLE junk (a INT)")
        other.close()
        with self.assertRaises(StorageError):
            load_network(path)

    def test_counting_a_missing_database_neither_creates_it_nor_leaks_sqlite(self):
        path = self.directory / "nope.sqlite"
        with self.assertRaises(StorageError):
            object_counts(path)
        self.assertFalse(path.exists())

    def test_counting_a_file_that_is_not_a_gridql_database(self):
        path = self.directory / "random.sqlite"
        sqlite3.connect(path).close()
        with self.assertRaises(StorageError):
            object_counts(path)

    def test_a_dangling_container_is_named_rather_than_a_bare_constraint(self):
        self.network.get("FDR-104").substation = "SUB-GHOST"
        with self.assertRaises(StorageError) as raised:
            save_network(self.network, self.path)
        self.assertIn("SUB-GHOST", str(raised.exception))
        self.assertNotIn("FOREIGN KEY", str(raised.exception))

    def test_a_self_connection_is_named_rather_than_a_bare_constraint(self):
        self.network.connect("BRK-001", "BRK-001")
        with self.assertRaises(StorageError) as raised:
            save_network(self.network, self.path)
        self.assertIn("BRK-001 is connected to itself", str(raised.exception))

    def test_a_connection_to_a_container_is_named(self):
        self.network.connect("BRK-001", "SUB-001")
        with self.assertRaises(StorageError) as raised:
            save_network(self.network, self.path)
        self.assertIn("SUB-001 is not equipment", str(raised.exception))

    def test_a_refused_save_leaves_the_existing_database_alone(self):
        save_network(self.network, self.path)
        self.network.connect("BRK-001", "BRK-001")
        with self.assertRaises(StorageError):
            save_network(self.network, self.path)
        self.assertEqual(len(load_network(self.path).devices), 11)

    def test_a_schema_version_this_build_does_not_know(self):
        save_network(self.network, self.path)
        with open_database(self.path) as connection:
            connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        with self.assertRaises(StorageError) as raised:
            load_network(self.path)
        self.assertIn("99", str(raised.exception))

    def test_an_unknown_device_type(self):
        save_network(self.network, self.path)
        with open_database(self.path) as connection:
            connection.execute("UPDATE devices SET device_type = 'flux_capacitor' "
                               "WHERE mrid = 'REC-001'")
        with self.assertRaises(StorageError) as raised:
            load_network(self.path)
        self.assertIn("flux_capacitor", str(raised.exception))

    def test_a_loaded_feeder_will_not_silently_chain_a_new_device(self):
        save_network(self.network, self.path)
        feeder = load_network(self.path).feeders[0]
        with self.assertRaises(RuntimeError) as raised:
            feeder.add_switch("SW-NEW")
        self.assertIn("after=", str(raised.exception))

    def test_a_loaded_feeder_can_be_extended_explicitly(self):
        save_network(self.network, self.path)
        loaded = load_network(self.path)
        loaded.feeders[0].add_switch("SW-NEW", after="REC-001")
        self.assertIn("SW-NEW", [o.mrid for o in loaded.downstream_of("REC-001")])


class CommandLineTests(StorageTestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_init_then_query(self):
        code, out, _ = self.run_cli(["init", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("11 devices", out)

        code, out, _ = self.run_cli(["--db", str(self.path), "FIND reclosers"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", out)

    def test_init_refuses_to_clobber_without_force(self):
        self.run_cli(["init", str(self.path)])
        code, _, err = self.run_cli(["init", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("--force", err)

    def test_init_force_overwrites(self):
        self.run_cli(["init", str(self.path)])
        code, out, _ = self.run_cli(["init", str(self.path), "--force"])
        self.assertEqual(code, 0)
        self.assertIn("11 devices", out)

    def test_init_empty(self):
        code, out, _ = self.run_cli(["init", str(self.path), "--empty"])
        self.assertEqual(code, 0)
        self.assertIn("empty schema", out)
        self.assertEqual(len(load_network(self.path)), 0)

    def test_run_a_gridql_file_against_a_database(self):
        self.run_cli(["init", str(self.path)])
        query = Path(__file__).resolve().parent.parent / "queries" / "open_devices.gridql"
        code, out, _ = self.run_cli(["run", str(query), "--db", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("SW-002", out)

    def test_querying_a_missing_database_exits_non_zero(self):
        code, _, err = self.run_cli(["--db", str(self.directory / "nope.sqlite"), "FIND feeders"])
        self.assertEqual(code, 1)
        self.assertIn("no such database", err)


class RefreshTests(StorageTestCase):
    """Replacing a database with a new import: a refresh must not lose data by accident."""

    def setUp(self):
        super().setUp()
        save_network(self.network, self.path)
        self.export = self.directory / "export"
        write_csv(self.network, self.export)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def refresh(self, *flags):
        return self.run_cli(
            ["import-csv", str(self.export), "--db", str(self.path), "--force", "--no-config", *flags]
        )

    def devices_stored(self):
        return len(load_network(self.path).devices)

    def keep_rows(self, predicate):
        """Rewrite the export's devices.csv keeping only some rows."""
        path = self.export / "devices.csv"
        header, *rows = path.read_text().splitlines()
        path.write_text("\n".join([header, *(row for row in rows if predicate(row))]) + "\n")

    def test_a_like_for_like_refresh_says_what_it_replaced(self):
        code, out, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertIn("(was 11 devices, now 11)", out)

    def test_a_truncated_export_is_refused_and_the_database_kept(self):
        self.keep_rows(lambda row: row.startswith(("BRK", "LN-001")))
        code, out, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertIn("replace 11 devices with 2, 82% fewer", err)
        self.assertIn("--skip-checks", err)
        self.assertIn("loaded 2 devices", out)  # the report still shows what was read
        self.assertEqual(self.devices_stored(), 11)

    def test_a_small_change_is_an_ordinary_refresh(self):
        self.keep_rows(lambda row: not row.startswith("LOAD-002"))  # one device of 11
        code, out, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertIn("(was 11 devices, now 10)", out)

    def test_a_missing_feeder_is_refused_even_when_the_count_holds(self):
        extra = Network()
        feeder = extra.add_feeder("FDR-200")
        feeder.add_breaker("BRK-200")
        network = build_sample_network()
        network.add_feeder("FDR-200")
        network.add(dataclasses.replace(extra.get("BRK-200"), feeder="FDR-200"))
        save_network(network, self.path)  # the database knows two feeders; the export one
        code, _, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertIn("1 feeder in the database is not in the new data: FDR-200", err)
        self.assertEqual(len(load_network(self.path).feeders), 2)

    def test_validation_errors_are_refused(self):
        path = self.export / "devices.csv"
        path.write_text(path.read_text().replace("OPEN,OPEN,true", "AJAR,OPEN,true"))
        code, _, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertIn("validation found 1 error: invalid-state", err)

    def test_skip_checks_replaces_it_anyway(self):
        self.keep_rows(lambda row: row.startswith("BRK"))
        code, out, _ = self.refresh("--skip-checks")
        self.assertEqual(code, 0)
        self.assertEqual(self.devices_stored(), 1)

    def test_a_new_database_has_nothing_to_protect(self):
        self.path.unlink()
        self.keep_rows(lambda row: row.startswith("BRK"))
        code, _, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(self.devices_stored(), 1)

    def test_a_file_that_cannot_be_compared_against_is_refused(self):
        self.path.unlink()
        sqlite3.connect(self.path).close()  # an empty file, not a GridQL database
        code, _, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertIn("could not be read to compare against", err)

    def test_the_validation_hint_points_at_the_new_data_not_the_database(self):
        # After a refused save the database still holds the old network.
        path = self.export / "devices.csv"
        path.write_text(path.read_text().replace("OPEN,OPEN,true", "AJAR,OPEN,true"))
        _, out, _ = self.refresh()
        self.assertIn(f"gridql validate --csv {self.export}", out)
        self.assertNotIn("validate --db", out)

    def test_import_cim_is_guarded_the_same_way(self):
        from gridql.cim import export_network

        xml = self.directory / "slice.xml"
        lateral = build_sample_network()
        export_network(lateral, execute(lateral, 'FIND devices FED BY "SW-002"').objects, xml)
        code, _, err = self.run_cli(["import-cim", str(xml), "--db", str(self.path), "--force"])
        self.assertEqual(code, 1)
        self.assertIn("replace 11 devices with", err)
        code, _, _ = self.run_cli(
            ["import-cim", str(xml), "--db", str(self.path), "--force", "--skip-checks"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.devices_stored(), 3)


if __name__ == "__main__":
    unittest.main()
