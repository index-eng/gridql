# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bind the parameters of a .gridql file to the values a run supplies.

A saved query is only reusable if the circuit it asks about can change
without editing it. Binding is what makes that true: the parser leaves a
``$feeder`` in the tree, and this module replaces it with the value the run
named, producing an ordinary query that the evaluator cannot tell apart from
one written out by hand.

Values arrive as text -- from a command line, a config file or a CI job -- so
they are read the way the language reads a literal: ``0.5MVA`` becomes a
quantity with its unit, ``FDR-104`` stays a string. A parameter is always a
*value*, never an attribute reference, so binding cannot change what a query
means, only what it is about.
"""

from __future__ import annotations

import difflib
from typing import Any, Mapping

from ..errors import ParameterError
from ..units import split_quantity
from .ast import (
    And,
    Compare,
    Contains,
    In,
    Literal,
    Node,
    Not,
    Or,
    Param,
    ParamRef,
    Quantity,
    Query,
    Relation,
    Script,
)


def coerce(value: Any) -> Node:
    """Read a supplied value the way the parser reads a written one."""
    if isinstance(value, Node):
        return value
    if isinstance(value, bool):
        return Literal(value)
    if isinstance(value, (int, float)):
        return Quantity(float(value))

    text = str(value)
    number = split_quantity(text)
    if number is not None:
        return Quantity(*number)
    return Literal(text)


def text_of(node: Node) -> str:
    """The plain text of a bound value, for the places that need a name."""
    if isinstance(node, Quantity):
        number = f"{node.value:.6f}".rstrip("0").rstrip(".") or "0"
        return f"{number}{node.unit or ''}"
    if isinstance(node, Literal):
        if isinstance(node.value, bool):
            return "true" if node.value else "false"
        return str(node.value)
    return node.describe()


def resolve(script: Script, values: Mapping[str, Any] | None = None) -> dict[str, Node]:
    """The value each declared parameter takes, by lowercased name.

    Supplied values win over the file's defaults. A parameter with neither is
    refused, naming every one that is missing rather than the first.
    """
    supplied = {str(name).lower(): value for name, value in (values or {}).items()}
    declared = {param.name.lower(): param for param in script.params}

    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise ParameterError(_unknown_message(unknown, script.params, script.path))

    bindings: dict[str, Node] = {}
    missing: list[Param] = []
    for key, param in declared.items():
        if key in supplied:
            bindings[key] = coerce(supplied[key])
        elif param.default is not None:
            bindings[key] = param.default
        else:
            missing.append(param)

    if missing:
        names = ", ".join(param.name for param in missing)
        flags = " ".join(f"--{param.name} <value>" for param in missing)
        where = f" of {script.path}" if script.path else ""
        raise ParameterError(
            f"missing required parameter{'s' if len(missing) > 1 else ''}{where}: "
            f"{names}. Supply {'them' if len(missing) > 1 else 'it'} with {flags}"
        )

    return bindings


def bind_script(script: Script, values: Mapping[str, Any] | None = None) -> Script:
    """A copy of ``script`` with every ``$name`` replaced by its value."""
    if not script.params:
        if values:
            raise ParameterError(_unknown_message(sorted(values), (), script.path))
        return script

    bindings = resolve(script, values)
    statements = tuple(bind_query(statement, bindings) for statement in script.statements)
    return Script(statements, (), script.source, script.path)


def bind_query(query: Query, bindings: Mapping[str, Node]) -> Query:
    """Substitute ``bindings`` into one statement."""
    relations = tuple(_bind_relation(relation, bindings) for relation in query.relations)
    where = None if query.where is None else _bind(query.where, bindings)
    limit = query.limit
    if isinstance(limit, ParamRef):
        limit = _bind_limit(limit, bindings)

    return Query(
        query.type_name,
        relations,
        where,
        query.select,
        query.group_by,
        query.order_by,
        limit,
        query.return_format,
        query.source,
    )


def _bind_relation(relation: Relation, bindings: Mapping[str, Node]) -> Relation:
    if not isinstance(relation.target, ParamRef):
        return relation
    # A topology target is a device name, so whatever the value reads as,
    # what the traversal needs is its text: --feeder 104 means device "104".
    target = text_of(_lookup(relation.target, bindings))
    return Relation(relation.kind, target, relation.position)


def _bind_limit(reference: ParamRef, bindings: Mapping[str, Node]) -> int:
    node = _lookup(reference, bindings)
    if isinstance(node, Quantity) and node.unit is None and node.value >= 0:
        if node.value == int(node.value):
            return int(node.value)
    raise ParameterError(
        f"LIMIT ${reference.name} takes a whole number of rows, zero or more; "
        f"got '{text_of(node)}'"
    )


def _bind(node: Node, bindings: Mapping[str, Node]) -> Node:
    if isinstance(node, ParamRef):
        return _lookup(node, bindings)
    if isinstance(node, And):
        return And(_bind(node.left, bindings), _bind(node.right, bindings))
    if isinstance(node, Or):
        return Or(_bind(node.left, bindings), _bind(node.right, bindings))
    if isinstance(node, Not):
        return Not(_bind(node.operand, bindings))
    if isinstance(node, Compare):
        return Compare(
            node.attribute, node.operator, _bind(node.operand, bindings), node.position
        )
    if isinstance(node, Contains):
        return Contains(node.attribute, _bind(node.operand, bindings), node.position)
    if isinstance(node, In):
        return In(
            node.attribute,
            tuple(_bind(operand, bindings) for operand in node.operands),
            node.position,
        )
    return node


def _lookup(reference: ParamRef, bindings: Mapping[str, Node]) -> Node:
    try:
        return bindings[reference.name.lower()]
    except KeyError:  # pragma: no cover -- the parser rejects undeclared names
        raise ParameterError(f"no value for ${reference.name}") from None


def _unknown_message(
    unknown: list[str], params: tuple[Param, ...], path: str | None
) -> str:
    where = f"{path} " if path else "this query "
    if not params:
        return (
            f"{where}declares no parameters, so "
            f"{', '.join(unknown)} cannot be supplied"
        )

    known = [param.name for param in params]
    lines = [
        f"unknown parameter{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
        f"{where}declares: {', '.join(known)}"
    ]
    for name in unknown:
        close = difflib.get_close_matches(name, [k.lower() for k in known], n=1, cutoff=0.6)
        if close:
            lines.append(f"did you mean --{close[0]}?")
    return "; ".join(lines)
