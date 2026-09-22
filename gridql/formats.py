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

from .lang.evaluator import Result

FORMATS = ("table", "json", "csv")


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


def to_table(result: Result) -> str:
    rows = result.rows()
    if not rows:
        return f"no {result.type_name} matched"

    columns = columns_for(rows)
    cells = [[format_value(row.get(column), empty="-") for column in columns] for row in rows]
    widths = [
        max(len(column), *(len(cell[index]) for cell in cells))
        for index, column in enumerate(columns)
    ]

    lines = [
        "  ".join(column.ljust(widths[i]) for i, column in enumerate(columns)).rstrip(),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in cells
    )
    lines.append("")
    lines.append(f"{len(rows)} row{'s' if len(rows) != 1 else ''}")
    return "\n".join(lines)


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


_RENDERERS = {"table": to_table, "json": to_json, "csv": to_csv}


def render(result: Result, output_format: str = "table") -> str:
    try:
        return _RENDERERS[output_format](result)
    except KeyError:
        raise ValueError(
            f"unknown format '{output_format}'; expected one of {', '.join(FORMATS)}"
        ) from None
