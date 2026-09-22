import unittest

from gridql.errors import GridQLSyntaxError
from gridql.lang.lexer import tokenize
from gridql.lang.tokens import TokenKind


def kinds_and_values(source):
    return [(t.kind, t.value) for t in tokenize(source)[:-1]]


class LexerTests(unittest.TestCase):
    def test_ends_with_eof(self):
        self.assertIs(tokenize("FIND feeders")[-1].kind, TokenKind.EOF)

    def test_keywords_are_case_insensitive(self):
        for source in ("FIND feeders", "find feeders", "Find Feeders"):
            self.assertTrue(tokenize(source)[0].is_keyword("FIND"), source)

    def test_number_keeps_its_unit(self):
        token = tokenize("13.8kV")[0]
        self.assertIs(token.kind, TokenKind.NUMBER)
        self.assertEqual(token.value, 13.8)
        self.assertEqual(token.unit, "kV")

    def test_bare_number_has_no_unit(self):
        self.assertIsNone(tokenize("500")[0].unit)

    def test_negative_number(self):
        self.assertEqual(tokenize("-12.5")[0].value, -12.5)

    def test_hyphenated_identifier_is_one_token(self):
        self.assertEqual(kinds_and_values("REC-104-01"), [(TokenKind.IDENT, "REC-104-01")])

    def test_operators(self):
        self.assertEqual(
            [t.value for t in tokenize("a >= b != c < d")[:-1]],
            ["a", ">=", "b", "!=", "c", "<", "d"],
        )

    def test_strings_and_escapes(self):
        self.assertEqual(tokenize('"REC-001"')[0].value, "REC-001")
        self.assertEqual(tokenize("'REC-001'")[0].value, "REC-001")
        self.assertEqual(tokenize(r'"say \"hi\""')[0].value, 'say "hi"')

    def test_comments_are_skipped(self):
        self.assertEqual(kinds_and_values("FIND feeders -- a comment\n# another"),
                         [(TokenKind.IDENT, "FIND"), (TokenKind.IDENT, "feeders")])

    def test_unterminated_string_reports_its_position(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            tokenize('FIND devices WHERE name = "oops')
        self.assertEqual(raised.exception.position, 26)

    def test_unexpected_character(self):
        with self.assertRaises(GridQLSyntaxError):
            tokenize("FIND devices WHERE a @ b")


if __name__ == "__main__":
    unittest.main()
