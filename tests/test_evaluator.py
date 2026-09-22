import json
import unittest

from gridql import build_sample_network, execute, render
from gridql.errors import GridQLNameError, UnitError


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def run_query(self, source):
        return execute(self.network, source).mrids

    # -- type selection -------------------------------------------------

    def test_switches_covers_every_switching_device(self):
        self.assertEqual(
            self.run_query("FIND switches"),
            ["BRK-001", "REC-001", "SW-001", "SW-002", "TIE-001"],
        )

    def test_reclosers_is_narrower_than_switches(self):
        self.assertEqual(self.run_query("FIND reclosers"), ["REC-001"])

    def test_devices_excludes_containers(self):
        found = self.run_query("FIND devices")
        self.assertNotIn("FDR-104", found)
        self.assertNotIn("SUB-001", found)
        self.assertEqual(len(found), len(self.network.devices))

    def test_singular_and_alias_type_names(self):
        for source in ("FIND transformer", "FIND transformers", "FIND xfmrs"):
            self.assertEqual(self.run_query(source), ["XFMR-001", "XFMR-002"], source)

    def test_unknown_type_suggests_a_real_one(self):
        with self.assertRaises(GridQLNameError) as raised:
            self.run_query("FIND transfomers")
        self.assertIn("transformers", str(raised.exception))

    def test_unknown_device_in_a_relation(self):
        with self.assertRaises(GridQLNameError):
            self.run_query('FIND devices DOWNSTREAM OF "REC-999"')

    # -- filters --------------------------------------------------------

    def test_results_are_sorted_by_mrid(self):
        found = self.run_query("FIND devices")
        self.assertEqual(found, sorted(found))

    def test_value_comparison_is_case_insensitive(self):
        self.assertEqual(self.run_query("FIND switches WHERE state = open"),
                         self.run_query('FIND switches WHERE state = "OPEN"'))

    def test_attribute_to_attribute_comparison(self):
        self.assertEqual(self.run_query("FIND switches WHERE state != normal_state"), ["SW-002"])

    def test_quoting_forces_the_literal_reading(self):
        # No switch is in the literal state "normal_state".
        self.assertEqual(self.run_query('FIND switches WHERE state != "normal_state"'),
                         ["BRK-001", "REC-001", "SW-001", "SW-002", "TIE-001"])

    def test_numeric_comparisons(self):
        self.assertEqual(self.run_query("FIND transformers WHERE kva >= 500"), ["XFMR-001"])
        self.assertEqual(self.run_query("FIND transformers WHERE kva < 500"), ["XFMR-002"])
        self.assertEqual(self.run_query("FIND transformers WHERE kva = 75"), ["XFMR-002"])

    def test_units_convert_to_the_attribute_unit(self):
        for source in ("kva >= 500", "kva >= 500kVA", "kva >= 0.5MVA", "kva >= 500000VA"):
            self.assertEqual(self.run_query(f"FIND transformers WHERE {source}"),
                             ["XFMR-001"], source)

    def test_voltage_units(self):
        self.assertEqual(self.run_query("FIND feeders WHERE voltage = 13.8kV"), ["FDR-104"])
        self.assertEqual(self.run_query("FIND feeders WHERE voltage = 13800V"), ["FDR-104"])
        self.assertEqual(self.run_query("FIND feeders WHERE voltage = 13.8"), ["FDR-104"])

    def test_mismatched_dimensions_are_an_error(self):
        with self.assertRaises(UnitError):
            self.run_query("FIND transformers WHERE kva >= 500kW")

    def test_unit_on_a_unitless_attribute_is_an_error(self):
        with self.assertRaises(UnitError):
            self.run_query("FIND devices WHERE phases = 3kV")

    def test_and_or_not(self):
        self.assertEqual(
            self.run_query("FIND transformers WHERE kva >= 500 AND phases = ABC"), ["XFMR-001"]
        )
        self.assertEqual(
            self.run_query("FIND transformers WHERE kva >= 500 OR phases = A"),
            ["XFMR-001", "XFMR-002"],
        )
        self.assertEqual(self.run_query("FIND transformers WHERE NOT kva >= 500"), ["XFMR-002"])

    def test_parentheses_change_the_answer(self):
        self.assertEqual(
            self.run_query("FIND switches WHERE state = OPEN AND is_tie = false OR type = breaker"),
            ["BRK-001", "SW-002"],
        )
        self.assertEqual(
            self.run_query("FIND switches WHERE state = OPEN AND (is_tie = false OR type = breaker)"),
            ["SW-002"],
        )

    def test_in_list(self):
        self.assertEqual(self.run_query("FIND devices WHERE type IN (load, transformer)"),
                         ["LOAD-001", "LOAD-002", "XFMR-001", "XFMR-002"])

    def test_contains(self):
        self.assertEqual(self.run_query("FIND transformers WHERE phases CONTAINS A"),
                         ["XFMR-001", "XFMR-002"])
        self.assertEqual(self.run_query("FIND devices WHERE name CONTAINS 'Maple'"),
                         ["LOAD-002", "SW-002", "XFMR-002"])

    def test_bare_attribute_is_a_truth_test(self):
        self.assertEqual(self.run_query("FIND switches WHERE is_tie"), ["TIE-001"])
        self.assertEqual(self.run_query("FIND loads WHERE NOT energized"), ["LOAD-002"])

    def test_derived_attributes(self):
        self.assertEqual(self.run_query("FIND devices WHERE cim_class = PowerTransformer"),
                         ["XFMR-001", "XFMR-002"])
        self.assertEqual(self.run_query('FIND devices WHERE feeder = "FDR-104" AND type = fuse'), [])

    def test_attribute_the_object_lacks_never_matches(self):
        # Only transformers carry a kVA rating, so for every other device
        # neither the test nor its negation matches.
        self.assertEqual(self.run_query("FIND devices WHERE kva >= 0"), ["XFMR-001", "XFMR-002"])
        self.assertEqual(self.run_query("FIND devices WHERE kva != 0"), ["XFMR-001", "XFMR-002"])

    def test_attribute_the_type_cannot_have_is_an_error_not_an_empty_answer(self):
        # A misspelt attribute would otherwise look exactly like "none match".
        with self.assertRaises(GridQLNameError) as raised:
            self.run_query("FIND transformers WHERE kvaa >= 500")
        self.assertIn("kva", raised.exception.suggestions)
        with self.assertRaises(GridQLNameError):
            self.run_query("FIND loads WHERE kva >= 0")  # loads have kvar, not kva
        with self.assertRaises(GridQLNameError):
            self.run_query("FIND switches WHERE NOT (state = OPEN OR energised)")

    def test_a_bare_word_value_on_the_right_is_not_checked_as_an_attribute(self):
        self.assertEqual(self.run_query("FIND switches WHERE state = OPEN"), ["SW-002", "TIE-001"])

    def test_subclass_attributes_are_queryable_through_the_base_type(self):
        self.assertEqual(self.run_query("FIND devices WHERE kva >= 500"), ["XFMR-001"])

    # -- topology combined with filters ---------------------------------

    def test_relation_then_filter(self):
        self.assertEqual(
            self.run_query('FIND transformers DOWNSTREAM OF "REC-001" WHERE kva >= 500'),
            ["XFMR-001"],
        )

    def test_relations_intersect(self):
        self.assertEqual(
            self.run_query('FIND switches DOWNSTREAM OF "REC-001" CONNECTED TO "REC-001"'),
            ["SW-001", "SW-002"],
        )

    def test_de_energised_work_area(self):
        # Nearest the recloser first: the transformer, then the load beyond it.
        self.assertEqual(
            self.run_query('FIND devices DOWNSTREAM OF "REC-001" WHERE NOT energized'),
            ["XFMR-002", "LOAD-002"],
        )


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_table_has_a_header_and_a_row_count(self):
        output = render(execute(self.network, "FIND reclosers"), "table")
        self.assertIn("mrid", output.splitlines()[0])
        self.assertIn("REC-001", output)
        self.assertTrue(output.rstrip().endswith("1 row"))

    def test_empty_result_says_so(self):
        self.assertEqual(render(execute(self.network, "FIND fuses"), "table"),
                         "no fuses matched")

    def test_json_is_parseable_and_typed(self):
        payload = json.loads(render(execute(self.network, "FIND transformers"), "json"))
        self.assertEqual([row["mrid"] for row in payload], ["XFMR-001", "XFMR-002"])
        self.assertEqual(payload[0]["kva"], 500)

    def test_csv_header_matches_the_columns(self):
        lines = render(execute(self.network, "FIND loads"), "csv").splitlines()
        self.assertEqual(lines[0].split(",")[:3], ["mrid", "name", "type"])
        self.assertEqual(len(lines), 3)

    def test_mixed_results_report_every_value_they_have(self):
        rows = {row["mrid"]: row for row in execute(self.network, "FIND devices").rows()}
        self.assertEqual(rows["XFMR-002"]["voltage"], 13.8)  # not blanked by the union
        self.assertIsNone(rows["LOAD-001"]["kva"])

    def test_floats_render_without_noise(self):
        self.assertIn("500", render(execute(self.network, "FIND transformers"), "csv"))
        self.assertNotIn("500.0", render(execute(self.network, "FIND transformers"), "csv"))

    def test_unknown_format(self):
        with self.assertRaises(ValueError):
            render(execute(self.network, "FIND feeders"), "xml")


if __name__ == "__main__":
    unittest.main()
