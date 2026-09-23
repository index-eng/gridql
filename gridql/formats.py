# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render a query result as a table, JSON or CSV.

Columns come from the classes actually present in the result, so
``FIND devices`` shows the shared columns while ``FIND transformers`` also
shows kVA and winding voltages.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable

from .cim import export_network
from .color import PLAIN, Palette
from .lang.ast import OUTPUT_FORMATS
from .lang.evaluator import Result

FORMATS = OUTPUT_FORMATS


def columns_for(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Union of the row keys, in first-seen order."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def format_value(value: Any, *, empty: str = "") -> str:
    if value is None:
        return empty
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 13.8 rather than 13.8000000001, 500 rather than 500.0.
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def to_table(result: Result, palette: Palette = PLAIN) -> str:
    rows = result.rows()
    if not rows:
        return palette.paint(f"no {result.type_name} matched", "muted")

    columns = columns_for(rows)
    cells = [[format_value(row.get(column), empty="-") for column in columns] for row in rows]
    widths = [
        max(len(column), *(len(cell[index]) for cell in cells))
        for index, column in enumerate(columns)
    ]
    # Rows of equipment line up with the objects they came from; an
    # aggregate's rows are computed and describe no one device.
    objects = [None] * len(rows) if result.is_aggregate else result.objects

    def line(values: list[str], roles: list[str | None]) -> str:
        # Padding goes outside the paint: escape codes take no room on
        # screen, and a painted trailing space would survive rstrip().
        padded = [
            (palette.paint(value, role) if role else value) + " " * (widths[i] - len(value))
            for i, (value, role) in enumerate(zip(values, roles))
        ]
        return "  ".join(padded).rstrip()

    lines = [
        line(columns, ["header"] * len(columns)),
        palette.paint("  ".join("-" * width for width in widths), "muted"),
    ]
    for obj, row, cell in zip(objects, rows, cells):
        lines.append(line(cell, [_role(obj, column, row.get(column)) for column in columns]))
    lines.append("")
    lines.append(palette.paint(f"{len(rows)} row{'s' if len(rows) != 1 else ''}", "muted"))
    return "\n".join(lines)


def _role(obj: Any, column: str, value: Any) -> str | None:
    """What a cell marks: an off-normal state, dead equipment, or nothing."""
    if value is None:
        return "muted"
    name = column.lower()
    if name == "energized" and value is False:
        return "attention"
    if name == "state" and obj is not None:
        normal = getattr(obj, "normal_state", None)
        if normal is not None and value != normal:
            return "attention"
    return None


def to_json(result: Result) -> str:
    return json.dumps(result.rows(), indent=2, default=str)


def to_csv(result: Result) -> str:
    rows = result.rows()
    buffer = io.StringIO()
    columns = columns_for(rows)
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: format_value(row.get(column)) for column in columns})
    return buffer.getvalue().rstrip("\n")


def to_cim(result: Result) -> str:
    """The result as a CIM RDF/XML document: the slice of grid it selected."""
    if result.network is None:
        raise ValueError("this result is not attached to a network, so it cannot export CIM")
    return export_network(result.network, result.objects)


_RENDERERS = {"table": to_table, "json": to_json, "csv": to_csv, "cim": to_cim}


def render(result: Result, output_format: str = "table", palette: Palette = PLAIN) -> str:
    """The result in ``output_format``. Only a table takes colour."""
    if output_format == "table":
        return to_table(result, palette)
    try:
        return _RENDERERS[output_format](result)
    except KeyError:
        raise ValueError(
            f"unknown format '{output_format}'; expected one of {', '.join(FORMATS)}"
        ) from None


def summarize(result: Result) -> str:
    """The statement on one line, for a header above its output."""
    return " ".join(result.query.describe().split())


def render_script(
    results: list[Result], output_format: str | None = None, palette: Palette = PLAIN
) -> str:
    """Render every statement of a script run.

    Two modes, because a script serves two audiences:

    * ``--format json`` produces one JSON document -- an array with an entry
      per statement -- so a CI job can parse the whole run at once;
    * otherwise each statement is rendered in its own format (its ``RETURN``
      clause, else the given format, else a table) and the chunks are
      separated by a comment naming the statement.
    """
    if not results:
        return ""

    if output_format == "cim" or (
        output_format is None and all(r.output_format == "cim" for r in results)
    ):
        network = next((r.network for r in results if r.network is not None), None)
        if network is None:
            raise ValueError("these results are not attached to a network")
        selected = {obj.mrid: obj for result in results for obj in result.objects}
        return export_network(network, list(selected.values()))

    if output_format == "json":
        return json.dumps(
            [
                {"query": summarize(result), "type": result.type_name, "rows": result.rows()}
                for result in results
            ],
            indent=2,
            default=str,
        )

    chunks: list[str] = []
    for number, result in enumerate(results, start=1):
        body = render(result, output_format or result.output_format or "table", palette)
        if len(results) > 1:
            body = f"{palette.paint(f'-- {number}. {summarize(result)}', 'muted')}\n{body}"
        chunks.append(body)
    return "\n\n".join(chunks)
