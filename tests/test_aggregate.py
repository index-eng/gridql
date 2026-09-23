"""Aggregation: SUM/COUNT/AVG/MIN/MAX, GROUP BY, ORDER BY and LIMIT."""

import contextlib
import io
import json
import unittest

from gridql import Network, build_sample_network, execute, render
from gridql.cli import main
from gridql.errors import GridQLError, GridQLNameError, GridQLSyntaxError
from gridql.lang import parse

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project


def setUpModule():
    unittest.enterModuleContext(sample_project())


def tied_feeders():
    """Two feeders on one substation, so grouping has something to group."""
    network = Network()
    network.add_substation("SUB", voltage="13.8kV")
    for name, loads in (("FDR-A", (300, 120)), ("FDR-B", (75,))):
        feeder = network.add_feeder(name, voltage="13.8kV", substation="SUB")
        feeder.add_breaker(f"{name}-BRK")
        for index, kw in enumerate(loads):
            feeder.add_transformer(f"{name}-X{index}", kva=kw * 2, after=f"{name}-BRK")
            feeder.add_load(f"{name}-L{index}", kw=kw)
    return network


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def rows(self, source):
        return execute(self.network, source).rows()

    def test_the_load_transfer_question(self):
        self.assertEqual(
            self.rows('FIND loads DOWNSTREAM OF "REC-001" SELECT COUNT(*), SUM(kw), SUM(kvar)'),
            [{"COUNT(*)": 2, "SUM(kw)": 358.0, "SUM(kvar)": 107.0}],
        )

    def test_every_function(self):
        self.assertEqual(
            self.rows("FIND loads SELECT COUNT(*), SUM(kw), AVG(kw), MIN(kw), MAX(kw)"),
            [{"COUNT(*)": 2, "SUM(kw)": 358.0, "AVG(kw)": 179.0,
              "MIN(kw)": 48.0, "MAX(kw)": 310.0}],
        )

    def test_an_aggregate_query_returns_exactly_one_row(self):
        self.assertEqual(len(self.rows("FIND devices SELECT COUNT(*)")), 1)

    def test_count_star_counts_rows_and_count_attribute_counts_values(self):
        # Every device has an mRID; only transformers have a kVA rating.
        self.assertEqual(
            self.rows("FIND devices SELECT COUNT(*), COUNT(mrid), COUNT(kva)"),
            [{"COUNT(*)": 11, "COUNT(mrid)": 11, "COUNT(kva)": 2}],
        )

    def test_an_empty_match_still_answers(self):
        self.assertEqual(self.rows("FIND fuses SELECT COUNT(*)"), [{"COUNT(*)": 0}])

    def test_sum_of_nothing_is_nothing_not_zero(self):
        rows = self.rows("FIND transformers WHERE kva > 9000 SELECT COUNT(*), SUM(kva)")
        self.assertEqual(rows, [{"COUNT(*)": 0, "SUM(kva)": None}])

    def test_aggregates_ignore_equipment_that_lacks_the_attribute(self):
        # Only the two transformers carry kVA; the other nine devices do not.
        self.assertEqual(self.rows("FIND devices SELECT SUM(kva)"), [{"SUM(kva)": 575.0}])

    def test_min_and_max_work_on_text(self):
        rows = self.rows("FIND transformers SELECT MIN(mrid), MAX(mrid)")
        self.assertEqual(rows, [{"MIN(mrid)": "XFMR-001", "MAX(mrid)": "XFMR-002"}])

    def test_aggregates_respect_filters_and_topology(self):
        self.assertEqual(
            self.rows('FIND loads FED BY "SW-002" SELECT SUM(kw)'), [{"SUM(kw)": 48.0}]
        )

    def test_units_apply_inside_a_filtered_aggregate(self):
        self.assertEqual(
            self.rows("FIND transformers WHERE kva >= 0.5MVA SELECT COUNT(*)"),
            [{"COUNT(*)": 1}],
        )

    def test_the_header_keeps_the_spelling_written(self):
        self.assertEqual(list(self.rows("FIND loads SELECT sum(kw)")[0]), ["sum(kw)"])


class GroupByTests(unittest.TestCase):
    def setUp(self):
        self.network = tied_feeders()

    def rows(self, source):
        return execute(self.network, source).rows()

    def test_group_by_alone_counts_each_group(self):
        self.assertEqual(
            self.rows("FIND devices GROUP BY feeder"),
            [{"feeder": "FDR-A", "COUNT(*)": 5}, {"feeder": "FDR-B", "COUNT(*)": 3}],
        )

    def test_group_with_an_explicit_projection(self):
        self.assertEqual(
            self.rows("FIND loads SELECT feeder, SUM(kw) GROUP BY feeder"),
            [{"feeder": "FDR-A", "SUM(kw)": 420.0}, {"feeder": "FDR-B", "SUM(kw)": 75.0}],
        )

    def test_group_by_two_columns(self):
        rows = self.rows("FIND devices SELECT feeder, type, COUNT(*) GROUP BY feeder, type")
        self.assertIn({"feeder": "FDR-A", "type": "load", "COUNT(*)": 2}, rows)
        self.assertEqual(len(rows), 6)

    def test_groups_are_ordered_by_key_when_nothing_else_says(self):
        keys = [row["feeder"] for row in self.rows("FIND devices GROUP BY feeder")]
        self.assertEqual(keys, sorted(keys))

    def test_an_ungrouped_column_is_refused(self):
        with self.assertRaises(GridQLError) as raised:
            self.rows("FIND loads SELECT mrid, SUM(kw) GROUP BY feeder")
        self.assertIn("add it to GROUP BY", str(raised.exception))

    def test_ordering_groups_by_an_ungrouped_column_is_refused(self):
        # A group has no single kva to sort by; silently not sorting is worse.
        with self.assertRaises(GridQLError) as raised:
            self.rows("FIND transformers GROUP BY feeder ORDER BY kva DESC")
        self.assertIn("add it to GROUP BY", str(raised.exception))

    def test_ordering_groups_by_a_grouped_column_still_works(self):
        keys = [row["feeder"] for row in self.rows("FIND devices GROUP BY feeder ORDER BY FEEDER DESC")]
        self.assertEqual(keys, ["FDR-B", "FDR-A"])

    def test_an_aggregate_without_grouping_needs_no_group_clause(self):
        self.assertEqual(self.rows("FIND loads SELECT SUM(kw)"), [{"SUM(kw)": 495.0}])


class OrderAndLimitTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def mrids(self, source):
        return execute(self.network, source).mrids

    def test_order_by_ascends_by_default(self):
        self.assertEqual(self.mrids("FIND transformers ORDER BY kva"), ["XFMR-002", "XFMR-001"])

    def test_order_by_descending(self):
        self.assertEqual(
            self.mrids("FIND transformers ORDER BY kva DESC"), ["XFMR-001", "XFMR-002"]
        )

    def test_explicit_asc(self):
        self.assertEqual(
            self.mrids("FIND transformers ORDER BY kva ASC"), ["XFMR-002", "XFMR-001"]
        )

    def test_without_order_by_results_stay_sorted_by_mrid(self):
        found = self.mrids("FIND devices")
        self.assertEqual(found, sorted(found))

    def test_equipment_missing_the_sort_column_goes_last_either_way(self):
        # "Biggest first" should not open with nine devices that have no rating.
        descending = self.mrids("FIND devices ORDER BY kva DESC")
        self.assertEqual(descending[:2], ["XFMR-001", "XFMR-002"])

        ascending = self.mrids("FIND devices ORDER BY kva")
        self.assertEqual(ascending[:2], ["XFMR-002", "XFMR-001"])

        self.assertEqual(sorted(descending[2:]), sorted(ascending[2:]))

    def test_limit_after_a_descending_sort_skips_the_unrated(self):
        self.assertEqual(self.mrids("FIND devices ORDER BY kva DESC LIMIT 2"),
                         ["XFMR-001", "XFMR-002"])

    def test_order_by_several_columns(self):
        found = self.mrids("FIND devices ORDER BY type, mrid DESC")
        self.assertEqual(found[0], "BRK-001")  # breaker sorts first by type

    def test_limit_truncates(self):
        self.assertEqual(self.mrids("FIND devices LIMIT 3"), ["BRK-001", "LN-001", "LN-002"])

    def test_limit_applies_after_ordering(self):
        self.assertEqual(self.mrids("FIND transformers ORDER BY kva DESC LIMIT 1"), ["XFMR-001"])

    def test_limit_zero(self):
        self.assertEqual(self.mrids("FIND devices LIMIT 0"), [])

    def test_limit_beyond_the_result_is_harmless(self):
        self.assertEqual(len(self.mrids("FIND devices LIMIT 999")), 11)

    def test_aggregate_rows_can_be_ordered_and_limited(self):
        rows = execute(
            tied_feeders(),
            "FIND loads SELECT feeder, SUM(kw) GROUP BY feeder ORDER BY SUM(kw) DESC LIMIT 1",
        ).rows()
        self.assertEqual(rows, [{"feeder": "FDR-A", "SUM(kw)": 420.0}])

    def test_ordering_by_an_aggregate_not_in_the_projection(self):
        rows = execute(
            tied_feeders(), "FIND loads SELECT feeder GROUP BY feeder ORDER BY SUM(kw)"
        ).rows()
        self.assertEqual(rows, [{"feeder": "FDR-B"}, {"feeder": "FDR-A"}])


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_an_unknown_function(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices SELECT TOTAL(kw)")
        self.assertIn("COUNT, SUM, AVG, MIN, MAX", str(raised.exception))

    def test_star_is_only_for_count(self):
        with self.assertRaises(GridQLSyntaxError):
            parse("FIND devices SELECT SUM(*)")

    def test_star_cannot_be_mixed_with_other_columns(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices SELECT *, COUNT(*)")
        self.assertIn("must stand alone", str(raised.exception))

    def test_a_limit_must_be_a_whole_count(self):
        for source in ("FIND devices LIMIT -3", "FIND devices LIMIT 2.5", "FIND devices LIMIT x"):
            with self.subTest(source=source), self.assertRaises(GridQLSyntaxError):
                parse(source)

    def test_clauses_must_be_in_order(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices ORDER BY kva GROUP BY feeder")
        self.assertIn("must come earlier", str(raised.exception))

    def test_summing_text_is_refused(self):
        with self.assertRaises(GridQLError) as raised:
            execute(self.network, "FIND devices SELECT SUM(name)")
        self.assertIn("not a number", str(raised.exception))

    def test_ordering_by_an_aggregate_needs_an_aggregate_query(self):
        with self.assertRaises(GridQLError) as raised:
            execute(self.network, "FIND devices ORDER BY SUM(kva)")
        self.assertIn("needs an aggregate query", str(raised.exception))

    def test_an_unknown_attribute_inside_an_aggregate(self):
        with self.assertRaises(GridQLNameError) as raised:
            execute(self.network, "FIND devices SELECT COUNT(bogus)")
        self.assertIn("not an attribute", str(raised.exception))

    def test_an_unknown_group_column(self):
        with self.assertRaises(GridQLNameError):
            execute(self.network, "FIND devices GROUP BY bogus")

    def test_an_unknown_sort_column(self):
        with self.assertRaises(GridQLNameError):
            execute(self.network, "FIND devices ORDER BY bogus")

    def test_an_attribute_the_type_cannot_have(self):
        with self.assertRaises(GridQLNameError):
            execute(self.network, "FIND fuses SELECT SUM(kva)")


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def result(self, source):
        return execute(self.network, source)

    def test_an_aggregate_renders_as_a_one_row_table(self):
        output = render(self.result("FIND loads SELECT SUM(kw)"), "table")
        self.assertIn("SUM(kw)", output)
        self.assertIn("358", output)
        self.assertTrue(output.rstrip().endswith("1 row"))

    def test_json(self):
        payload = json.loads(render(self.result("FIND loads SELECT COUNT(*), SUM(kw)"), "json"))
        self.assertEqual(payload, [{"COUNT(*)": 2, "SUM(kw)": 358.0}])

    def test_csv(self):
        lines = render(self.result("FIND loads SELECT COUNT(*), SUM(kw)"), "csv").splitlines()
        self.assertEqual(lines, ["COUNT(*),SUM(kw)", "2,358"])

    def test_the_result_knows_it_is_an_aggregate(self):
        self.assertTrue(self.result("FIND loads SELECT SUM(kw)").is_aggregate)
        self.assertFalse(self.result("FIND loads").is_aggregate)

    def test_the_matched_equipment_is_still_available(self):
        result = self.result("FIND loads SELECT SUM(kw)")
        self.assertEqual(result.mrids, ["LOAD-001", "LOAD-002"])


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_an_aggregate_from_the_command_line(self):
        code, out, _ = self.run_cli(['FIND loads DOWNSTREAM OF "REC-001" SELECT SUM(kw)'])
        self.assertEqual(code, 0)
        self.assertIn("358", out)

    def test_explain_shows_the_aggregation_plan(self):
        code, out, _ = self.run_cli(
            ["--explain", "FIND devices SELECT type, COUNT(*) GROUP BY type ORDER BY type LIMIT 2"]
        )
        self.assertEqual(code, 0)
        self.assertIn("group by -> type", out)
        self.assertIn("order by -> type", out)
        self.assertIn("limit -> 2", out)

    def test_a_bad_aggregate_exits_non_zero(self):
        code, _, err = self.run_cli(["FIND devices SELECT SUM(name)"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_aggregation_works_in_a_gridql_file(self):
        import tempfile
        from pathlib import Path

        handle = tempfile.NamedTemporaryFile("w", suffix=".gridql", delete=False, encoding="utf-8")
        handle.write(
            "-- connected load by feeder\n"
            "FIND loads SELECT feeder, COUNT(*), SUM(kw) GROUP BY feeder RETURN csv\n"
        )
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))

        code, out, _ = self.run_cli(["run", handle.name])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "feeder,COUNT(*),SUM(kw)")
        self.assertIn("FDR-104,2,358", out)


if __name__ == "__main__":
    unittest.main()
