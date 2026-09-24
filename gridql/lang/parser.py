# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recursive-descent parser for GridQL.

    script     := param* statement { ";" statement } [ ";" ]
    param      := "PARAM" IDENT [ "=" value ] [ ";" ]
    statement  := "FIND" type relation* [ "WHERE" expr ] [ "SELECT" columns ]
                  [ "GROUP" "BY" IDENT {"," IDENT} ] [ "ORDER" "BY" sortkeys ]
                  [ "LIMIT" NUMBER ] [ "RETURN" format ]
    columns    := "*" | item { "," item }
    item       := IDENT | aggregate
    aggregate  := ("COUNT"|"SUM"|"AVG"|"MIN"|"MAX") "(" (IDENT | "*") ")"
    sortkeys   := item ["ASC"|"DESC"] { "," item ["ASC"|"DESC"] }
    relation   := ("DOWNSTREAM" | "UPSTREAM") "OF" target
                | "CONNECTED" "TO" target
                | "FED" "BY" target
    expr       := and_expr ( "OR" and_expr )*
    and_expr   := unary ( "AND" unary )*
    unary      := "NOT" unary | "(" expr ")" | predicate
    predicate  := IDENT [ op operand | "IN" "(" value {"," value} ")"
                        | "CONTAINS" value | "LIKE" value ]
    operand    := STRING | NUMBER | IDENT | "$" IDENT

A ``$name`` stands wherever a value stands -- a relation target, an operand, a
LIMIT -- and every one of them must be declared by a PARAM at the head of the
file, so a typo is a parse error rather than an empty result.
"""

from __future__ import annotations

import difflib

from ..errors import GridQLSyntaxError
from .ast import (
    AGGREGATE_FUNCTIONS,
    OUTPUT_FORMATS,
    And,
    Compare,
    Contains,
    In,
    Like,
    Literal,
    Name,
    Node,
    Not,
    Or,
    Param,
    ParamRef,
    Quantity,
    Query,
    Relation,
    Script,
    SelectItem,
    SortKey,
    Truthy,
)
from .lexer import tokenize
from .tokens import KEYWORDS, Token, TokenKind

_COMPARISONS = {">=", "<=", "!=", "<>", "==", "=", ">", "<"}


def parse(source: str) -> Query:
    """Parse a single GridQL query.

    Raises if the source holds more than one statement -- use
    :func:`parse_script` for a .gridql file.
    """
    script = parse_script(source)
    if script.params:
        first = script.params[0]
        raise GridQLSyntaxError(
            "PARAM declarations belong in a .gridql file run with 'gridql run'; "
            "write the value directly in a one-off query",
            source,
            first.position,
        )
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
        #: Lowercased names of the parameters declared so far.
        self.params_in_scope: set[str] = set()

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
        params: list[Param] = []

        while True:
            while self.current.kind is TokenKind.SEMICOLON:
                self.advance()
            if self.current.kind is TokenKind.EOF:
                break
            if self.current.is_keyword("PARAM"):
                # Declarations head the file: every statement below may use
                # them, and the parser can reject an undeclared $name.
                if statements:
                    raise self.error(
                        "PARAM declarations must come before the first FIND, "
                        "so that every statement in the file can use them"
                    )
                params.append(self.parse_param())
                continue
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
            if params:
                raise self.error(
                    "this file declares parameters but asks nothing; expected FIND"
                )
            raise self.error("empty query; expected FIND")

        return Script(tuple(statements), tuple(params), self.source, path)

    def parse_param(self) -> Param:
        """``PARAM feeder`` or ``PARAM feeder = "FDR-104"``.

        The name may be written ``$feeder`` too, since that is how every
        other line of the file spells it.
        """
        keyword = self.advance()

        token = self.current
        if token.kind not in (TokenKind.IDENT, TokenKind.PARAM):
            raise self.error(
                f"expected a parameter name after PARAM, found {token.describe()}"
            )
        if token.kind is TokenKind.IDENT and token.keyword in KEYWORDS:
            raise self.error(
                f"'{token.value}' is a GridQL keyword and cannot name a parameter", token
            )
        self.advance()
        name = str(token.value)

        if name.lower() in self.params_in_scope:
            raise self.error(f"parameter '{name}' is declared twice", token)

        default: Node | None = None
        if self.current.kind is TokenKind.OP and self.current.value in ("=", "=="):
            self.advance()
            default = self.parse_default(name)

        self.params_in_scope.add(name.lower())
        return Param(name, default, keyword.position)

    def parse_default(self, name: str) -> Node:
        """A parameter default, which is always a value and never an attribute."""
        token = self.current
        if token.kind is TokenKind.STRING:
            self.advance()
            return Literal(str(token.value))
        if token.kind is TokenKind.NUMBER:
            self.advance()
            # Kept with its spelling: a default is a supplied value like any
            # other, so PARAM feeder = 0412 names device 0412, not 412.
            return Quantity(float(token.value), token.unit, token.text)
        if token.kind is TokenKind.IDENT:
            self.advance()
            if token.is_keyword("TRUE"):
                return Literal(True)
            if token.is_keyword("FALSE"):
                return Literal(False)
            # A bare word here is a value: a default cannot name an attribute,
            # since there is no equipment in hand when it is resolved.
            return Literal(str(token.value))
        raise self.error(
            f"expected a default value for '{name}', found {token.describe()}"
        )

    def parse_param_ref(self) -> ParamRef:
        """``$feeder``, checked against the declarations above it."""
        token = self.advance()
        name = str(token.value)
        if name.lower() not in self.params_in_scope:
            known = sorted(self.params_in_scope)
            hint = (
                f"; this file declares {', '.join('$' + p for p in known)}"
                if known
                else "; declare it with PARAM at the top of the file"
            )
            suggestions = difflib.get_close_matches(name.lower(), known, n=2, cutoff=0.6)
            if suggestions:
                hint = f"; did you mean {', '.join('$' + s for s in suggestions)}?"
            raise self.error(f"no parameter named '${name}'{hint}", token)
        return ParamRef(name, token.position)

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

        group_by: tuple[str, ...] | None = None
        if self.current.is_keyword("GROUP"):
            self.advance()
            self.expect_keyword("BY")
            group_by = self.parse_group_by()

        order_by: tuple[SortKey, ...] | None = None
        if self.current.is_keyword("ORDER"):
            self.advance()
            self.expect_keyword("BY")
            order_by = self.parse_order_by()

        limit: int | ParamRef | None = None
        if self.current.is_keyword("LIMIT"):
            self.advance()
            limit = self.parse_limit()

        return_format: str | None = None
        if self.current.is_keyword("RETURN"):
            self.advance()
            return_format = self.parse_return_format()

        # Clauses are ordered, so say which one is out of place rather than
        # just pointing at an unexpected word.
        if self.current.is_keyword("FIND"):
            raise self.error("unexpected FIND; separate statements with ';'")
        if self.current.is_keyword("WHERE", "SELECT", "GROUP", "ORDER", "LIMIT",
                                   "DOWNSTREAM", "UPSTREAM", "CONNECTED", "FED"):
            word = self.current.keyword
            raise self.error(
                f"{word} must come earlier in the query; the order is "
                "FIND, topology, WHERE, SELECT, GROUP BY, ORDER BY, LIMIT, RETURN"
            )

        return Query(
            type_name,
            tuple(relations),
            where,
            select,
            group_by,
            order_by,
            limit,
            return_format,
            self.source,
        )

    def parse_columns(self) -> tuple[SelectItem, ...]:
        if self.current.kind is TokenKind.STAR:
            star = self.advance()
            if self.current.kind is TokenKind.COMMA:
                raise self.error(
                    "SELECT * must stand alone; name the columns you want instead", star
                )
            return (SelectItem("*", "*"),)

        columns = [self.parse_select_item()]
        while self.current.kind is TokenKind.COMMA:
            self.advance()
            columns.append(self.parse_select_item())
        return tuple(columns)

    def parse_select_item(self) -> SelectItem:
        token = self.current
        if token.kind is not TokenKind.IDENT:
            raise self.error(f"expected a column name, found {token.describe()}")
        self.advance()
        name = str(token.value)

        if self.current.kind is not TokenKind.LPAREN:
            return SelectItem(name, name)

        # An attribute name followed by '(' is an aggregate call.
        function = name.lower()
        if function not in AGGREGATE_FUNCTIONS:
            raise self.error(
                f"unknown function '{name}'; expected one of "
                f"{', '.join(f.upper() for f in AGGREGATE_FUNCTIONS)}",
                token,
            )
        self.advance()

        if self.current.kind is TokenKind.STAR:
            self.advance()
            argument = "*"
            if function != "count":
                raise self.error(f"{name}(*) is not meaningful; {name} needs an attribute")
        elif self.current.kind is TokenKind.IDENT:
            argument = str(self.advance().value)
        else:
            raise self.error(
                f"expected an attribute inside {name}(), found {self.current.describe()}"
            )

        if self.current.kind is not TokenKind.RPAREN:
            raise self.error(f"expected ) to close {name}(, found {self.current.describe()}")
        self.advance()

        return SelectItem(f"{name}({argument})", argument, function)

    def parse_group_by(self) -> tuple[str, ...]:
        columns: list[str] = []
        while True:
            token = self.current
            if token.kind is not TokenKind.IDENT:
                raise self.error(f"expected a column to group by, found {token.describe()}")
            self.advance()
            columns.append(str(token.value))
            if self.current.kind is not TokenKind.COMMA:
                break
            self.advance()
        return tuple(columns)

    def parse_order_by(self) -> tuple[SortKey, ...]:
        keys = [self.parse_sort_key()]
        while self.current.kind is TokenKind.COMMA:
            self.advance()
            keys.append(self.parse_sort_key())
        return tuple(keys)

    def parse_sort_key(self) -> SortKey:
        item = self.parse_select_item()
        descending = False
        if self.current.is_keyword("DESC"):
            self.advance()
            descending = True
        elif self.current.is_keyword("ASC"):
            self.advance()
        return SortKey(item, descending)

    def parse_limit(self) -> int | ParamRef:
        token = self.current
        if token.kind is TokenKind.PARAM:
            return self.parse_param_ref()
        if token.kind is not TokenKind.NUMBER:
            raise self.error(f"expected a row count after LIMIT, found {token.describe()}")
        self.advance()
        value = float(token.value)
        if value < 0 or value != int(value):
            raise self.error("LIMIT takes a whole number of rows, zero or more", token)
        return int(value)

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
        if target.kind is TokenKind.PARAM:
            return Relation(kind, self.parse_param_ref(), token.position)
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

        if self.current.is_keyword("CONTAINS"):
            self.advance()
            return Contains(attribute, self.parse_operand(), position)

        if self.current.is_keyword("LIKE"):
            self.advance()
            return Like(attribute, self.parse_operand(), position)

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
        if token.kind is TokenKind.PARAM:
            return self.parse_param_ref()
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
