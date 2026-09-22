"""Token definitions for the GridQL lexer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TokenKind(Enum):
    IDENT = "ident"
    STRING = "string"
    NUMBER = "number"
    OP = "op"
    LPAREN = "("
    RPAREN = ")"
    COMMA = ","
    STAR = "*"
    SEMICOLON = ";"
    EOF = "end of query"


#: Words the parser treats as grammar rather than as identifiers. They are
#: matched case-insensitively, so FIND, find and Find are the same token.
KEYWORDS = frozenset(
    {
        "FIND",
        "WHERE",
        "SELECT",
        "RETURN",
        "GROUP",
        "ORDER",
        "LIMIT",
        "ASC",
        "DESC",
        "AND",
        "OR",
        "NOT",
        "IN",
        "CONTAINS",
        "LIKE",
        "DOWNSTREAM",
        "UPSTREAM",
        "CONNECTED",
        "FED",
        "OF",
        "TO",
        "BY",
        "TRUE",
        "FALSE",
    }
)

#: Comparison operators, longest first so the lexer matches ">=" before ">".
OPERATORS = (">=", "<=", "!=", "<>", "==", "=", ">", "<")


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: Any
    position: int
    unit: str | None = None

    @property
    def keyword(self) -> str | None:
        """The uppercased word, when this token is a bare identifier."""
        if self.kind is TokenKind.IDENT:
            return str(self.value).upper()
        return None

    def is_keyword(self, *words: str) -> bool:
        return self.keyword in words

    def describe(self) -> str:
        if self.kind is TokenKind.EOF:
            return "end of query"
        if self.kind is TokenKind.STRING:
            return f'string "{self.value}"'
        return f"'{self.value}'"
