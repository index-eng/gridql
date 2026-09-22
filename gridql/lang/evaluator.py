"""Evaluate a parsed query against a :class:`~gridql.model.network.Network`.

The evaluator only ever calls the semantic model's public API -- ``of_class``,
``downstream_of``, ``attribute`` and friends. It has no idea whether those
objects came from a hard-coded feeder, SQLite or a CIM file, which is the whole
point of the layering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from ..errors import UnitError
from ..model import MISSING, GridObject, Network
from ..model.types import attribute_universe, canonical_unit, class_for, resolve_type
from ..units import convert
from .ast import And, Compare, Contains, In, Literal, Name, Node, Not, Or, Quantity, Query, Truthy
from .parser import parse

#: Strings that read as false when an attribute is used as a bare test.
_FALSEY_WORDS = frozenset({"", "false", "no", "n", "0", "off"})


@dataclass
class Result:
    """The objects a query selected, plus enough context to render them."""

    query: Query
    type_name: str
    type_key: str
    objects: list[GridObject]

    def __iter__(self) -> Iterator[GridObject]:
        return iter(self.objects)

    def __len__(self) -> int:
        return len(self.objects)

    @property
    def mrids(self) -> list[str]:
        return [obj.mrid for obj in self.objects]

    def columns(self) -> list[str]:
        """Union of the display columns of the classes present, in order."""
        columns: list[str] = []
        for obj in self.objects:
            for column in obj.COLUMNS:
                if column not in columns:
                    columns.append(column)
        return columns

    def rows(self) -> list[dict[str, Any]]:
        """One mapping per object, every object answering every column.

        A transformer does not list ``kw`` among its columns, but if a load in
        the same result does, the transformer still reports its own value for
        every column it genuinely has rather than showing a hole.
        """
        columns = self.columns()
        rows: list[dict[str, Any]] = []
        for obj in self.objects:
            value_of = {}
            for column in columns:
                value = obj.attribute(column)
                value_of[column] = None if value is MISSING else value
            rows.append(value_of)
        return rows


def execute(network: Network, source: str) -> Result:
    """Parse and run a GridQL query."""
    return evaluate(network, parse(source))


def evaluate(network: Network, query: Query) -> Result:
    type_key = resolve_type(query.type_name)
    objects = network.of_class(class_for(type_key))

    # Topology constraints intersect: each one narrows what came before.
    for relation in query.relations:
        related = {obj.mrid for obj in getattr(network, relation.method)(relation.target)}
        objects = [obj for obj in objects if obj.mrid in related]

    if query.where is not None:
        context = _Context(network, attribute_universe(type_key))
        objects = [obj for obj in objects if _test(query.where, obj, context)]

    objects.sort(key=lambda obj: obj.mrid)
    return Result(query, query.type_name, type_key, objects)


@dataclass(frozen=True)
class _Context:
    network: Network
    attributes: frozenset[str]


def _test(node: Node, obj: GridObject, context: _Context) -> bool:
    if isinstance(node, And):
        return _test(node.left, obj, context) and _test(node.right, obj, context)
    if isinstance(node, Or):
        return _test(node.left, obj, context) or _test(node.right, obj, context)
    if isinstance(node, Not):
        return not _test(node.operand, obj, context)

    if isinstance(node, Truthy):
        return _truthy(context.network.attribute(obj, node.attribute))

    if isinstance(node, Compare):
        left = context.network.attribute(obj, node.attribute)
        if left is MISSING:
            return False
        right = _operand(node.operand, obj, context, node.attribute)
        if right is MISSING:
            return False
        return _compare(left, node.operator, right)

    if isinstance(node, In):
        left = context.network.attribute(obj, node.attribute)
        if left is MISSING:
            return False
        return any(
            _compare(left, "=", value)
            for value in (_operand(o, obj, context, node.attribute) for o in node.operands)
            if value is not MISSING
        )

    if isinstance(node, Contains):
        left = context.network.attribute(obj, node.attribute)
        right = _operand(node.operand, obj, context, node.attribute)
        if left is MISSING or right is MISSING:
            return False
        if isinstance(left, (list, tuple, set, frozenset)):
            return any(_compare(item, "=", right) for item in left)
        return str(right).casefold() in str(left).casefold()

    raise TypeError(f"cannot evaluate node {node!r}")


def _operand(node: Node, obj: GridObject, context: _Context, attribute: str) -> Any:
    """Resolve the right-hand side of a predicate.

    A bare word is an attribute reference when the queried type has an
    attribute by that name (``state != normal_state``) and a plain value
    otherwise (``state = OPEN``). Quoting always forces the value reading.
    """
    if isinstance(node, Literal):
        return node.value

    if isinstance(node, Quantity):
        if node.unit is None:
            return node.value
        target_unit = canonical_unit(attribute)
        if target_unit is None:
            raise UnitError(f"attribute '{attribute}' does not take a unit like '{node.unit}'")
        return convert(node.value, node.unit, target_unit)

    if isinstance(node, Name):
        if node.name.lower() in context.attributes:
            return context.network.attribute(obj, node.name)
        return node.name

    raise TypeError(f"cannot resolve operand {node!r}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(left: Any, operator: str, right: Any) -> bool:
    if left is None or right is None:
        equal = left is None and right is None
        return equal if operator == "=" else (not equal if operator == "!=" else False)

    if isinstance(left, bool) or isinstance(right, bool):
        left_bool, right_bool = _truthy(left), _truthy(right)
        if operator == "=":
            return left_bool == right_bool
        if operator == "!=":
            return left_bool != right_bool
        return _ordered(int(left_bool), operator, int(right_bool))

    if _is_number(left) and not _is_number(right):
        right = _as_number(right, right)
    elif _is_number(right) and not _is_number(left):
        left = _as_number(left, left)

    if _is_number(left) and _is_number(right):
        if operator == "=":
            return float(left) == float(right)
        if operator == "!=":
            return float(left) != float(right)
        return _ordered(float(left), operator, float(right))

    left_text, right_text = str(left).casefold(), str(right).casefold()
    if operator == "=":
        return left_text == right_text
    if operator == "!=":
        return left_text != right_text
    return _ordered(left_text, operator, right_text)


def _ordered(left: Any, operator: str, right: Any) -> bool:
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    return left <= right


def _as_number(value: Any, fallback: Any) -> Any:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return fallback


def _truthy(value: Any) -> bool:
    if value is MISSING or value is None:
        return False
    if isinstance(value, bool):
        return value
    if _is_number(value):
        return value != 0
    return str(value).strip().casefold() not in _FALSEY_WORDS
