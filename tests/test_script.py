"""SELECT / RETURN projection and .gridql file support."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from gridql import build_sample_network, execute, render
from gridql.cli import main
from gridql.errors import GridQLError, GridQLNameError
from gridql.formats import render_script
from gridql.lang import execute_script
from gridql.config import CONFIG_NAME, load_config
from gridql.script import find_scripts, read_script, run_file

REPO = Path(__file__).resolve().parent.parent
QUERIES = REPO / "queries"


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_projection_limits_and_orders_the_columns(self):
        result = execute(self.network, "FIND transformers SELECT kva, name")
        self.assertEqual(result.columns(), ["kva", "name"])
        self.assertEqual(result.rows()[0], {"kva": 500.0, "name": "Elm St Bank"})

    def test_selected_columns_keep_the_spelling_you_wrote(self):
        result = execute(self.network, "FIND transformers SELECT mRID")
        self.assertEqual(result.columns(), ["mRID"])
        self.assertEqual(result.rows()[0]["mRID"], "XFMR-001")

    def test_select_star_shows_everything_the_objects_carry(self):
        self.network.get("XFMR-001").extras["install_year"] = 1998
        starred = execute(self.network, "FIND transformers SELECT *").columns()
        plain = execute(self.network, "FIND transformers").columns()
        # The standard columns first, then the other fields, then the utility's own.
        self.assertEqual(starred[: len(plain)], plain)
        self.assertEqual(starred[len(plain):], ["substation", "voltage", "install_year"])

    def test_select_star_over_mixed_types_is_the_union(self):
        columns = execute(self.network, 'FIND devices FED BY "SW-002" SELECT *').columns()
        for column in ("kva", "kw", "state", "is_tie", "substation"):
            self.assertIn(column, columns)
        self.assertEqual(len(columns), len({c.lower() for c in columns}))

    def test_without_select_the_filtered_attribute_is_shown(self):
        self.network.get("XFMR-001").extras["install_year"] = 1998
        self.network.get("XFMR-002").extras["install_year"] = 2011
        result = execute(self.network, "FIND transformers WHERE install_year > 1990")
        self.assertEqual(result.columns()[-1], "install_year")
        self.assertEqual([row["install_year"] for row in result.rows()], [1998, 2011])

    def test_a_filter_on_a_column_already_shown_adds_nothing(self):
        plain = execute(self.network, "FIND transformers").columns()
        self.assertEqual(execute(self.network, "FIND transformers WHERE KVA > 100").columns(), plain)

    def test_sorting_and_derived_attributes_are_shown_too(self):
        columns = execute(
            self.network, "FIND devices WHERE NOT energized ORDER BY depth DESC"
        ).columns()
        self.assertEqual(columns[-2:], ["energized", "depth"])

    def test_an_explicit_select_is_exactly_what_it_says(self):
        result = execute(self.network, "FIND transformers WHERE kva > 100 SELECT name ORDER BY depth")
        self.assertEqual(result.columns(), ["name"])

    def test_a_derived_attribute_can_be_selected(self):
        rows = execute(self.network, "FIND loads SELECT mrid, energized").rows()
        self.assertEqual(rows, [
            {"mrid": "LOAD-001", "energized": True},
            {"mrid": "LOAD-002", "energized": False},
        ])

    def test_a_column_the_object_lacks_is_empty_not_missing(self):
        rows = execute(self.network, "FIND devices SELECT mrid, kva").rows()
        by_mrid = {row["mrid"]: row["kva"] for row in rows}
        self.assertEqual(by_mrid["XFMR-002"], 75.0)
        self.assertIsNone(by_mrid["LOAD-001"])

    def test_unknown_column_is_rejected_with_a_suggestion(self):
        with self.assertRaises(GridQLNameError) as raised:
            execute(self.network, "FIND transformers SELECT kvaa")
        self.assertIn("kva", str(raised.exception))

    def test_projection_reaches_the_rendered_output(self):
        output = render(execute(self.network, "FIND reclosers SELECT mRID, name"), "csv")
        self.assertEqual(output.splitlines()[0], "mRID,name")

    def test_the_docs_example_query(self):
        result = execute(self.network, '''
            FIND transformers
            DOWNSTREAM OF "FDR-104"
            WHERE kva >= 500
            SELECT name, mRID, kva, primary_voltage, secondary_voltage
            RETURN table
        ''')
        self.assertEqual(result.mrids, ["XFMR-001"])
        self.assertEqual(result.columns(),
                         ["name", "mRID", "kva", "primary_voltage", "secondary_voltage"])
        self.assertEqual(result.output_format, "table")


class ReturnClauseTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_return_is_reported_by_the_result(self):
        self.assertEqual(execute(self.network, "FIND feeders RETURN json").output_format, "json")
        self.assertIsNone(execute(self.network, "FIND feeders").output_format)

    def test_the_cli_honours_return(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["FIND feeders RETURN json"])
        self.assertEqual(json.loads(buffer.getvalue())[0]["mrid"], "FDR-104")

    def test_an_explicit_format_flag_overrides_return(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["FIND feeders RETURN json", "--format", "csv"])
        self.assertEqual(buffer.getvalue().splitlines()[0].split(",")[0], "mrid")


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_statements_run_in_order(self):
        results = execute_script(self.network, "FIND reclosers; FIND feeders; FIND breakers")
        self.assertEqual([r.type_name for r in results], ["reclosers", "feeders", "breakers"])
        self.assertEqual([r.mrids for r in results], [["REC-001"], ["FDR-104"], ["BRK-001"]])

    def test_each_statement_keeps_its_own_clauses(self):
        results = execute_script(
            self.network,
            "FIND feeders SELECT mrid RETURN json; FIND reclosers SELECT name RETURN csv",
        )
        self.assertEqual([r.columns() for r in results], [["mrid"], ["name"]])
        self.assertEqual([r.output_format for r in results], ["json", "csv"])

    def test_render_script_labels_multiple_statements(self):
        output = render_script(execute_script(self.network, "FIND feeders; FIND reclosers"))
        self.assertIn("-- 1. FIND feeders", output)
        self.assertIn("-- 2. FIND reclosers", output)

    def test_render_script_leaves_a_single_statement_unlabelled(self):
        self.assertNotIn("-- 1.", render_script(execute_script(self.network, "FIND feeders")))

    def test_json_mode_wraps_every_statement_in_one_document(self):
        payload = json.loads(
            render_script(execute_script(self.network, "FIND feeders; FIND reclosers"), "json")
        )
        self.assertEqual([entry["type"] for entry in payload], ["feeders", "reclosers"])
        self.assertEqual(payload[1]["rows"][0]["mrid"], "REC-001")

    def test_a_format_argument_overrides_every_return_clause(self):
        output = render_script(
            execute_script(self.network, "FIND feeders RETURN table; FIND reclosers RETURN table"),
            "csv",
        )
        self.assertNotIn("---", output)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def write(self, text, suffix=".gridql"):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_read_and_run_a_file(self):
        path = self.write("-- a comment\nFIND reclosers SELECT mRID\n")
        script = read_script(path)
        self.assertEqual(len(script), 1)
        self.assertEqual(script.path, path)
        self.assertEqual(run_file(self.network, path)[0].mrids, ["REC-001"])

    def test_a_multi_statement_file(self):
        path = self.write("FIND feeders;\n\nFIND reclosers;\n")
        self.assertEqual([r.type_name for r in run_file(self.network, path)],
                         ["feeders", "reclosers"])

    def test_a_missing_file_is_a_gridql_error(self):
        with self.assertRaises(GridQLError):
            read_script("does-not-exist.gridql")

    def test_find_scripts_lists_the_bundled_queries(self):
        names = [path.name for path in find_scripts(QUERIES)]
        self.assertIn("feeder_analysis.gridql", names)
        self.assertEqual(names, sorted(names))

    def test_every_bundled_query_runs(self):
        # The project file beside them supplies the parameters they require,
        # which is how they are meant to be run.
        params = load_config(REPO / CONFIG_NAME).params
        for path in find_scripts(QUERIES):
            with self.subTest(query=path.name):
                script = read_script(path)
                declared = {k: v for k, v in params.items() if script.param(k)}
                results = run_file(self.network, path, declared)
                self.assertTrue(results)
                for result in results:
                    result.rows()

    def test_the_bundled_analysis_query_matches_the_doc(self):
        results = run_file(self.network, QUERIES / "feeder_analysis.gridql")
        self.assertEqual(results[0].mrids, ["XFMR-001"])
        self.assertEqual(
            results[0].columns(),
            ["name", "mRID", "kva", "primary_voltage", "secondary_voltage"],
        )


class RunCommandTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_run_a_bundled_query(self):
        code, out, _ = self.run_cli(["run", str(QUERIES / "feeder_analysis.gridql")])
        self.assertEqual(code, 0)
        self.assertIn("XFMR-001", out)
        self.assertIn("mRID", out)

    def test_run_a_multi_statement_query(self):
        code, out, _ = self.run_cli(["run", str(QUERIES / "feeder_summary.gridql")])
        self.assertEqual(code, 0)
        self.assertIn("-- 1.", out)
        self.assertIn("-- 3.", out)

    def test_run_with_a_format_override(self):
        code, out, _ = self.run_cli(
            ["run", str(QUERIES / "feeder_summary.gridql"), "--format", "json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 3)

    def test_run_explain(self):
        code, out, _ = self.run_cli(
            ["run", str(QUERIES / "feeder_analysis.gridql"), "--explain"]
        )
        self.assertEqual(code, 0)
        self.assertIn("plan:", out)
        self.assertIn("project -> name, mRID", out)

    def test_run_a_missing_file_exits_non_zero(self):
        code, _, err = self.run_cli(["run", "nope.gridql"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_run_a_file_with_a_syntax_error(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".gridql", delete=False, encoding="utf-8")
        handle.write("FIND transformers WHERE kva >=\n")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        code, _, err = self.run_cli(["run", handle.name])
        self.assertEqual(code, 1)
        self.assertIn("^", err)


if __name__ == "__main__":
    unittest.main()
