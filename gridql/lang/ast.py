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
    """A number, optionally carrying the unit it was written with."""

    value: float
    unit: str | None = None

    def describe(self) -> str:
        return f"{self.value}{self.unit or ''}"


@dataclass(frozen=True)
class Name(Node):
    """A bare word: an attribute reference if one exists, else a value."""

    name: str

    def describe(self) -> str:
        return self.name


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
}


@dataclass(frozen=True)
class Relation(Node):
    """A topology constraint, such as ``DOWNSTREAM OF "REC-001"``."""

    kind: str
    target: str
    position: int = 0

    @property
    def method(self) -> str:
        return RELATION_METHODS[self.kind]

    def describe(self) -> str:
        return f'{self.kind} "{self.target}"'


#: Output formats a RETURN clause may name.
OUTPUT_FORMATS = ("table", "json", "csv", "cim")


@dataclass(frozen=True)
class Query(Node):
    type_name: str
    relations: tuple[Relation, ...] = ()
    where: Node | None = None
    select: tuple[str, ...] | None = None
    return_format: str | None = None
    source: str = field(default="", compare=False)

    def describe(self) -> str:
        parts = [f"FIND {self.type_name}"]
        parts.extend(relation.describe() for relation in self.relations)
        if self.where is not None:
            parts.append(f"WHERE {self.where.describe()}")
        if self.select is not None:
            parts.append(f"SELECT {', '.join(self.select)}")
        if self.return_format is not None:
            parts.append(f"RETURN {self.return_format}")
        return "\n".join(parts)


@dataclass(frozen=True)
class Script(Node):
    """The statements of one .gridql file, in the order they will run."""

    statements: tuple[Query, ...]
    source: str = field(default="", compare=False)
    path: str | None = field(default=None, compare=False)

    def __iter__(self):
        return iter(self.statements)

    def __len__(self) -> int:
        return len(self.statements)

    def describe(self) -> str:
        return ";\n\n".join(statement.describe() for statement in self.statements)
