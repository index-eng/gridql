"""project.gridqlconfig: the dataset, the queries and the defaults of a project."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from gridql.cli import main, network_for, source_of
from gridql.config import (
    CONFIG_NAME,
    Config,
    find_config,
    load_config,
    project_config,
    resolve_script,
)
from gridql.errors import GridQLError
from gridql.storage import save_network
from gridql.data import build_sample_network


class ProjectTests(unittest.TestCase):
    """Each test gets a project directory and runs inside it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

        (self.root / "queries").mkdir()
        self.write_query("feeder_report", 'PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        save_network(build_sample_network(), str(self.root / "grid.sqlite"))

    def write_config(self, text):
        (self.root / CONFIG_NAME).write_text(text)
        return self.root / CONFIG_NAME

    def write_query(self, name, text):
        path = self.root / "queries" / f"{name}.gridql"
        path.write_text(text)
        return path


class LoadingTests(ProjectTests):
    def test_a_config_is_found_from_a_subdirectory(self):
        path = self.write_config('db = "grid.sqlite"\n')
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(find_config(deep), path)

    def test_no_config_gives_an_empty_one(self):
        config = project_config()
        self.assertFalse(config)
        self.assertIsNone(config.db)
        self.assertIn("no project.gridqlconfig found", config.describe())

    def test_paths_resolve_against_the_config_not_the_caller(self):
        self.write_config('db = "grid.sqlite"\nqueries = "queries"\n')
        config = project_config(start=self.root / "queries")
        self.assertEqual(Path(config.db), self.root / "grid.sqlite")
        self.assertEqual(Path(config.queries), self.root / "queries")

    def test_params_are_read_as_a_table(self):
        self.write_config('[params]\nfeeder = "FDR-104"\nmin_kva = 500\n')
        self.assertEqual(project_config().params, {"feeder": "FDR-104", "min_kva": 500})

    def test_no_config_flag_ignores_the_file(self):
        self.write_config('db = "grid.sqlite"\n')
        self.assertFalse(project_config(enabled=False))

    def test_an_explicit_config_need_not_be_named_the_usual_thing(self):
        path = self.root / "other.gridqlconfig"
        path.write_text('name = "Elsewhere"\n')
        self.assertEqual(project_config(path).name, "Elsewhere")

    def test_a_missing_explicit_config_is_an_error(self):
        with self.assertRaises(GridQLError) as raised:
            project_config("nowhere.gridqlconfig")
        self.assertIn("no such config file", str(raised.exception))

    def test_an_unknown_setting_is_refused(self):
        path = self.write_config('database = "grid.sqlite"\n')
        with self.assertRaises(GridQLError) as raised:
            load_config(path)
        self.assertIn("unknown setting database", str(raised.exception))

    def test_db_and_csv_together_are_refused(self):
        path = self.write_config('db = "grid.sqlite"\ncsv = "export"\n')
        with self.assertRaises(GridQLError) as raised:
            load_config(path)
        self.assertIn("not both", str(raised.exception))

    def test_broken_toml_names_the_file(self):
        path = self.write_config("db = \n")
        with self.assertRaises(GridQLError) as raised:
            load_config(path)
        self.assertIn(CONFIG_NAME, str(raised.exception))

    def test_params_must_be_a_table(self):
        path = self.write_config('params = "feeder"\n')
        with self.assertRaises(GridQLError) as raised:
            load_config(path)
        self.assertIn("[params]", str(raised.exception))


class DatasetTests(ProjectTests):
    def test_the_config_supplies_the_dataset(self):
        self.write_config('db = "grid.sqlite"\n')
        network = network_for(None, None, project_config())
        self.assertEqual(network.get("REC-001").name, "Mainline Recloser")

    def test_an_explicit_db_wins_over_the_config(self):
        self.write_config('db = "nonexistent.sqlite"\n')
        save_network(build_sample_network(), str(self.root / "other.sqlite"))
        network = network_for("other.sqlite", None, project_config())
        self.assertEqual(network.get("REC-001").name, "Mainline Recloser")

    def test_without_a_config_the_sample_still_answers(self):
        network = network_for(None, None, Config())
        self.assertEqual(network.get("FDR-104").name, "Oakdale 104")

    def test_the_banner_names_the_project(self):
        self.write_config('name = "Oakdale"\ndb = "grid.sqlite"\n')
        self.assertIn("Oakdale", source_of(None, None, project_config()))

    def test_a_query_runs_against_the_configured_dataset(self):
        self.write_config('db = "grid.sqlite"\n')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["FIND reclosers"])
        self.assertIn("REC-001", buffer.getvalue())


class ScriptResolutionTests(ProjectTests):
    def test_a_path_that_exists_is_used_as_given(self):
        self.assertEqual(
            resolve_script(Config(), "queries/feeder_report.gridql"),
            "queries/feeder_report.gridql",
        )

    def test_a_name_resolves_inside_the_configured_queries(self):
        self.write_config('queries = "queries"\n')
        resolved = resolve_script(project_config(), "feeder_report")
        self.assertEqual(Path(resolved), self.root / "queries" / "feeder_report.gridql")

    def test_a_missing_suffix_is_supplied(self):
        self.assertEqual(
            Path(resolve_script(Config(), "queries/feeder_report")).name,
            "feeder_report.gridql",
        )

    def test_a_query_that_is_nowhere_says_what_was_tried(self):
        self.write_config('queries = "queries"\n')
        with self.assertRaises(GridQLError) as raised:
            resolve_script(project_config(), "no_such_report")
        self.assertIn("Tried", str(raised.exception))

    def test_running_a_query_by_name_uses_the_config(self):
        self.write_config('db = "grid.sqlite"\nqueries = "queries"\n')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["run", "feeder_report"])
        self.assertIn("REC-001", buffer.getvalue())

    def test_the_config_supplies_a_declared_parameter(self):
        # FDR-999 is not in the sample, so the error proves the value from
        # the config reached the query.
        self.write_config('queries = "queries"\n[params]\nfeeder = "FDR-999"\n')
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["run", "feeder_report"])
        self.assertEqual(code, 1)
        self.assertIn("FDR-999", buffer.getvalue())

    def test_the_command_line_wins_over_the_config(self):
        self.write_config('queries = "queries"\n[params]\nfeeder = "FDR-999"\n')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["run", "feeder_report", "--feeder", "FDR-104"])
        self.assertIn("REC-001", buffer.getvalue())

    def test_a_config_parameter_the_file_does_not_declare_is_ignored(self):
        # A project-wide default must not break every query that has no
        # use for it.
        self.write_query("plain", "FIND reclosers")
        self.write_config('queries = "queries"\n[params]\nfeeder = "FDR-104"\n')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["run", "plain"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", buffer.getvalue())


class ConfigCommandTests(ProjectTests):
    def test_it_reports_the_settings_in_effect(self):
        self.write_config(
            'name = "Oakdale"\ndb = "grid.sqlite"\nqueries = "queries"\n'
            '[params]\nfeeder = "FDR-104"\n'
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["config"])
        output = buffer.getvalue()
        self.assertIn("Oakdale", output)
        self.assertIn("grid.sqlite", output)
        self.assertIn("feeder = FDR-104", output)
        self.assertIn("feeder_report", output)

    def test_it_says_when_there_is_no_config(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["config"])
        self.assertIn("no project.gridqlconfig found", buffer.getvalue())

    def test_a_broken_config_is_reported_not_raised(self):
        self.write_config("db = \n")
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["config"])
        self.assertEqual(code, 1)
        self.assertIn("error:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
