# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Evaluate a parsed query against a :class:`~gridql.model.network.Network`.

The evaluator only ever calls the semantic model's public API -- ``of_class``,
``downstream_of``, ``attribute`` and friends. It has no idea whether those
objects came from a hard-coded feeder, SQLite or a CIM file, which is the whole
point of the layering.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ..errors import GridQLError, GridQLNameError, UnitError
from ..model import MISSING, GridObject, Network
from ..model.types import attribute_universe, canonical_unit, class_for, plural, resolve_type
from ..units import convert
from .ast import (
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
    Script,
    SelectItem,
    Truthy,
)
from .binding import bind_script
from .parser import parse, parse_script

#: Attribute names whose value depends on the query, not on the object.
HOPS = "hops"


def _value(network: Network, obj: GridObject, name: str, distances) -> Any:
    """An attribute's value, including the ones only this query can answer.

    ``hops`` is how far the object is from the query's topology target, so
    it exists only while a query is being evaluated -- unlike ``depth``,
    which the network knows on its own.
    """
    if name.lower() == HOPS:
        if distances is None:
            return MISSING
        found = distances.get(obj.mrid)
        return MISSING if found is None else found
    return network.attribute(obj, name)


def _tested(node: Node | None) -> list[str]:
    """The attributes a WHERE clause tests, as written -- not its operands.

    A bare word on the right may be a value (``state = OPEN``), so only the
    left-hand side is certain to name an attribute.
    """
    found: list[str] = []
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if isinstance(current, (Compare, In, Contains, Truthy)):
            found.append(current.attribute)
        elif isinstance(current, (And, Or)):
            stack.extend((current.right, current.left))
        elif isinstance(current, Not):
            stack.append(current.operand)
    return found


def _referenced(node: Node | None) -> set[str]:
    """Every attribute a WHERE clause mentions, on either side."""
    found: set[str] = set()
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if isinstance(current, (Compare, In, Contains, Truthy)):
            found.add(current.attribute.lower())
        if isinstance(current, (And, Or)):
            stack.extend((current.left, current.right))
        elif isinstance(current, Not):
            stack.append(current.operand)
        elif isinstance(current, Compare):
            stack.append(current.operand)
        elif isinstance(current, Contains):
            stack.append(current.operand)
        elif isinstance(current, In):
            stack.extend(current.operands)
        elif isinstance(current, Name):
            found.add(current.name.lower())
    return found


#: Strings that read as false when an attribute is used as a bare test.
_FALSEY_WORDS = frozenset({"", "false", "no", "n", "0", "off"})


@dataclass
class Result:
    """The objects a query selected, plus enough context to render them."""

    query: Query
    type_name: str
    type_key: str
    objects: list[GridObject]
    network: Network | None = field(default=None, repr=False, compare=False)
    #: The projection actually used, which GROUP BY may have supplied.
    select: tuple[SelectItem, ...] | None = None
    #: Rows an aggregate query computed. None for a plain equipment query.
    computed: list[dict[str, Any]] | None = None
    #: Distance from the query's topology target, by mRID.
    distances: dict[str, int] | None = field(default=None, repr=False, compare=False)

    @property
    def is_aggregate(self) -> bool:
        return self.computed is not None

    @property
    def output_format(self) -> str | None:
        """The format the query's RETURN clause asked for, if any."""
        return self.query.return_format

    def __iter__(self) -> Iterator[GridObject]:
        return iter(self.objects)

    def __len__(self) -> int:
        return len(self.objects)

    @property
    def mrids(self) -> list[str]:
        return [obj.mrid for obj in self.objects]

    def columns(self) -> list[str]:
        """The columns to render: the SELECT list, or the classes' own.

        A selected column keeps the spelling the query used, so
        ``SELECT mRID`` produces an ``mRID`` header and ``SUM(kva)`` a
        ``SUM(kva)`` one, even though attribute lookup is case-insensitive.
        """
        select = self.select
        if select is not None and [i.attribute for i in select] != ["*"]:
            return [item.written for item in select]

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
        if self.computed is not None:
            return self.computed

        columns = self.columns()
        rows: list[dict[str, Any]] = []
        for obj in self.objects:
            value_of = {}
            for column in columns:
                value = (
                    obj.attribute(column)
                    if self.network is None
                    else _value(self.network, obj, column, self.distances)
                )
                value_of[column] = None if value is MISSING else value
            rows.append(value_of)
        return rows


def execute(network: Network, source: str) -> Result:
    """Parse and run a single GridQL query."""
    return evaluate(network, parse(source))


def execute_script(
    network: Network,
    source: str,
    path: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[Result]:
    """Parse and run every statement of a .gridql script, in order."""
    return evaluate_script(network, parse_script(source, path), params)


def evaluate_script(
    network: Network, script: Script, params: Mapping[str, Any] | None = None
) -> list[Result]:
    """Run a script, binding its parameters first.

    A script whose parameters all have defaults runs with no values at all;
    one with a required parameter is refused rather than run with a hole in
    it.
    """
    if script.params or params:
        script = bind_script(script, params)
    return [evaluate(network, statement) for statement in script.statements]


def evaluate(network: Network, query: Query) -> Result:
    type_key = resolve_type(query.type_name)
    candidates = network.of_class(class_for(type_key))
    objects = candidates

    # Topology constraints intersect: each one narrows what came before.
    # The first one also fixes what "hops" means and the natural order.
    distances: dict[str, int] | None = None
    for index, relation in enumerate(query.relations):
        related = getattr(network, relation.method)(relation.target)
        if index == 0:
            distances = _distances(network, relation, related)
        allowed = {obj.mrid for obj in related}
        objects = [obj for obj in objects if obj.mrid in allowed]

    # Taken from every candidate, not only the ones topology kept, so a
    # utility's own column is known even where this slice lacks it.
    vocabulary = _Vocabulary(
        type_key, _known_attributes(type_key, candidates), network.source, len(candidates)
    )
    select = _projection(query, vocabulary, distances)

    if query.where is not None:
        context = _Context(network, vocabulary.names, distances)
        objects = [obj for obj in objects if _test(query.where, obj, context)]

    if query.is_aggregate:
        rows = _aggregate(network, query, objects, select, distances)
        return Result(
            query, query.type_name, type_key, objects, network, select, rows, distances
        )

    _order_objects(network, query, objects, distances)
    if query.limit is not None:
        objects = objects[: query.limit]

    return Result(
        query, query.type_name, type_key, objects, network, select, None, distances
    )


def _distances(network: Network, relation, related: list[GridObject]) -> dict[str, int]:
    """How far each related object is from the relation's target.

    Every tree relation can be answered from one depth map: the target and
    the object both sit somewhere on the feeder, and the gap between them is
    the number of devices in between.
    """
    if relation.kind == "CONNECTED TO":
        return {obj.mrid: 1 for obj in related}

    target = network.get(relation.target)
    origin = network.depth_of(target.mrid)

    distances: dict[str, int] = {}
    for obj in related:
        depth = network.depth_of(obj.mrid)
        if depth is MISSING:
            continue
        # A feeder or substation target has no depth of its own, so distance
        # is measured from that container's head.
        distances[obj.mrid] = depth if origin is MISSING else abs(depth - origin)
    return distances


# -- projection ---------------------------------------------------------


def _known_attributes(type_key: str, candidates: list[GridObject]) -> frozenset[str]:
    """Every attribute a query on this type may name, the utility's own included.

    One set serves both questions -- is this name a typo, and is this bare
    word on the right an attribute or a value -- so that a column the query
    may filter on is also one it may compare against.
    """
    return attribute_universe(type_key) | frozenset(
        key.lower() for obj in candidates for key in obj.extras
    )


@dataclass(frozen=True)
class _Vocabulary:
    """The attribute names a query may use, and enough context to refuse one well."""

    type_key: str
    names: frozenset[str]
    #: Where the network came from, as its loader described it.
    source: str | None
    candidates: int

    def check(self, name: str) -> None:
        """Refuse a name no candidate has, saying which data was searched.

        Whether a utility's own column exists depends on the data loaded,
        so "not an attribute" alone reads as a limit of the language when
        the real answer is usually "not in this dataset".
        """
        if name.lower() in self.names:
            return
        where = self.source or "this network"
        if self.candidates:
            message = f"'{name}' is not an attribute of any {self.type_key} in {where}"
        else:
            message = (
                f"'{name}' is not one of GridQL's {self.type_key} attributes, and "
                f"{where} has no {plural(self.type_key)} to carry one of its own"
            )
        raise GridQLNameError(
            message,
            tuple(difflib.get_close_matches(name.lower(), self.names, n=3, cutoff=0.5)),
        )


def _projection(
    query: Query,
    vocabulary: _Vocabulary,
    distances: dict[str, int] | None = None,
) -> tuple[SelectItem, ...] | None:
    """The effective SELECT list, with GROUP BY's default filled in and checked."""
    select = query.select

    referenced = (
        {item.attribute.lower() for item in select or ()}
        | {column.lower() for column in query.group_by or ()}
        | {key.item.attribute.lower() for key in query.order_by or ()}
        | _referenced(query.where)
    )
    if HOPS in referenced and distances is None:
        raise GridQLError(
            "hops is the distance from a topology target, so the query needs "
            "DOWNSTREAM OF, UPSTREAM OF, CONNECTED TO or FED BY"
        )

    if select is None and query.group_by is not None:
        # "FIND devices GROUP BY feeder" means: one row per feeder, and count them.
        select = tuple(SelectItem(column, column) for column in query.group_by) + (
            SelectItem("COUNT(*)", "*", "count"),
        )

    # A misspelt attribute in WHERE would otherwise match nothing and look
    # exactly like a real empty answer.
    for attribute in _tested(query.where):
        vocabulary.check(attribute)
    for column in query.group_by or ():
        vocabulary.check(column)
    grouped = {column.lower() for column in query.group_by or ()}
    for key in query.order_by or ():
        if key.item.attribute != "*":
            vocabulary.check(key.item.attribute)
        if key.item.is_aggregate and not query.is_aggregate:
            raise GridQLError(
                f"ORDER BY {key.item.written} needs an aggregate query; "
                "add the aggregate to SELECT or group the query"
            )
        if (
            query.is_aggregate
            and not key.item.is_aggregate
            and key.item.attribute.lower() not in grouped
        ):
            # Each row stands for a whole group, which has no single value
            # of an ungrouped attribute to sort by.
            raise GridQLError(
                f"cannot ORDER BY {key.item.written}: it is neither an aggregate "
                "nor grouped; add it to GROUP BY or wrap it in an aggregate"
            )

    if select is None:
        return None

    starred = [item for item in select if item.attribute == "*" and not item.is_aggregate]
    if starred and query.is_aggregate:
        raise GridQLError("SELECT * cannot be combined with an aggregate")

    for item in select:
        if item.attribute != "*":
            vocabulary.check(item.attribute)

    if query.is_aggregate:
        grouped = {column.lower() for column in query.group_by or ()}
        for item in select:
            if not item.is_aggregate and item.attribute.lower() not in grouped:
                raise GridQLError(
                    f"'{item.written}' is neither an aggregate nor grouped; "
                    f"add it to GROUP BY or wrap it in an aggregate"
                )

    return select


# -- aggregation --------------------------------------------------------


def _aggregate(
    network: Network,
    query: Query,
    objects: list[GridObject],
    select: tuple[SelectItem, ...] | None,
    distances: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fold the matched equipment into one row per group."""
    select = select or ()
    group_by = query.group_by or ()

    if group_by:
        groups: dict[tuple, list[GridObject]] = {}
        for obj in objects:
            key = tuple(_plain(_value(network, obj, column, distances)) for column in group_by)
            groups.setdefault(key, []).append(obj)
        ordered = sorted(groups.items(), key=lambda pair: _sort_key(pair[0]))
    else:
        # No grouping still means one row: COUNT of nothing is 0, not no answer.
        ordered = [((), objects)]

    order_items = [key.item for key in query.order_by or ()]

    computed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for key, members in ordered:
        keyed = dict(zip((c.lower() for c in group_by), key))
        values: dict[str, Any] = {}
        for item in (*select, *order_items):
            if item.written in values:
                continue
            values[item.written] = (
                _apply(item, network, members, distances)
                if item.is_aggregate
                else keyed.get(item.attribute.lower())
            )
        computed.append(({item.written: values[item.written] for item in select}, values))

    _order_rows(query, computed)

    rows = [row for row, _ in computed]
    return rows if query.limit is None else rows[: query.limit]


def _apply(
    item: SelectItem,
    network: Network,
    members: list[GridObject],
    distances: dict[str, int] | None = None,
) -> Any:
    function = item.function

    if function == "count" and item.attribute == "*":
        return len(members)

    values = [
        value
        for value in (_value(network, obj, item.attribute, distances) for obj in members)
        if value is not MISSING and value is not None
    ]

    if function == "count":
        return len(values)
    if not values:
        return None

    if function in ("sum", "avg"):
        numbers = []
        for value in values:
            if not _is_number(value):
                raise GridQLError(
                    f"{item.written}: '{value}' is not a number, so it cannot be totalled"
                )
            numbers.append(float(value))
        total = sum(numbers)
        return total if function == "sum" else total / len(numbers)

    if all(_is_number(value) for value in values):
        return min(values) if function == "min" else max(values)
    texts = [str(value) for value in values]
    return min(texts) if function == "min" else max(texts)


# -- ordering -----------------------------------------------------------


def _order_objects(
    network: Network,
    query: Query,
    objects: list[GridObject],
    distances: dict[str, int] | None = None,
) -> None:
    objects.sort(key=lambda obj: obj.mrid)  # a stable, meaningful tie-break

    if not query.order_by and distances is not None:
        # A topology query walked the circuit to find these, so report them
        # in that order: nearest to the target first.
        objects.sort(key=lambda obj: distances.get(obj.mrid, len(distances) + 1))
        return

    for key in reversed(query.order_by or ()):
        _sort_in_place(
            objects,
            lambda obj, k=key: _value(network, obj, k.item.attribute, distances),
            key.descending,
        )


def _order_rows(query: Query, computed: list[tuple[dict, dict]]) -> None:
    for key in reversed(query.order_by or ()):
        _sort_in_place(
            computed, lambda pair, k=key: pair[1].get(k.item.written), key.descending
        )


def _sort_in_place(items: list, value_of, descending: bool) -> None:
    """Sort by a value, keeping rows that have none at the end either way.

    Reversing the whole order would put them first on DESC, which makes
    "the biggest transformers, descending" open with everything that has no
    rating at all. They are not part of the ranking, so they follow it.
    """
    present, missing = [], []
    for item in items:
        value = value_of(item)
        (missing if value is None or value is MISSING else present).append(item)
    present.sort(key=lambda item: _sort_key(value_of(item)), reverse=descending)
    items[:] = present + missing


def _sort_key(value: Any) -> tuple:
    """Order numbers before text before nothing, so mixed columns still sort."""
    if isinstance(value, tuple):
        return tuple(_sort_key(item) for item in value)
    if value is MISSING or value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if _is_number(value):
        return (0, float(value), "")
    return (1, 0.0, str(value).casefold())


def _plain(value: Any) -> Any:
    return None if value is MISSING else value


@dataclass(frozen=True)
class _Context:
    network: Network
    attributes: frozenset[str]
    distances: dict[str, int] | None = None

    def value(self, obj: GridObject, name: str) -> Any:
        return _value(self.network, obj, name, self.distances)


def _test(node: Node, obj: GridObject, context: _Context) -> bool:
    if isinstance(node, And):
        return _test(node.left, obj, context) and _test(node.right, obj, context)
    if isinstance(node, Or):
        return _test(node.left, obj, context) or _test(node.right, obj, context)
    if isinstance(node, Not):
        return not _test(node.operand, obj, context)

    if isinstance(node, Truthy):
        return _truthy(context.value(obj, node.attribute))

    if isinstance(node, Compare):
        left = context.value(obj, node.attribute)
        if left is MISSING:
            return False
        right = _operand(node.operand, obj, context, node.attribute)
        if right is MISSING:
            return False
        return _compare(left, node.operator, right)

    if isinstance(node, In):
        left = context.value(obj, node.attribute)
        if left is MISSING:
            return False
        return any(
            _compare(left, "=", value)
            for value in (_operand(o, obj, context, node.attribute) for o in node.operands)
            if value is not MISSING
        )

    if isinstance(node, Contains):
        left = context.value(obj, node.attribute)
        right = _operand(node.operand, obj, context, node.attribute)
        if left is MISSING or right is MISSING:
            return False
        if isinstance(left, (list, tuple, set, frozenset)):
            return any(_compare(item, "=", right) for item in left)
        return _as_text(right).casefold() in _as_text(left).casefold()

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
        target_unit = canonical_unit(attribute)
        if target_unit is None and node.text is not None:
            # A supplied value meeting an attribute that takes no unit is read
            # as it was written: --name 12A is a name, and 0412 is not 412.
            return node.text
        if node.unit is None:
            return node.value
        if target_unit is None:
            raise UnitError(f"attribute '{attribute}' does not take a unit like '{node.unit}'")
        return convert(node.value, node.unit, target_unit)

    if isinstance(node, Name):
        if node.name.lower() in context.attributes:
            return context.value(obj, node.name)
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


def _as_text(value: Any) -> str:
    """A value as text to search in: 104, not the 104.0 a float would print."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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
