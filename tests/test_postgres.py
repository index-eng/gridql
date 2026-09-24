"""Reading a network from Postgres, through a mapping.

The tests that need a server start a throwaway cluster of their own when
Postgres is installed (initdb and pg_ctl on the PATH), or use the server
GRIDQL_TEST_POSTGRES names -- a connection string for a role that may create
databases, on a server that trusts it without a password (some tests connect
with a made-up one to check it is never printed). Without either they are
skipped; the rest need no server.
"""

import contextlib
import dataclasses
import io
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from gridql import execute
from gridql.cli import main
from gridql.config import CONFIG_NAME, load_config
from gridql.errors import GridQLError
from gridql.ingest import CsvError, PostgresError, read_csv, read_postgres
from gridql.ingest.mapping import MappingError, parse_mapping
from gridql.ingest.postgres import _text, has_password, redact

try:
    import psycopg
    from psycopg.conninfo import make_conninfo
except ImportError:
    psycopg = None

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SQL = ROOT / "examples" / "postgres" / "cedar_hill.sql"
EXAMPLE_MAPPING = ROOT / "examples" / "postgres" / "mapping.toml"
CSV_EXAMPLE = ROOT / "examples" / "mapped"

#: A connection string for the server, or None when there is none to use.
SERVER = None


def setUpModule():
    global SERVER
    unittest.enterModuleContext(sample_project())
    SERVER = unittest.enterModuleContext(postgres_server())


@contextlib.contextmanager
def postgres_server():
    """The server named by GRIDQL_TEST_POSTGRES, or a throwaway one, or None."""
    given = os.environ.get("GRIDQL_TEST_POSTGRES")
    if given or psycopg is None:
        yield given if psycopg is not None else None
        return
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        yield None
        return

    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data"
        # On macOS the postmaster refuses to start without a locale set.
        env = {**os.environ, "LC_ALL": "C"}
        port = _free_port()
        try:
            subprocess.run(
                [initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                check=True, capture_output=True, env=env,
            )
            subprocess.run(
                [
                    pg_ctl, "-D", str(data), "-l", str(Path(directory) / "log"), "-w",
                    "-o", f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''",
                    "start",
                ],
                check=True, capture_output=True, env=env,
            )
        except (OSError, subprocess.CalledProcessError):
            yield None
            return
        try:
            yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                capture_output=True, env=env,
            )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def snapshot(network):
    return (
        {o.mrid: (type(o).__name__, dataclasses.asdict(o)) for o in network.objects.values()},
        {mrid: sorted(network.neighbors(mrid)) for mrid in network.objects},
        {feeder.mrid: feeder.head for feeder in network.feeders},
    )


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ServerTestCase(unittest.TestCase):
    """Each test class gets a database of its own on the server."""

    #: SQL to set the database up with, run once for the class.
    SETUP_SQL: str | None = None

    @classmethod
    def setUpClass(cls):
        if SERVER is None:
            raise unittest.SkipTest(
                "no Postgres server: install Postgres, or set GRIDQL_TEST_POSTGRES"
            )
        name = f"gridql_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(SERVER, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{name}"')
        cls.addClassCleanup(cls._drop, name)
        cls.database = make_conninfo(SERVER, dbname=name)
        if cls.SETUP_SQL:
            cls.sql(cls.SETUP_SQL)

    @staticmethod
    def _drop(name):
        with psycopg.connect(SERVER, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    @classmethod
    def sql(cls, text):
        with psycopg.connect(cls.database, autocommit=True) as connection:
            connection.execute(text)

    def read(self, toml: str):
        return read_postgres(self.database, parse_mapping(_toml(toml)))

    def refused(self, toml: str) -> str:
        with self.assertRaises(GridQLError) as caught:
            self.read(toml)
        return str(caught.exception)


def _toml(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


# -- against a server ----------------------------------------------------


class ExampleTests(ServerTestCase):
    SETUP_SQL = EXAMPLE_SQL.read_text(encoding="utf-8")

    def test_the_example_reads_exactly_as_its_csv_export_does(self):
        from_database = read_postgres(self.database, EXAMPLE_MAPPING)
        from_files = read_csv(CSV_EXAMPLE, CSV_EXAMPLE / "mapping.toml")
        self.assertEqual(snapshot(from_database.network), snapshot(from_files.network))
        self.assertEqual(from_database.report.problems, [])

    def test_a_query_section_joins_in_what_another_system_knows(self):
        network = read_postgres(self.database, EXAMPLE_MAPPING).network
        self.assertEqual(network.objects["SP-40214"].extras["customer_count"], 8)
        dead = execute(network, "FIND loads WHERE NOT energized SELECT SUM(customer_count)")
        self.assertEqual(dead.rows(), [{"SUM(customer_count)": 9}])

    def test_the_report_names_the_database_and_never_the_password(self):
        with_password = make_conninfo(self.database, password="hunter2")
        document = read_postgres(with_password, EXAMPLE_MAPPING)
        dbname = psycopg.conninfo.conninfo_to_dict(self.database)["dbname"]
        self.assertIn(f"Postgres database {dbname} on 127.0.0.1", document.network.source)
        self.assertNotIn("hunter2", document.network.source)

    def test_nothing_is_written_to_the_database(self):
        # nextval() writes, so a read-only transaction refuses it.
        message = self.refused(
            '[[devices]]\nquery = "SELECT *, nextval(\'cis.premise_premise_no_seq\') AS n '
            'FROM gis.switch"\nmrid = "facility_id"\n'
        )
        self.assertIn("read-only transaction", message)

        self.refused(
            '[[devices]]\nquery = "WITH gone AS (DELETE FROM gis.fuse RETURNING *) '
            'SELECT * FROM gone"\nmrid = "facility_id"\n'
        )
        message = self.refused('[[devices]]\nquery = "DELETE FROM gis.fuse"\nmrid = "facility_id"\n')
        self.assertIn("a query must be a single SELECT", message)
        with psycopg.connect(self.database) as connection:
            count = connection.execute("SELECT count(*) FROM gis.fuse").fetchone()[0]
        self.assertEqual(count, 8)

    def test_import_postgres_saves_what_it_read_and_guards_a_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "grid.sqlite")
            argv = [
                "import-postgres", self.database, "--mapping", str(EXAMPLE_MAPPING), "--db", db,
                "--color", "never",
            ]
            code, out, err = run_cli(argv)
            self.assertEqual(code, 0, err)
            self.assertIn("read Postgres database", out)
            self.assertIn("loaded 77 devices, 2 feeders", out)
            self.assertIn("validation: no problems found", out)
            self.assertIn(f"saved to {db}", out)

            code, _, err = run_cli(argv)
            self.assertEqual(code, 1)
            self.assertIn("already exists; pass --force", err)

            code, out, err = run_cli([*argv, "--force"])
            self.assertEqual(code, 0, err)
            self.assertIn("(was 77 devices, now 77)", out)

            code, out, _ = run_cli(["--db", db, "FIND reclosers", "--format", "csv"])
            self.assertEqual(code, 0)
            self.assertIn("REC-1201-01", out)

    def test_import_postgres_takes_the_database_and_mapping_from_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / CONFIG_NAME
            config.write_text(
                f'postgres = "{self.database}"\nmapping = "{EXAMPLE_MAPPING.as_posix()}"\n',
                encoding="utf-8",
            )
            code, out, err = run_cli(["import-postgres", "--config", str(config)])
        self.assertEqual(code, 0, err)
        self.assertIn("loaded 77 devices", out)
        self.assertIn("not saved: pass --db PATH", out)


class TableTests(ServerTestCase):
    SETUP_SQL = """
        CREATE SCHEMA gis;
        CREATE TABLE gis.switch (
            facility_id text PRIMARY KEY,
            sw_type     text,
            tie_flag    boolean,
            op_kv       numeric(5,2),
            installed   date,
            owner       text,
            shape       bytea,
            tags        text[],
            attributes  jsonb
        );
        INSERT INTO gis.switch VALUES
            ('SW-2', 'LBS', true, 12.47, '2004-06-01', NULL, '\\x0101', '{a,b}', '{"x": 1}'),
            ('SW-1', 'RCL', false, 4.16, NULL, 'CO-OP', NULL, NULL, NULL),
            ('SW-3', 'LBS', NULL, NULL, NULL, NULL, NULL, NULL, NULL);

        CREATE TABLE gis."Fuse" (fuse_id text PRIMARY KEY, circuit text);
        INSERT INTO gis."Fuse" VALUES ('FU-1', '12');

        CREATE TABLE public.twins (id text);
        CREATE TABLE public."TWINS" (id text);

        -- No primary key, and two rows with one mRID.
        CREATE TABLE gis.loose (mrid text, name text);
        INSERT INTO gis.loose VALUES ('LD-1', 'first'), (NULL, 'unnamed'), ('LD-1', 'second');

        -- Two with one mRID in a keyed table: the lower key wins.
        CREATE TABLE gis.keyed (objectid integer PRIMARY KEY, mrid text, name text);
        INSERT INTO gis.keyed VALUES (20, 'LD-9', 'later'), (10, 'LD-9', 'earlier'),
                                     (30, NULL, 'unnamed');
    """

    SWITCHES = """
        [[devices]]
        table        = "gis.switch"
        mrid         = "facility_id"
        type         = { column = "sw_type", values = { RCL = "recloser", LBS = "switch" } }
        is_tie       = "tie_flag"
        voltage      = { column = "op_kv", unit = "kV" }
        state        = { value = "CLOSED" }
    """

    def test_typed_values_are_read_as_the_loader_reads_text(self):
        network = self.read(self.SWITCHES).network
        tie, recloser = network.objects["SW-2"], network.objects["SW-1"]
        self.assertTrue(tie.is_tie)
        self.assertFalse(recloser.is_tie)
        self.assertEqual(type(recloser).__name__, "Recloser")
        self.assertAlmostEqual(tie.voltage, 12.47)
        self.assertEqual(tie.extras["installed"], "2004-06-01")
        self.assertEqual(recloser.extras["owner"], "CO-OP")
        self.assertNotIn("owner", tie.extras)  # NULL is an empty cell

    def test_columns_holding_no_plain_value_are_not_kept(self):
        document = self.read(self.SWITCHES)
        extras = document.network.objects["SW-2"].extras
        for column in ("shape", "tags", "attributes"):
            self.assertNotIn(column, extras)
        self.assertIn(
            "gis.switch: not kept as attributes, as they hold no plain value "
            "(name them in extras to keep them as text): shape (bytea), tags (text[]), "
            "attributes (jsonb)",
            document.report.notes,
        )

    def test_extras_can_keep_them_as_text_anyway(self):
        network = self.read(self.SWITCHES + '        extras = ["shape", "attributes"]\n').network
        self.assertEqual(network.objects["SW-2"].extras["shape"], "0101")
        self.assertEqual(network.objects["SW-2"].extras["attributes"], '{"x": 1}')
        self.assertNotIn("installed", network.objects["SW-2"].extras)

    def test_table_names_match_whatever_their_case(self):
        toml = '[[devices]]\ntable = "GIS.FUSE"\nmrid = "FUSE_ID"\ntype = { value = "fuse" }\n'
        self.assertIn("FU-1", self.read(toml).network.objects)

    def test_an_unqualified_name_is_found_on_the_search_path(self):
        self.sql("CREATE TABLE public.pole (pole_id text); INSERT INTO public.pole VALUES ('P-1')")
        self.addCleanup(self.sql, "DROP TABLE public.pole")
        self.assertIn("P-1", self.read('[[devices]]\ntable = "pole"\nmrid = "pole_id"\n').network.objects)

    def test_a_table_off_the_search_path_is_pointed_at(self):
        message = self.refused('[[devices]]\ntable = "switch"\nmrid = "facility_id"\n')
        self.assertIn("no table or view 'switch'", message)
        self.assertIn("not on the search path (did you mean gis.switch?)", message)

    def test_a_misspelt_table_gets_a_suggestion(self):
        message = self.refused('[[devices]]\ntable = "gis.swtich"\nmrid = "facility_id"\n')
        self.assertIn("no table or view 'gis.swtich'", message)
        self.assertIn("did you mean gis.switch?", message)

    def test_names_differing_only_in_case_must_be_written_exactly(self):
        message = self.refused('[[devices]]\ntable = "Twins"\nmrid = "id"\n')
        self.assertIn('matches "public"."TWINS", "public"."twins"', message)
        self.read('[[devices]]\ntable = "TWINS"\nmrid = "id"\n')

    def test_a_missing_column_says_what_the_table_has(self):
        message = self.refused('[[devices]]\ntable = "gis.switch"\nmrid = "facilty_id"\n')
        self.assertIn("[devices] gis.switch: no column 'facilty_id' for mrid", message)
        self.assertIn("(did you mean facility_id?)", message)
        self.assertIn("The table has: facility_id, sw_type", message)

    def test_rows_are_read_in_key_order_and_named_by_key(self):
        document = self.read('[[devices]]\ntable = "gis.keyed"\nmrid = "mrid"\nname = "name"\n')
        self.assertEqual(document.network.objects["LD-9"].name, "earlier")
        self.assertEqual(
            document.report.problems,
            ["gis.keyed (objectid 20): duplicate mRID 'LD-9'", "gis.keyed (objectid 30): no mRID"],
        )

    def test_rows_without_a_key_are_named_by_their_place(self):
        document = self.read('[[devices]]\ntable = "gis.loose"\nmrid = "mrid"\n')
        self.assertIn("gis.loose (row 2): no mRID", document.report.problems)

    def test_a_query_may_end_in_a_semicolon(self):
        toml = '[[devices]]\nquery = "SELECT * FROM gis.switch WHERE sw_type = \'RCL\';"\nmrid = "facility_id"\n'
        self.assertEqual(list(self.read(toml).network.objects), ["SW-1"])

    def test_a_query_with_two_columns_of_one_name_is_refused(self):
        message = self.refused(
            '[[devices]]\nquery = "SELECT s.facility_id, f.fuse_id AS FACILITY_ID '
            'FROM gis.switch s, gis.\\"Fuse\\" f"\nmrid = "facility_id"\n'
        )
        self.assertIn("two columns are named 'facility_id'", message)

    def test_a_broken_query_reports_the_servers_words(self):
        message = self.refused(
            '[[devices]]\nquery = "SELECT facility_idd FROM gis.switch"\nmrid = "facility_id"\n'
        )
        self.assertIn("[devices] query (SELECT facility_idd FROM gis.switch)", message)
        self.assertIn('column "facility_idd" does not exist', message)


class LiveQueryTests(ServerTestCase):
    SETUP_SQL = EXAMPLE_SQL.read_text(encoding="utf-8")

    OPEN_FUSES = "FIND fuses WHERE state = OPEN"

    def live(self, *argv):
        return run_cli(
            [*argv, "--postgres", self.database, "--mapping", str(EXAMPLE_MAPPING),
             "--format", "csv", "--no-config"]
        )

    def close_the_open_fuse(self):
        self.sql("UPDATE gis.fuse SET position = 'C' WHERE facility_id = 'FU-1201-04'")
        self.addCleanup(
            self.sql, "UPDATE gis.fuse SET position = 'O' WHERE facility_id = 'FU-1201-04'"
        )

    def project(self, **settings) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / CONFIG_NAME
        settings.setdefault("mapping", EXAMPLE_MAPPING.as_posix())
        path.write_text(
            "".join(f'{key} = "{value}"\n' for key, value in settings.items()), encoding="utf-8"
        )
        return path

    def test_a_query_reads_the_database_as_it_is_now(self):
        code, out, err = self.live(self.OPEN_FUSES)
        self.assertEqual(code, 0, err)
        self.assertIn("FU-1201-04", out)

        self.close_the_open_fuse()
        code, out, _ = self.live(self.OPEN_FUSES)
        self.assertEqual(code, 0)
        self.assertNotIn("FU-1201-04", out)

    def test_a_project_naming_only_postgres_is_queried_live(self):
        config = self.project(postgres=self.database)
        code, out, err = run_cli([self.OPEN_FUSES, "--config", str(config), "--format", "csv"])
        self.assertEqual(code, 0, err)
        self.assertIn("FU-1201-04", out)

        code, out, _ = run_cli(["config", "--config", str(config)])
        self.assertIn("(Postgres, read live at each query)", out)

    def test_live_passes_over_the_projects_snapshot(self):
        db = str(self.project().parent / "grid.sqlite")
        code, _, err = run_cli(
            ["import-postgres", self.database, "--mapping", str(EXAMPLE_MAPPING), "--db", db]
        )
        self.assertEqual(code, 0, err)
        config = self.project(postgres=self.database, db=db)
        self.close_the_open_fuse()

        _, snapshot_answer, _ = run_cli([self.OPEN_FUSES, "--config", str(config), "-f", "csv"])
        self.assertIn("FU-1201-04", snapshot_answer)
        code, live_answer, err = run_cli(
            [self.OPEN_FUSES, "--config", str(config), "-f", "csv", "--live"]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("FU-1201-04", live_answer)

    def test_run_validate_and_export_read_live_too(self):
        code, out, err = run_cli(
            ["run", str(ROOT / "queries" / "feeder_report.gridql"), "--feeder", "FDR-1202",
             "--postgres", self.database, "--mapping", str(EXAMPLE_MAPPING), "--no-config"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("TX-50214  Pad 50214   150", out)

        code, out, err = run_cli(
            ["validate", "--postgres", self.database, "--mapping", str(EXAMPLE_MAPPING),
             "--no-config", "--color", "never"]
        )
        self.assertEqual(code, 0, err)

        code, out, err = run_cli(
            ["export-cim", "-", "--query", 'FIND devices FED BY "FDR-1202"',
             "--postgres", self.database, "--mapping", str(EXAMPLE_MAPPING), "--no-config"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("BKR-1202", out)
        self.assertNotIn("BKR-1201", out)

    def test_the_repl_reloads_what_the_database_holds_now(self):
        answers = []

        def typed(prompt):
            answers.append(prompt)
            step = len(answers)
            if step == 2:
                self.close_the_open_fuse()
            return [self.OPEN_FUSES, ".reload", self.OPEN_FUSES, ".quit"][step - 1]

        with mock.patch("builtins.input", typed):
            code, out, err = run_cli(
                ["--postgres", self.database, "--mapping", str(EXAMPLE_MAPPING), "--no-config",
                 "--format", "csv", "--color", "never"]
            )
        self.assertEqual(code, 0, err)
        first, reloaded = out.split("reloaded:")
        self.assertIn("Loaded: Postgres database", out)
        self.assertIn("FU-1201-04", first)
        self.assertIn(" 77 devices, 2 feeders", reloaded)
        self.assertNotIn("FU-1201-04", reloaded)

    def test_a_bad_load_warns_and_points_at_the_import_without_the_password(self):
        # A transformer with a fuse's mRID: the second of the two is skipped.
        self.sql("INSERT INTO gis.transformer (facility_id, circuit_no) VALUES ('FU-1201-01', 1201)")
        self.addCleanup(self.sql, "DELETE FROM gis.transformer WHERE facility_id = 'FU-1201-01'")
        with_password = make_conninfo(self.database, password="hunter2")
        code, out, err = run_cli(
            ["FIND fuses", "--postgres", with_password, "--mapping", str(EXAMPLE_MAPPING),
             "--no-config", "--color", "never"]
        )
        self.assertEqual(code, 0)
        self.assertIn("warning: 1 row skipped or incomplete in Postgres database", err)
        self.assertIn("gis.transformer (facility_id FU-1201-01): duplicate mRID", err)
        self.assertIn("gridql import-postgres", err)
        self.assertNotIn("hunter2", err)


# -- without a server ----------------------------------------------------


class MappingTests(unittest.TestCase):
    def test_a_section_reads_a_table_or_a_query(self):
        mapping = parse_mapping(
            {"devices": [{"table": "gis.switch", "mrid": "ID"}, {"query": "SELECT 1", "mrid": "ID"}]}
        )
        table, query = mapping.of("devices")
        self.assertEqual((table.form, table.source, table.label), ("table", "gis.switch", "gis.switch"))
        self.assertEqual((query.form, query.label), ("query", "query (SELECT 1)"))
        self.assertTrue(mapping.reads_database)

    def test_a_long_query_is_named_by_its_opening_words(self):
        mapping = parse_mapping(
            {"devices": {"query": "SELECT *\n  FROM gis.transformer\n  WHERE status = 'IN SERVICE'", "mrid": "ID"}}
        )
        self.assertEqual(
            mapping.of("devices")[0].label, "query (SELECT * FROM gis.transformer WHERE...)"
        )

    def test_a_section_reads_from_exactly_one_place(self):
        with self.assertRaises(MappingError) as caught:
            parse_mapping({"devices": {"file": "SW.csv", "table": "gis.switch", "mrid": "ID"}})
        self.assertIn("sets file and table", str(caught.exception))

        with self.assertRaises(MappingError) as caught:
            parse_mapping({"devices": {"mrid": "ID"}})
        self.assertIn('table = "<schema.table>"', str(caught.exception))

        with self.assertRaises(MappingError) as caught:
            parse_mapping({"devices": {"query": "  ", "mrid": "ID"}})
        self.assertIn("query must be non-empty text", str(caught.exception))

    def test_a_mapping_reads_files_or_a_database_not_both(self):
        with self.assertRaises(MappingError) as caught:
            parse_mapping(
                {"feeders": {"file": "CKT.csv", "mrid": "ID"}, "devices": {"table": "sw", "mrid": "ID"}}
            )
        self.assertIn("either CSV files or a database, not both", str(caught.exception))

    def test_each_reader_refuses_the_others_mapping(self):
        with self.assertRaises(CsvError) as caught:
            read_csv(CSV_EXAMPLE, EXAMPLE_MAPPING)
        self.assertIn("names database tables, not files; read it with 'gridql import-postgres'", str(caught.exception))

        with self.assertRaises(PostgresError) as caught:
            read_postgres("", CSV_EXAMPLE / "mapping.toml")
        self.assertIn("names CSV files, not tables", str(caught.exception))


class ValueTests(unittest.TestCase):
    def test_values_are_written_as_a_csv_cell_would_hold_them(self):
        self.assertEqual(_text(None), "")
        self.assertEqual(_text(True), "true")
        self.assertEqual(_text(12470), "12470")
        self.assertEqual(_text(Decimal("37.50")), "37.50")
        self.assertEqual(_text(Decimal("1E+2")), "100")
        self.assertEqual(_text(0.1), "0.1")
        self.assertEqual(_text(1e16), "10000000000000000")
        self.assertEqual(_text(date(2004, 6, 1)), "2004-06-01")
        self.assertEqual(_text(b"\x01\x02"), "0102")
        self.assertEqual(_text({"x": 1}), '{"x": 1}')

    def test_passwords_are_redacted_from_connection_strings(self):
        self.assertEqual(redact("postgresql://gis:s3cret@gis-db:5432/utility"), "postgresql://gis:***@gis-db:5432/utility")
        self.assertEqual(redact("postgresql://gis@gis-db/utility?password=s3cret&sslmode=require"), "postgresql://gis@gis-db/utility?password=***&sslmode=require")
        self.assertEqual(redact("host=gis-db password='it\\'s secret' user=gis"), "host=gis-db password=*** user=gis")
        self.assertEqual(redact("service=gis"), "service=gis")
        self.assertTrue(has_password("host=gis-db password=x"))
        self.assertFalse(has_password("postgresql://gis@gis-db/utility"))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_import_postgres_asks_which_database(self):
        code, _, err = run_cli(["import-postgres", "--mapping", str(EXAMPLE_MAPPING), "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("which database?", err)
        self.assertIn("PGHOST, PGDATABASE, PGSERVICE", err)

    def test_import_postgres_needs_a_mapping(self):
        code, _, err = run_cli(["import-postgres", "postgresql://gis@gis-db/utility", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("a Postgres import needs --mapping", err)

    @unittest.skipIf(psycopg is None, "psycopg is not installed")
    def test_a_server_that_is_not_there_is_reported(self):
        code, _, err = run_cli(
            ["import-postgres", f"postgresql://gis@127.0.0.1:{_free_port()}/utility?connect_timeout=5",
             "--mapping", str(EXAMPLE_MAPPING), "--no-config"]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot connect to Postgres", err)

    def test_live_needs_a_project_naming_a_database(self):
        code, _, err = run_cli(["FIND reclosers", "--live", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("--live reads the database a project names", err)
        self.assertIn("name one with --postgres", err)

    def test_postgres_is_one_dataset_among_the_others(self):
        code, _, err = run_cli(
            ["FIND reclosers", "--db", "grid.sqlite", "--postgres", "service=gis", "--no-config"]
        )
        self.assertEqual(code, 1)
        self.assertIn("pass either --db or --postgres, not both", err)

    def test_querying_postgres_needs_a_mapping(self):
        code, _, err = run_cli(["FIND reclosers", "--postgres", "service=gis", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("reading Postgres needs --mapping", err)

    def test_a_query_given_in_place_of_the_database_is_caught(self):
        code, _, err = run_cli(["--postgres", "FIND reclosers", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("--postgres takes a database to connect to, but was given the query", err)

    def test_the_config_names_the_database_without_its_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / CONFIG_NAME
            path.write_text('postgres = "postgresql://gis:s3cret@gis-db/utility"\n', encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.postgres, "postgresql://gis:s3cret@gis-db/utility")
            described = config.describe()
            self.assertIn(
                "data:    postgresql://gis:***@gis-db/utility (Postgres, read live at each query)",
                described,
            )
            self.assertIn("~/.pgpass or PGPASSWORD keeps it out", described)
            self.assertNotIn("s3cret", described)

            path.write_text('postgres = "service=gis"\ndb = "grid.sqlite"\n', encoding="utf-8")
            described = load_config(path).describe()
            self.assertIn("data:    ", described)
            self.assertIn("grid.sqlite", described)
            self.assertIn("import:  service=gis (--live to query it directly)", described)

            path.write_text("postgres = 5432\n", encoding="utf-8")
            with self.assertRaises(GridQLError) as caught:
                load_config(path)
            self.assertIn("postgres must be a string", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
