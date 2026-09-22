"""PARAM declarations, $references and binding."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute, run_file
from gridql.cli import main, parameters, split_parameters
from gridql.errors import GridQLError, GridQLSyntaxError, ParameterError
from gridql.lang import bind_script, evaluate_script, parse, parse_script
from gridql.lang.ast import Literal, ParamRef, Quantity
from gridql.lang.binding import coerce, resolve


class ParseTests(unittest.TestCase):
    def test_a_declaration_carries_its_default(self):
        script = parse_script('PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        param = script.params[0]
        self.assertEqual(param.name, "feeder")
        self.assertEqual(param.default, Literal("FDR-104"))
        self.assertFalse(param.required)

    def test_a_declaration_without_a_default_is_required(self):
        script = parse_script("PARAM feeder\nFIND devices FED BY $feeder")
        self.assertTrue(script.params[0].required)
        self.assertEqual(script.required_params, script.params)

    def test_a_default_may_carry_a_unit(self):
        script = parse_script("PARAM floor = 0.5MVA\nFIND transformers WHERE kva >= $floor")
        self.assertEqual(script.params[0].default, Quantity(0.5, "MVA"))

    def test_a_bare_word_default_is_a_value_not_an_attribute(self):
        script = parse_script("PARAM position = OPEN\nFIND switches WHERE state = $position")
        self.assertEqual(script.params[0].default, Literal("OPEN"))

    def test_a_reference_stands_where_a_value_stands(self):
        script = parse_script(
            "PARAM feeder\nPARAM floor\nPARAM top\n"
            "FIND transformers FED BY $feeder WHERE kva >= $floor LIMIT $top"
        )
        query = script.statements[0]
        self.assertEqual(query.relations[0].target.name, "feeder")
        self.assertIsInstance(query.where.operand, ParamRef)
        self.assertIsInstance(query.limit, ParamRef)

    def test_a_reference_survives_describe(self):
        source = 'PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder'
        described = parse_script(source).describe()
        self.assertIn('PARAM feeder = "FDR-104"', described)
        self.assertIn("FED BY $feeder", described)
        # What describe() prints, the parser reads back as the same script.
        self.assertEqual(parse_script(described).describe(), described)

    def test_an_undeclared_reference_is_a_syntax_error(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("PARAM feeder\nFIND devices FED BY $feedr")
        self.assertIn("$feedr", str(raised.exception))
        self.assertIn("did you mean $feeder?", str(raised.exception))

    def test_a_reference_with_no_declarations_at_all_says_so(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("FIND devices FED BY $feeder")
        self.assertIn("declare it with PARAM", str(raised.exception))

    def test_declarations_must_come_first(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script('FIND devices;\nPARAM feeder = "FDR-104"')
        self.assertIn("before the first FIND", str(raised.exception))

    def test_a_parameter_cannot_be_declared_twice(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("PARAM feeder\nPARAM feeder\nFIND devices FED BY $feeder")
        self.assertIn("declared twice", str(raised.exception))

    def test_the_name_may_be_written_with_its_dollar(self):
        script = parse_script('PARAM $feeder = "FDR-104"\nFIND devices FED BY $feeder')
        self.assertEqual(script.params[0].name, "feeder")

    def test_a_keyword_cannot_name_a_parameter(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("PARAM where = 1\nFIND devices")
        self.assertIn("keyword", str(raised.exception))

    def test_a_bare_dollar_is_rejected(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("FIND devices FED BY $")
        self.assertIn("parameter name after '$'", str(raised.exception))

    def test_a_file_of_declarations_alone_asks_nothing(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("PARAM feeder = 1")
        self.assertIn("asks nothing", str(raised.exception))

    def test_a_one_off_query_cannot_declare_parameters(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse('PARAM feeder = "FDR-104" FIND devices FED BY $feeder')
        self.assertIn("belong in a .gridql file", str(raised.exception))


class CoercionTests(unittest.TestCase):
    def test_a_name_stays_a_string(self):
        self.assertEqual(coerce("FDR-104"), Literal("FDR-104"))

    def test_a_number_becomes_a_quantity(self):
        self.assertEqual(coerce("500"), Quantity(500.0, None))
        self.assertEqual(coerce(500), Quantity(500.0, None))

    def test_a_value_may_carry_its_unit(self):
        self.assertEqual(coerce("0.5MVA"), Quantity(0.5, "MVA"))

    def test_a_boolean_stays_a_boolean(self):
        self.assertEqual(coerce(True), Literal(True))

    def test_a_number_keeps_its_spelling(self):
        self.assertEqual(coerce("0412").text, "0412")
        self.assertEqual(coerce(" 12A ").text, "12A")

    def test_a_name_that_starts_with_a_number_stays_a_string(self):
        # "Main" is not a unit, so this was never a quantity.
        self.assertEqual(coerce("12 Main"), Literal("12 Main"))


def numbered_network():
    """Equipment whose names look like numbers, as pole and circuit IDs do."""
    network = Network()
    feeder = network.add_feeder("0412")
    feeder.add_breaker("12A")
    feeder.add_switch("BRK-104")
    street = network.add_feeder("12 Main")
    street.add_breaker("B-12")
    return network


class NumberLikeNameTests(unittest.TestCase):
    def setUp(self):
        self.network = numbered_network()

    def run_script(self, source, **values):
        return [r.mrids for r in evaluate_script(self.network, parse_script(source), values)]

    def test_a_leading_zero_survives_as_a_topology_target(self):
        found = self.run_script("PARAM feeder\nFIND devices FED BY $feeder", feeder="0412")
        self.assertEqual(found, [["12A", "BRK-104"]])

    def test_a_leading_zero_survives_in_a_default(self):
        found = self.run_script("PARAM feeder = 0412\nFIND devices FED BY $feeder")
        self.assertEqual(found, [["12A", "BRK-104"]])

    def test_a_name_with_a_space_survives_as_a_topology_target(self):
        found = self.run_script("PARAM feeder\nFIND devices FED BY $feeder", feeder="12 Main")
        self.assertEqual(found, [["B-12"]])

    def test_a_name_ending_in_a_unit_compares_as_a_name(self):
        found = self.run_script("PARAM device\nFIND devices WHERE name = $device", device="12A")
        self.assertEqual(found, [["12A"]])

    def test_a_leading_zero_does_not_match_the_bare_number(self):
        self.network.add_feeder("412")
        found = self.run_script("PARAM feeder\nFIND feeders WHERE mrid = $feeder", feeder="0412")
        self.assertEqual(found, [["0412"]])

    def test_a_supplied_number_still_converts_against_a_rated_attribute(self):
        network = build_sample_network()
        script = parse_script("PARAM floor\nFIND transformers WHERE kva >= $floor")
        self.assertEqual(evaluate_script(network, script, {"floor": "0.4MVA"})[0].mrids, ["XFMR-001"])
        self.assertEqual(evaluate_script(network, script, {"floor": "400"})[0].mrids, ["XFMR-001"])

    def test_contains_a_written_number(self):
        found = execute(self.network, "FIND devices WHERE name CONTAINS 104").mrids
        self.assertEqual(found, ["BRK-104"])

    def test_contains_a_supplied_number(self):
        found = self.run_script("PARAM part\nFIND devices WHERE name CONTAINS $part", part="104")
        self.assertEqual(found, [["BRK-104"]])


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def script(self, source):
        return parse_script(source, "test.gridql")

    def test_a_supplied_value_replaces_the_reference(self):
        script = self.script('PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        bound = bind_script(script, {"feeder": "FDR-999"})
        self.assertEqual(bound.statements[0].relations[0].target, "FDR-999")
        self.assertEqual(bound.params, ())

    def test_the_default_applies_when_nothing_is_supplied(self):
        script = self.script('PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        self.assertEqual(bind_script(script).statements[0].relations[0].target, "FDR-104")

    def test_parameter_names_match_case_insensitively(self):
        script = self.script('PARAM Feeder = "FDR-104"\nFIND devices FED BY $feeder')
        bound = bind_script(script, {"FEEDER": "FDR-201"})
        self.assertEqual(bound.statements[0].relations[0].target, "FDR-201")

    def test_a_missing_required_parameter_names_the_flag(self):
        script = self.script("PARAM feeder\nFIND devices FED BY $feeder")
        with self.assertRaises(ParameterError) as raised:
            bind_script(script)
        self.assertIn("--feeder <value>", str(raised.exception))
        self.assertIn("test.gridql", str(raised.exception))

    def test_every_missing_parameter_is_reported_at_once(self):
        script = self.script("PARAM a\nPARAM b\nFIND devices WHERE mrid = $a OR mrid = $b")
        with self.assertRaises(ParameterError) as raised:
            bind_script(script)
        self.assertIn("a, b", str(raised.exception))

    def test_an_unknown_parameter_is_refused_with_a_suggestion(self):
        script = self.script('PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        with self.assertRaises(ParameterError) as raised:
            bind_script(script, {"feedr": "FDR-104"})
        self.assertIn("did you mean --feeder?", str(raised.exception))

    def test_a_file_with_no_parameters_takes_none(self):
        script = self.script("FIND devices")
        with self.assertRaises(ParameterError) as raised:
            bind_script(script, {"feeder": "FDR-104"})
        self.assertIn("declares no parameters", str(raised.exception))

    def test_resolve_reports_what_each_parameter_takes(self):
        script = self.script('PARAM feeder = "FDR-104"\nPARAM floor\nFIND devices FED BY $feeder')
        self.assertEqual(
            resolve(script, {"floor": "500kVA"}),
            {"feeder": Literal("FDR-104"), "floor": Quantity(500.0, "kVA")},
        )

    def test_a_bound_value_keeps_its_unit_for_conversion(self):
        script = self.script("PARAM floor = 0\nFIND transformers WHERE kva >= $floor")
        results = evaluate_script(self.network, script, {"floor": "0.4MVA"})
        self.assertEqual(results[0].mrids, ["XFMR-001"])

    def test_a_bound_value_is_a_value_not_an_attribute(self):
        # 'normal_state' is an attribute of switches, but a parameter is
        # always a value, so this compares against the literal text.
        script = self.script("PARAM position\nFIND switches WHERE state = $position")
        results = evaluate_script(self.network, script, {"position": "normal_state"})
        self.assertEqual(results[0].mrids, [])

    def test_a_limit_parameter_takes_a_whole_number(self):
        script = self.script("PARAM top = 1\nFIND devices LIMIT $top")
        self.assertEqual(len(evaluate_script(self.network, script, {"top": "2"})[0]), 2)

    def test_a_limit_that_is_not_a_count_is_refused(self):
        script = self.script("PARAM top = 1\nFIND devices LIMIT $top")
        with self.assertRaises(ParameterError) as raised:
            evaluate_script(self.network, script, {"top": "two"})
        self.assertIn("whole number of rows", str(raised.exception))

    def test_binding_reaches_every_statement(self):
        script = self.script(
            'PARAM feeder = "FDR-104"\n'
            "FIND transformers FED BY $feeder; FIND loads FED BY $feeder"
        )
        bound = bind_script(script, {"feeder": "FDR-201"})
        self.assertEqual([s.relations[0].target for s in bound.statements], ["FDR-201"] * 2)

    def test_binding_reaches_inside_and_or_not(self):
        script = self.script(
            "PARAM floor\nFIND transformers WHERE NOT (kva < $floor OR kva = $floor)"
        )
        results = evaluate_script(self.network, script, {"floor": "500"})
        self.assertEqual(results[0].mrids, [])

    def test_binding_reaches_an_in_list(self):
        script = self.script("PARAM one\nPARAM two\nFIND devices WHERE mrid IN ($one, $two)")
        results = evaluate_script(
            self.network, script, {"one": "REC-001", "two": "BRK-001"}
        )
        self.assertEqual(sorted(results[0].mrids), ["BRK-001", "REC-001"])

    def test_the_explained_query_shows_the_value_that_ran(self):
        script = self.script('PARAM feeder = "FDR-104"\nFIND devices FED BY $feeder')
        described = bind_script(script, {"feeder": "FDR-201"}).statements[0].describe()
        self.assertIn('FED BY "FDR-201"', described)
        self.assertNotIn("$feeder", described)


class RunFileTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "report.gridql")
        self.path.write_text(
            'PARAM feeder = "FDR-104"\n'
            "PARAM floor = 0\n"
            "FIND transformers FED BY $feeder WHERE kva >= $floor\n"
        )

    def test_defaults_run_without_any_values(self):
        self.assertEqual(run_file(self.network, self.path)[0].mrids, ["XFMR-001", "XFMR-002"])

    def test_a_value_changes_the_answer(self):
        results = run_file(self.network, self.path, {"floor": "500"})
        self.assertEqual(results[0].mrids, ["XFMR-001"])

    def test_the_cli_takes_a_parameter_as_its_own_option(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["run", str(self.path), "--floor", "500", "--no-config"])
        self.assertIn("XFMR-001", buffer.getvalue())
        self.assertNotIn("XFMR-002", buffer.getvalue())

    def test_the_cli_also_takes_param_name_value(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main(["run", str(self.path), "--param", "floor=500", "--no-config"])
        self.assertNotIn("XFMR-002", buffer.getvalue())

    def test_an_unknown_option_is_reported_as_a_parameter(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["run", str(self.path), "--flor", "500", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("did you mean --floor?", buffer.getvalue())

    def test_a_parameter_without_a_value_is_refused(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["run", str(self.path), "--floor", "--no-config"])
        self.assertEqual(code, 1)
        self.assertIn("needs a value", buffer.getvalue())

    def test_a_parameter_may_come_before_the_file(self):
        # argparse alone would read the file name as the unknown option's
        # value, so parameters are split out before it sees them.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["run", "--floor", "500", str(self.path), "--no-config"])
        self.assertEqual(code, 0)
        self.assertNotIn("XFMR-002", buffer.getvalue())


class ArgumentTests(unittest.TestCase):
    """Telling a file's parameters apart from gridql's own options."""

    def test_an_unknown_option_and_its_value_are_a_parameter(self):
        self.assertEqual(split_parameters(["report", "--feeder", "FDR-104"]),
                         (["report"], {"feeder": "FDR-104"}))

    def test_an_assignment_needs_no_second_token(self):
        self.assertEqual(split_parameters(["--feeder=FDR-104", "report"]),
                         (["report"], {"feeder": "FDR-104"}))

    def test_gridqls_own_options_keep_their_values(self):
        rest, values = split_parameters(
            ["report", "--db", "grid.sqlite", "--explain", "-f", "json"]
        )
        self.assertEqual(rest, ["report", "--db", "grid.sqlite", "--explain", "-f", "json"])
        self.assertEqual(values, {})

    def test_a_parameter_may_precede_the_file(self):
        self.assertEqual(split_parameters(["--feeder", "FDR-104", "report"]),
                         (["report"], {"feeder": "FDR-104"}))

    def test_a_negative_number_is_a_value_not_an_option(self):
        self.assertEqual(split_parameters(["report", "--floor", "-1"]),
                         (["report"], {"floor": "-1"}))

    def test_a_parameter_with_no_value_is_refused(self):
        with self.assertRaises(GridQLError) as raised:
            split_parameters(["report", "--floor"])
        self.assertIn("needs a value", str(raised.exception))

    def test_param_assignments_and_options_merge(self):
        self.assertEqual(
            parameters(["feeder=FDR-104", "floor=0"], {"floor": "500"}),
            {"feeder": "FDR-104", "floor": "500"},
        )

    def test_param_needs_name_and_value(self):
        with self.assertRaises(GridQLError):
            parameters(["feeder"], {})


if __name__ == "__main__":
    unittest.main()
