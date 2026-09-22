import unittest

from gridql.errors import GridQLSyntaxError
from gridql.lang import parse
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

    def test_errors(self):
        for source in ("", "feeders", "FIND", "FIND devices WHERE", "FIND devices DOWNSTREAM",
                       "FIND devices WHERE kva >=", "FIND devices WHERE (a", "FIND devices junk"):
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
