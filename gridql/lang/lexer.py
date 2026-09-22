# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn GridQL source text into tokens.

Two details matter for a utility-facing language:

* device identifiers like ``REC-104-01`` are single identifiers, hyphens and
  all -- the language has no arithmetic, so a hyphen inside a word is never
  ambiguous;
* numbers may carry a unit (``13.8kV``, ``0.5MVA``), which is kept on the
  token and resolved against the attribute being compared;
* ``$feeder`` is a parameter reference, so a saved query can be run against
  whichever circuit the engineer names today.
"""

from __future__ import annotations

from ..errors import GridQLSyntaxError
from .tokens import OPERATORS, Token, TokenKind

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_BODY = _IDENT_START | set("0123456789")
_DIGITS = set("0123456789")


def tokenize(source: str) -> list[Token]:
    """Lex ``source`` into a token list terminated by an EOF token."""
    tokens: list[Token] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char.isspace():
            index += 1
            continue

        # Comments: -- to end of line, or #.
        if char == "#" or (char == "-" and source.startswith("--", index)):
            newline = source.find("\n", index)
            index = length if newline == -1 else newline
            continue

        start = index

        if char == "(":
            tokens.append(Token(TokenKind.LPAREN, "(", start))
            index += 1
            continue
        if char == ")":
            tokens.append(Token(TokenKind.RPAREN, ")", start))
            index += 1
            continue
        if char == ",":
            tokens.append(Token(TokenKind.COMMA, ",", start))
            index += 1
            continue
        if char == "*":
            tokens.append(Token(TokenKind.STAR, "*", start))
            index += 1
            continue
        if char == ";":
            tokens.append(Token(TokenKind.SEMICOLON, ";", start))
            index += 1
            continue

        if char in "\"'":
            value, index = _read_string(source, index)
            tokens.append(Token(TokenKind.STRING, value, start))
            continue

        # $feeder -- a reference to a PARAM declared at the head of the file.
        if char == "$":
            if index + 1 >= length or source[index + 1] not in _IDENT_START:
                raise GridQLSyntaxError("expected a parameter name after '$'", source, start)
            word, index = _read_ident(source, index + 1)
            tokens.append(Token(TokenKind.PARAM, word, start))
            continue

        # A number, or a negative number. A '-' followed by a digit can only
        # be a sign here, since identifiers never start with one.
        if char in _DIGITS or (char == "-" and index + 1 < length and source[index + 1] in _DIGITS):
            value, unit, index = _read_number(source, index)
            tokens.append(Token(TokenKind.NUMBER, value, start, unit))
            continue

        if char in _IDENT_START:
            word, index = _read_ident(source, index)
            tokens.append(Token(TokenKind.IDENT, word, start))
            continue

        operator = next((op for op in OPERATORS if source.startswith(op, index)), None)
        if operator is not None:
            tokens.append(Token(TokenKind.OP, operator, start))
            index += len(operator)
            continue

        raise GridQLSyntaxError(f"unexpected character '{char}'", source, index)

    tokens.append(Token(TokenKind.EOF, None, length))
    return tokens


def _read_string(source: str, index: int) -> tuple[str, int]:
    quote = source[index]
    start = index
    index += 1
    chunks: list[str] = []
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            chunks.append(source[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    raise GridQLSyntaxError("unterminated string", source, start)


def _read_number(source: str, index: int) -> tuple[float, str | None, int]:
    start = index
    if source[index] == "-":
        index += 1
    while index < len(source) and source[index] in _DIGITS:
        index += 1
    if index < len(source) and source[index] == "." and index + 1 < len(source) and source[index + 1] in _DIGITS:
        index += 1
        while index < len(source) and source[index] in _DIGITS:
            index += 1

    value = float(source[start:index])

    # A unit suffix must be glued to the number: 500kVA, not 500 kVA.
    unit_start = index
    while index < len(source) and source[index].isalpha():
        index += 1
    unit = source[unit_start:index] or None

    return value, unit, index


def _read_ident(source: str, index: int) -> tuple[str, int]:
    start = index
    index += 1
    while index < len(source):
        char = source[index]
        if char in _IDENT_BODY:
            index += 1
            continue
        # Hyphens and dots join identifiers like REC-104-01 or bus.1, but only
        # when another word character follows.
        if char in "-." and index + 1 < len(source) and source[index + 1] in _IDENT_BODY:
            index += 2
            continue
        break
    return source[start:index], index
