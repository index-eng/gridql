# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The GridQL abstract syntax tree.

Nodes stay deliberately dumb: they record what was written, and the evaluator
decides what it means against a particular network. That split is what lets a
bare word on the right of a comparison be read as an attribute on one type and
as a literal value on another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Node:
    """Base class for every AST node."""


# -- operands -----------------------------------------------------------


@dataclass(frozen=True)
class Literal(Node):
    """A quoted string or a boolean -- always a value, never an attribute."""

    value: Any

    def describe(self) -> str:
        return repr(self.value)


@dataclass(frozen=True)
class Quantity(Node):
    """A number, optionally carrying the unit it was written with.

    ``text`` is the value exactly as a parameter supplied it (``0412``,
    ``12A``). It is kept because a supplied value is only a number when the
    attribute it meets is one -- used as a device name, ``0412`` must not
    become ``412``.
    """

    value: float
    unit: str | None = None
    text: str | None = field(default=None, compare=False)

    def describe(self) -> str:
        return f"{self.value}{self.unit or ''}"


@dataclass(frozen=True)
class Name(Node):
    """A bare word: an attribute reference if one exists, else a value."""

    name: str

    def describe(self) -> str:
        return self.name


@dataclass(frozen=True)
class ParamRef(Node):
    """``$feeder``: a value the query does not know until it is run."""

    name: str
    position: int = 0

    def describe(self) -> str:
        return f"${self.name}"


@dataclass(frozen=True)
class Param(Node):
    """A ``PARAM`` declaration at the head of a .gridql file.

    A parameter with no default is required, so running the file without it
    is refused rather than answered against whatever the file last named.
    """

    name: str
    default: Node | None = None
    position: int = 0

    @property
    def required(self) -> bool:
        return self.default is None

    def describe(self) -> str:
        if self.default is None:
            return f"PARAM {self.name}"
        return f"PARAM {self.name} = {source_text(self.default)}"


def source_text(node: Node) -> str:
    """A value node written the way a query would write it.

    ``describe`` renders for diagnostics -- this renders for re-reading, so a
    described script parses back to the same thing.
    """
    if isinstance(node, Literal):
        if isinstance(node.value, bool):
            return "TRUE" if node.value else "FALSE"
        return '"{}"'.format(str(node.value).replace('"', '\\"'))
    return node.describe()


# -- predicates ---------------------------------------------------------


@dataclass(frozen=True)
class Compare(Node):
    attribute: str
    operator: str
    operand: Node
    position: int = 0

    def describe(self) -> str:
        return f"{self.attribute} {self.operator} {self.operand.describe()}"


@dataclass(frozen=True)
class In(Node):
    attribute: str
    operands: tuple[Node, ...]
    position: int = 0

    def describe(self) -> str:
        return f"{self.attribute} IN ({', '.join(o.describe() for o in self.operands)})"


@dataclass(frozen=True)
class Contains(Node):
    attribute: str
    operand: Node
    position: int = 0

    def describe(self) -> str:
        return f"{self.attribute} CONTAINS {self.operand.describe()}"


@dataclass(frozen=True)
class Like(Node):
    """A pattern the whole value must match: ``%`` is any run, ``_`` one character."""

    attribute: str
    operand: Node
    position: int = 0

    def describe(self) -> str:
        return f"{self.attribute} LIKE {self.operand.describe()}"


@dataclass(frozen=True)
class Truthy(Node):
    """A bare attribute used as a test, as in ``WHERE energized``."""

    attribute: str
    position: int = 0

    def describe(self) -> str:
        return self.attribute


@dataclass(frozen=True)
class And(Node):
    left: Node
    right: Node

    def describe(self) -> str:
        return f"({self.left.describe()} AND {self.right.describe()})"


@dataclass(frozen=True)
class Or(Node):
    left: Node
    right: Node

    def describe(self) -> str:
        return f"({self.left.describe()} OR {self.right.describe()})"


@dataclass(frozen=True)
class Not(Node):
    operand: Node

    def describe(self) -> str:
        return f"NOT {self.operand.describe()}"


# -- query --------------------------------------------------------------

#: Relation keyword -> the Network method that answers it.
RELATION_METHODS = {
    "DOWNSTREAM OF": "downstream_of",
    "UPSTREAM OF": "upstream_of",
    "CONNECTED TO": "connected_to",
    "FED BY": "fed_by",
    "PROTECTED BY": "protected_by",
}


@dataclass(frozen=True)
class Relation(Node):
    """A topology constraint, such as ``DOWNSTREAM OF "REC-001"``.

    The target is a device name, or a :class:`ParamRef` until the script it
    came from is bound.
    """

    kind: str
    target: str | ParamRef
    position: int = 0

    @property
    def method(self) -> str:
        return RELATION_METHODS[self.kind]

    def describe(self) -> str:
        if isinstance(self.target, ParamRef):
            return f"{self.kind} {self.target.describe()}"
        return f'{self.kind} "{self.target}"'


#: Output formats a RETURN clause may name.
OUTPUT_FORMATS = ("table", "json", "csv", "cim")

#: Aggregate functions. COUNT takes ``*`` as well as an attribute; the rest
#: need something to add up.
AGGREGATE_FUNCTIONS = ("count", "sum", "avg", "min", "max")


@dataclass(frozen=True)
class SelectItem(Node):
    """One entry in a SELECT list: an attribute, or an aggregate of one.

    ``written`` keeps the spelling the query used, so ``SUM(kva)`` heads its
    column as ``SUM(kva)`` and ``mRID`` as ``mRID``, while lookup stays
    case-insensitive.
    """

    written: str
    attribute: str
    function: str | None = None

    @property
    def is_aggregate(self) -> bool:
        return self.function is not None

    def describe(self) -> str:
        return self.written


@dataclass(frozen=True)
class SortKey(Node):
    """One entry in an ORDER BY list."""

    item: SelectItem
    descending: bool = False

    def describe(self) -> str:
        return f"{self.item.describe()}{' DESC' if self.descending else ''}"


@dataclass(frozen=True)
class Query(Node):
    type_name: str
    relations: tuple[Relation, ...] = ()
    where: Node | None = None
    select: tuple[SelectItem, ...] | None = None
    group_by: tuple[str, ...] | None = None
    order_by: tuple[SortKey, ...] | None = None
    limit: int | ParamRef | None = None
    return_format: str | None = None
    source: str = field(default="", compare=False)

    @property
    def is_aggregate(self) -> bool:
        """True when the query answers with computed rows rather than equipment."""
        if self.group_by is not None:
            return True
        return any(item.is_aggregate for item in self.select or ())

    def describe(self) -> str:
        parts = [f"FIND {self.type_name}"]
        parts.extend(relation.describe() for relation in self.relations)
        if self.where is not None:
            parts.append(f"WHERE {self.where.describe()}")
        if self.select is not None:
            parts.append(f"SELECT {', '.join(i.describe() for i in self.select)}")
        if self.group_by is not None:
            parts.append(f"GROUP BY {', '.join(self.group_by)}")
        if self.order_by is not None:
            parts.append(f"ORDER BY {', '.join(k.describe() for k in self.order_by)}")
        if self.limit is not None:
            limit = self.limit
            parts.append(
                f"LIMIT {limit.describe() if isinstance(limit, ParamRef) else limit}"
            )
        if self.return_format is not None:
            parts.append(f"RETURN {self.return_format}")
        return "\n".join(parts)


@dataclass(frozen=True)
class Script(Node):
    """The statements of one .gridql file, in the order they will run."""

    statements: tuple[Query, ...]
    params: tuple[Param, ...] = ()
    source: str = field(default="", compare=False)
    path: str | None = field(default=None, compare=False)

    def __iter__(self):
        return iter(self.statements)

    def __len__(self) -> int:
        return len(self.statements)

    def param(self, name: str) -> Param | None:
        """The declaration of ``name``, matched case-insensitively."""
        wanted = name.lower()
        return next((p for p in self.params if p.name.lower() == wanted), None)

    @property
    def required_params(self) -> tuple[Param, ...]:
        return tuple(p for p in self.params if p.required)

    def describe(self) -> str:
        body = ";\n\n".join(statement.describe() for statement in self.statements)
        if not self.params:
            return body
        declarations = "\n".join(p.describe() for p in self.params)
        return f"{declarations}\n\n{body}"
