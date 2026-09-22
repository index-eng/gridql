import unittest

from gridql.errors import GridQLSyntaxError
from gridql.lang import parse, parse_script
from gridql.lang.ast import And, Compare, Contains, In, Literal, Name, Not, Or, Quantity, Truthy


class ParserTests(unittest.TestCase):
    def test_bare_find(self):
        query = parse("FIND feeders")
        self.assertEqual(query.type_name, "feeders")
        self.assertEqual(query.relations, ())
        self.assertIsNone(query.where)

    def test_relations(self):
        cases = {
            'FIND devices DOWNSTREAM OF "REC-001"': ("DOWNSTREAM OF", "downstream_of"),
            'FIND devices UPSTREAM OF "REC-001"': ("UPSTREAM OF", "upstream_of"),
            'FIND devices CONNECTED TO "REC-001"': ("CONNECTED TO", "connected_to"),
            'FIND devices FED BY "FDR-104"': ("FED BY", "fed_by"),
        }
        for source, (kind, method) in cases.items():
            relation = parse(source).relations[0]
            self.assertEqual((relation.kind, relation.method), (kind, method), source)
            self.assertEqual(relation.target, relation.target.upper())

    def test_unquoted_target(self):
        self.assertEqual(parse("FIND devices DOWNSTREAM OF REC-001").relations[0].target, "REC-001")

    def test_relations_stack(self):
        query = parse('FIND switches DOWNSTREAM OF "REC-001" FED BY "FDR-104"')
        self.assertEqual([r.kind for r in query.relations], ["DOWNSTREAM OF", "FED BY"])

    def test_not_binds_tighter_than_and_which_binds_tighter_than_or(self):
        where = parse("FIND devices WHERE NOT a AND b OR c").where
        self.assertIsInstance(where, Or)
        self.assertIsInstance(where.left, And)
        self.assertIsInstance(where.left.left, Not)
        self.assertIsInstance(where.right, Truthy)

    def test_parentheses_override_precedence(self):
        where = parse("FIND devices WHERE a AND (b OR c)").where
        self.assertIsInstance(where, And)
        self.assertIsInstance(where.right, Or)

    def test_quantity_operand_keeps_unit(self):
        where = parse("FIND transformers WHERE kva >= 0.5MVA").where
        self.assertEqual(where.operand, Quantity(0.5, "MVA"))

    def test_bare_word_operand_stays_a_name(self):
        self.assertEqual(parse("FIND switches WHERE state != normal_state").where.operand,
                         Name("normal_state"))

    def test_quoted_operand_is_a_literal(self):
        self.assertEqual(parse('FIND switches WHERE state = "OPEN"').where.operand,
                         Literal("OPEN"))

    def test_booleans_are_literals(self):
        self.assertEqual(parse("FIND devices WHERE energized = TRUE").where.operand, Literal(True))

    def test_operator_synonyms_normalize(self):
        self.assertEqual(parse("FIND devices WHERE a <> b").where.operator, "!=")
        self.assertEqual(parse("FIND devices WHERE a == b").where.operator, "=")

    def test_in_list(self):
        where = parse('FIND switches WHERE state IN (OPEN, "CLOSED")').where
        self.assertIsInstance(where, In)
        self.assertEqual(len(where.operands), 2)

    def test_contains(self):
        self.assertIsInstance(parse("FIND devices WHERE phases CONTAINS A").where, Contains)

    def test_describe_round_trips_structure(self):
        source = 'FIND transformers DOWNSTREAM OF "REC-001" WHERE kva >= 500'
        self.assertEqual(parse(parse(source).describe()), parse(source))

    def test_select_columns(self):
        query = parse("FIND transformers SELECT name, mRID, kva")
        self.assertEqual([i.written for i in query.select], ["name", "mRID", "kva"])
        self.assertEqual([i.attribute for i in query.select], ["name", "mRID", "kva"])
        self.assertFalse(any(i.is_aggregate for i in query.select))

    def test_select_star(self):
        starred = parse("FIND devices SELECT *").select
        self.assertEqual([i.attribute for i in starred], ["*"])

    def test_no_select_is_none(self):
        self.assertIsNone(parse("FIND devices").select)

    def test_return_format(self):
        self.assertEqual(parse("FIND devices RETURN json").return_format, "json")
        self.assertEqual(parse("FIND devices RETURN CSV").return_format, "csv")
        self.assertIsNone(parse("FIND devices").return_format)

    def test_unknown_return_format(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices RETURN xml")
        self.assertIn("table, json, csv", str(raised.exception))

    def test_full_clause_order(self):
        query = parse(
            'FIND transformers DOWNSTREAM OF "FDR-104" WHERE kva >= 500 '
            "SELECT name, kva RETURN csv"
        )
        self.assertEqual(query.type_name, "transformers")
        self.assertEqual(len(query.relations), 1)
        self.assertIsNotNone(query.where)
        self.assertEqual([i.written for i in query.select], ["name", "kva"])
        self.assertEqual(query.return_format, "csv")

    def test_clauses_out_of_order_say_so(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices SELECT name WHERE kva >= 500")
        self.assertIn("must come earlier", str(raised.exception))

    def test_describe_round_trips_select_and_return(self):
        source = "FIND transformers WHERE kva >= 500 SELECT name, kva RETURN json"
        self.assertEqual(parse(parse(source).describe()), parse(source))

    def test_script_splits_on_semicolons(self):
        script = parse_script("FIND feeders; FIND reclosers;")
        self.assertEqual([s.type_name for s in script], ["feeders", "reclosers"])

    def test_trailing_and_repeated_semicolons_are_tolerated(self):
        self.assertEqual(len(parse_script("FIND feeders ;; FIND reclosers ;")), 2)

    def test_parse_rejects_a_multi_statement_source(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND feeders; FIND reclosers")
        self.assertIn("single query", str(raised.exception))

    def test_two_statements_without_a_separator(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse_script("FIND feeders FIND reclosers")
        self.assertIn("';'", str(raised.exception))

    def test_comments_between_statements(self):
        script = parse_script("-- first\nFIND feeders;\n# second\nFIND reclosers")
        self.assertEqual(len(script), 2)

    def test_errors(self):
        for source in ("", "feeders", "FIND", "FIND devices WHERE", "FIND devices DOWNSTREAM",
                       "FIND devices WHERE kva >=", "FIND devices WHERE (a", "FIND devices junk",
                       "FIND devices SELECT", "FIND devices SELECT name,", "FIND devices RETURN"):
            with self.subTest(source=source), self.assertRaises(GridQLSyntaxError):
                parse(source)

    def test_error_points_at_the_problem(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse("FIND devices WHERE kva >= ")
        rendered = raised.exception.render()
        self.assertIn("^", rendered)
        self.assertIn("line 1", rendered)


if __name__ == "__main__":
    unittest.main()
