"""Recursive-descent parser for GridQL.

    script     := statement { ";" statement } [ ";" ]
    statement  := "FIND" type relation* [ "WHERE" expr ]
                  [ "SELECT" columns ] [ "RETURN" format ]
    columns    := "*" | IDENT { "," IDENT }
    relation   := ("DOWNSTREAM" | "UPSTREAM") "OF" target
                | "CONNECTED" "TO" target
                | "FED" "BY" target
    expr       := and_expr ( "OR" and_expr )*
    and_expr   := unary ( "AND" unary )*
    unary      := "NOT" unary | "(" expr ")" | predicate
    predicate  := IDENT [ op operand | "IN" "(" value {"," value} ")" | "CONTAINS" value ]
"""

from __future__ import annotations

from ..errors import GridQLSyntaxError
from .ast import (
    OUTPUT_FORMATS,
    And,
    Compare,
    Contains,
    In,
    Literal,
    Name,
    Node,
    Not,
    Or,
    Quantity,
    Query,
    Relation,
    Script,
    Truthy,
)
from .lexer import tokenize
from .tokens import Token, TokenKind

_COMPARISONS = {">=", "<=", "!=", "<>", "==", "=", ">", "<"}


def parse(source: str) -> Query:
    """Parse a single GridQL query.

    Raises if the source holds more than one statement -- use
    :func:`parse_script` for a .gridql file.
    """
    script = parse_script(source)
    if len(script) != 1:
        raise GridQLSyntaxError(
            f"expected a single query, found {len(script)}; use parse_script() "
            "or run the file with 'gridql run'",
            source,
            source.find(";"),
        )
    return script.statements[0]


def parse_script(source: str, path: str | None = None) -> Script:
    """Parse the statements of a .gridql file, separated by semicolons."""
    return _Parser(source).parse_script(path)


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens: list[Token] = tokenize(source)
        self.index = 0

    # -- token helpers --------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.current
        if token.kind is not TokenKind.EOF:
            self.index += 1
        return token

    def error(self, message: str, token: Token | None = None) -> GridQLSyntaxError:
        token = token or self.current
        return GridQLSyntaxError(message, self.source, token.position)

    def expect_keyword(self, word: str) -> Token:
        if not self.current.is_keyword(word):
            raise self.error(f"expected {word}, found {self.current.describe()}")
        return self.advance()

    # -- grammar --------------------------------------------------------

    def parse_script(self, path: str | None = None) -> Script:
        statements: list[Query] = []

        while True:
            while self.current.kind is TokenKind.SEMICOLON:
                self.advance()
            if self.current.kind is TokenKind.EOF:
                break
            statements.append(self.parse_query())
            if self.current.kind is TokenKind.SEMICOLON:
                continue
            if self.current.kind is not TokenKind.EOF:
                raise self.error(
                    f"unexpected {self.current.describe()} after query; "
                    "separate statements with ';'"
                )
            break

        if not statements:
            raise self.error("empty query; expected FIND")

        return Script(tuple(statements), self.source, path)

    def parse_query(self) -> Query:
        if self.current.kind is TokenKind.EOF:
            raise self.error("empty query; expected FIND")
        if not self.current.is_keyword("FIND"):
            raise self.error(f"expected FIND, found {self.current.describe()}")
        self.advance()

        if self.current.kind is not TokenKind.IDENT:
            raise self.error(
                f"expected a type after FIND, found {self.current.describe()}"
            )
        type_name = str(self.advance().value)

        relations: list[Relation] = []
        while self._at_relation():
            relations.append(self.parse_relation())

        where: Node | None = None
        if self.current.is_keyword("WHERE"):
            self.advance()
            where = self.parse_expression()

        select: tuple[str, ...] | None = None
        if self.current.is_keyword("SELECT"):
            self.advance()
            select = self.parse_columns()

        return_format: str | None = None
        if self.current.is_keyword("RETURN"):
            self.advance()
            return_format = self.parse_return_format()

        # Clauses are ordered, so say which one is out of place rather than
        # just pointing at an unexpected word.
        if self.current.is_keyword("FIND"):
            raise self.error("unexpected FIND; separate statements with ';'")
        if self.current.is_keyword("WHERE", "SELECT", "DOWNSTREAM", "UPSTREAM",
                                   "CONNECTED", "FED"):
            word = self.current.keyword
            raise self.error(
                f"{word} must come earlier in the query; the order is "
                "FIND, topology, WHERE, SELECT, RETURN"
            )

        return Query(
            type_name,
            tuple(relations),
            where,
            select,
            return_format,
            self.source,
        )

    def parse_columns(self) -> tuple[str, ...]:
        if self.current.kind is TokenKind.STAR:
            self.advance()
            return ("*",)

        columns: list[str] = []
        while True:
            token = self.current
            if token.kind is not TokenKind.IDENT:
                raise self.error(f"expected a column name, found {token.describe()}")
            self.advance()
            columns.append(str(token.value))
            if self.current.kind is not TokenKind.COMMA:
                break
            self.advance()
        return tuple(columns)

    def parse_return_format(self) -> str:
        token = self.current
        if token.kind not in (TokenKind.IDENT, TokenKind.STRING):
            raise self.error(f"expected an output format, found {token.describe()}")
        self.advance()
        name = str(token.value).lower()
        if name not in OUTPUT_FORMATS:
            raise GridQLSyntaxError(
                f"unknown output format '{token.value}'; "
                f"expected one of {', '.join(OUTPUT_FORMATS)}",
                self.source,
                token.position,
            )
        return name

    def _at_relation(self) -> bool:
        return self.current.is_keyword("DOWNSTREAM", "UPSTREAM", "CONNECTED", "FED")

    def parse_relation(self) -> Relation:
        token = self.advance()
        word = token.keyword or ""
        if word in ("DOWNSTREAM", "UPSTREAM"):
            self.expect_keyword("OF")
            kind = f"{word} OF"
        elif word == "CONNECTED":
            self.expect_keyword("TO")
            kind = "CONNECTED TO"
        else:
            self.expect_keyword("BY")
            kind = "FED BY"

        target = self.current
        if target.kind not in (TokenKind.STRING, TokenKind.IDENT):
            raise self.error(
                f"expected a device name after {kind}, found {target.describe()}"
            )
        self.advance()
        return Relation(kind, str(target.value), token.position)

    def parse_expression(self) -> Node:
        node = self.parse_and()
        while self.current.is_keyword("OR"):
            self.advance()
            node = Or(node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_unary()
        while self.current.is_keyword("AND"):
            self.advance()
            node = And(node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        if self.current.is_keyword("NOT"):
            self.advance()
            return Not(self.parse_unary())

        if self.current.kind is TokenKind.LPAREN:
            self.advance()
            node = self.parse_expression()
            if self.current.kind is not TokenKind.RPAREN:
                raise self.error(f"expected ), found {self.current.describe()}")
            self.advance()
            return node

        return self.parse_predicate()

    def parse_predicate(self) -> Node:
        token = self.current
        if token.kind is not TokenKind.IDENT:
            raise self.error(f"expected an attribute name, found {token.describe()}")
        attribute = str(self.advance().value)
        position = token.position

        if self.current.kind is TokenKind.OP:
            operator = str(self.advance().value)
            if operator not in _COMPARISONS:
                raise self.error(f"unknown operator '{operator}'")
            return Compare(attribute, _normalize(operator), self.parse_operand(), position)

        if self.current.is_keyword("IN"):
            self.advance()
            return In(attribute, self.parse_operand_list(), position)

        if self.current.is_keyword("CONTAINS", "LIKE"):
            self.advance()
            return Contains(attribute, self.parse_operand(), position)

        return Truthy(attribute, position)

    def parse_operand_list(self) -> tuple[Node, ...]:
        if self.current.kind is not TokenKind.LPAREN:
            raise self.error(f"expected ( after IN, found {self.current.describe()}")
        self.advance()

        operands = [self.parse_operand()]
        while self.current.kind is TokenKind.COMMA:
            self.advance()
            operands.append(self.parse_operand())

        if self.current.kind is not TokenKind.RPAREN:
            raise self.error(f"expected ) to close IN list, found {self.current.describe()}")
        self.advance()
        return tuple(operands)

    def parse_operand(self) -> Node:
        token = self.current
        if token.kind is TokenKind.STRING:
            self.advance()
            return Literal(str(token.value))
        if token.kind is TokenKind.NUMBER:
            self.advance()
            return Quantity(float(token.value), token.unit)
        if token.kind is TokenKind.IDENT:
            self.advance()
            if token.is_keyword("TRUE"):
                return Literal(True)
            if token.is_keyword("FALSE"):
                return Literal(False)
            return Name(str(token.value))
        raise self.error(f"expected a value, found {token.describe()}")


def _normalize(operator: str) -> str:
    if operator == "==":
        return "="
    if operator == "<>":
        return "!="
    return operator
