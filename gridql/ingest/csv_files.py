# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read a network from CSV, and write one back out.

CSV is what a utility will actually hand you. This reads the shape people
already keep in spreadsheets and GIS exports -- a table of equipment and a
table of what connects to what -- without asking them to produce CIM first.

    equipment/
      devices.csv       mrid, name, type, feeder, ...   (required)
      connections.csv   from_device, to_device          (optional)
      feeders.csv       mrid, name, voltage, head       (optional)
      substations.csv   mrid, name, voltage             (optional)

Only ``devices.csv`` is required; feeders and substations are created from
whatever the devices refer to, and a feeder with exactly one breaker gets
that breaker as its head.

Real exports never use the column names you expect, so headers are matched
case-insensitively against a table of aliases, and any column that is not
recognised is kept as an extra attribute rather than dropped -- which means
a utility's own fields stay queryable.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..errors import GridQLError
from ..model import MISSING, Device, GridObject, Network, Switch
from ..cim.vocabulary import CIM_TO_MODEL
from ..model.types import TYPE_ALIASES, TYPE_CLASSES, canonical_unit
from ..units import UnitError, parse_quantity

DEVICES = "devices.csv"
CONNECTIONS = "connections.csv"
FEEDERS = "feeders.csv"
SUBSTATIONS = "substations.csv"

#: Header spellings seen in the wild -> the attribute they mean.
ALIASES: dict[str, str] = {
    "id": "mrid", "mrid": "mrid", "device_id": "mrid", "deviceid": "mrid",
    "objectid": "mrid", "tag": "mrid",
    "name": "name", "description": "name", "label": "name",
    "type": "type", "device_type": "type", "devicetype": "type", "class": "type",
    "equipment_type": "type", "asset_type": "type",
    "feeder": "feeder", "feeder_id": "feeder", "circuit": "feeder", "circuit_id": "feeder",
    "substation": "substation", "sub": "substation", "station": "substation",
    "phases": "phases", "phase": "phases", "phasing": "phases",
    "voltage": "voltage", "kv": "voltage", "nominal_voltage": "voltage",
    "base_voltage": "voltage", "voltage_kv": "voltage",
    "state": "state", "position": "state", "status": "state", "current_state": "state",
    "normal_state": "normal_state", "normal_position": "normal_state", "normal": "normal_state",
    "is_tie": "is_tie", "tie": "is_tie",
    "kva": "kva", "rating": "kva", "rating_kva": "kva", "capacity_kva": "kva",
    "primary_voltage": "primary_voltage", "high_side_kv": "primary_voltage",
    "secondary_voltage": "secondary_voltage", "low_side_kv": "secondary_voltage",
    "kw": "kw", "load_kw": "kw", "p": "kw",
    "kvar": "kvar", "load_kvar": "kvar", "q": "kvar",
    "length": "length", "length_ft": "length", "len": "length",
    "conductor": "conductor", "wire": "conductor", "conductor_type": "conductor",
    "ampacity": "ampacity", "rated_current": "ampacity", "amps": "ampacity",
    "head": "head", "head_device": "head", "source": "head", "source_device": "head",
    "from_device": "from_device", "from": "from_device", "from_mrid": "from_device",
    "device1": "from_device", "node1": "from_device",
    "to_device": "to_device", "to": "to_device", "to_mrid": "to_device",
    "device2": "to_device", "node2": "to_device",
}

_BOOL_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_BOOL_FALSE = frozenset({"false", "f", "no", "n", "0"})
_INTEGER = re.compile(r"-?(0|[1-9]\d*)$")
_DECIMAL = re.compile(r"-?(0|[1-9]\d*)?\.\d+$")


class CsvError(GridQLError):
    """The files could not be read as a network."""


@dataclass
class CsvReport:
    """What the loader made of the files."""

    devices: int = 0
    feeders: int = 0
    substations: int = 0
    connections: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def problem(self, source: str, line: int, message: str) -> None:
        self.problems.append(f"{source}:{line}: {message}")

    def counts(self) -> str:
        return (
            f"{self.devices} devices, {self.feeders} feeders, "
            f"{self.substations} substations, {self.connections} connections"
        )

    def summary(self) -> str:
        lines = [f"loaded {self.counts()}"]
        if self.problems:
            lines.append(f"{len(self.problems)} row(s) skipped or incomplete:")
            lines.extend(f"  {problem}" for problem in self.problems[:20])
            if len(self.problems) > 20:
                lines.append(f"  ... and {len(self.problems) - 20} more")
        lines.extend(self.notes)
        return "\n".join(lines)


@dataclass
class CsvDocument:
    network: Network
    report: CsvReport


# -- reading ------------------------------------------------------------


def load_csv(source: str | Path, **overrides: str | Path) -> Network:
    """Read a network from CSV files."""
    return read_csv(source, **overrides).network


def read_csv(source: str | Path, **overrides: str | Path) -> CsvDocument:
    """Read CSV files, returning the network and a report on what was found.

    ``source`` is a directory of conventionally named files, or a single
    devices file. Individual paths may be given as keyword overrides
    (``devices=``, ``connections=``, ``feeders=``, ``substations=``).
    """
    paths = _resolve_paths(source, overrides)
    if not paths["devices"].exists():
        raise CsvError(f"no device file at {paths['devices']}")

    report = CsvReport()
    network = Network()

    for row, line in _rows(paths["substations"], report):
        _add_substation(network, row, line, report, paths["substations"].name)
    for row, line in _rows(paths["feeders"], report):
        _add_feeder(network, row, line, report, paths["feeders"].name)

    heads: dict[str, str] = {}
    for feeder in network.feeders:
        if feeder.head:
            heads[feeder.mrid] = feeder.head
            feeder.head = None  # set again once the devices exist

    devices = list(_rows(paths["devices"], report))
    _ensure_containers(network, devices)

    for row, line in devices:
        _add_device(network, row, line, report, paths["devices"].name)

    for row, line in _rows(paths["connections"], report):
        _add_connection(network, row, line, report, paths["connections"].name)

    for mrid, head in heads.items():
        feeder = network.objects.get(mrid)
        if head in network.objects:
            feeder.head = head
        else:
            report.notes.append(f"{mrid}: recorded head '{head}' is not in the device file")

    report.notes.extend(network.infer_heads())
    report.devices = len(network.devices)
    report.feeders = len(network.feeders)
    report.substations = len(network.substations)
    report.connections = sum(len(network.neighbors(m)) for m in network.objects) // 2
    return CsvDocument(network, report)


def _resolve_paths(source: str | Path, overrides: dict) -> dict[str, Path]:
    base = Path(source)
    if base.is_dir():
        paths = {
            "devices": base / DEVICES,
            "connections": base / CONNECTIONS,
            "feeders": base / FEEDERS,
            "substations": base / SUBSTATIONS,
        }
    else:
        # A single file is taken to be the equipment list.
        paths = {
            "devices": base,
            "connections": base.parent / CONNECTIONS,
            "feeders": base.parent / FEEDERS,
            "substations": base.parent / SUBSTATIONS,
        }
    for name, path in overrides.items():
        if name not in paths:
            raise CsvError(f"unknown file '{name}'; expected one of {', '.join(paths)}")
        paths[name] = Path(path)
    return paths


def _rows(path: Path, report: CsvReport) -> Iterator[tuple[dict[str, str], int]]:
    """Yield each row keyed by canonical attribute name, with its line number."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return
        columns = [_canonical(name) for name in header]
        for line, values in enumerate(reader, start=2):
            if not any(value.strip() for value in values):
                continue
            row = {
                column: value.strip()
                for column, value in zip(columns, values)
                if column and value.strip()
            }
            yield row, line


def _canonical(header: str) -> str:
    key = header.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


# -- building objects ---------------------------------------------------


def _add_substation(network, row, line, report, source) -> None:
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return
    network.add_substation(
        mrid,
        name=row.get("name", ""),
        voltage=_quantity(row.get("voltage"), "voltage", report, source, line),
        extras=_extras(row, {"mrid", "name", "voltage"}),
    )


def _add_feeder(network, row, line, report, source) -> None:
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return
    feeder = network.add_feeder(
        mrid,
        name=row.get("name", ""),
        voltage=_quantity(row.get("voltage"), "voltage", report, source, line),
        substation=row.get("substation"),
        extras=_extras(row, {"mrid", "name", "voltage", "substation", "head"}),
    )
    feeder.head = row.get("head")


def _ensure_containers(network: Network, devices: list) -> None:
    """Create any feeder or substation the devices refer to but no file declared."""
    for row, _line in devices:
        substation = row.get("substation")
        if substation and substation not in network.objects:
            network.add_substation(substation)
        feeder = row.get("feeder")
        if feeder and feeder not in network.objects:
            network.add_feeder(feeder, substation=substation)


def _add_device(network, row, line, report, source) -> None:
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return
    if mrid in network.objects:
        report.problem(source, line, f"duplicate mRID '{mrid}'")
        return

    cls, note = _device_class(row.get("type"))
    if note:
        report.problem(source, line, note)

    handled = {"mrid", "name", "type", "feeder", "substation", "phases", "voltage"}
    fields: dict[str, Any] = {
        "name": row.get("name", ""),
        "feeder": row.get("feeder"),
        "substation": row.get("substation"),
        "voltage": _quantity(row.get("voltage"), "voltage", report, source, line),
    }
    if row.get("phases"):
        fields["phases"] = row["phases"]

    for attribute in _numeric_fields(cls):
        handled.add(attribute)
        if row.get(attribute):
            fields[attribute] = _quantity(row[attribute], attribute, report, source, line)

    if issubclass(cls, Switch):
        handled |= {"state", "normal_state", "is_tie"}
        if row.get("normal_state"):
            fields["normal_state"] = row["normal_state"].upper()
        if row.get("state"):
            fields["state"] = row["state"].upper()
        fields["is_tie"] = _boolean(row.get("is_tie"))

    if "conductor" in _text_fields(cls):
        handled.add("conductor")
        if row.get("conductor"):
            fields["conductor"] = row["conductor"]

    extras = _extras(row, handled)
    if note:
        extras["source_type"] = row.get("type", "")
    fields["extras"] = extras

    try:
        network.add(cls(mrid=mrid, **fields))
    except (TypeError, ValueError) as error:
        report.problem(source, line, f"{mrid}: {error}")


def _add_connection(network, row, line, report, source) -> None:
    first, second = row.get("from_device"), row.get("to_device")
    if not first or not second:
        report.problem(source, line, "needs both from_device and to_device")
        return
    for mrid in (first, second):
        if mrid not in network.objects:
            report.problem(source, line, f"unknown device '{mrid}'")
            return
    if first == second:
        report.problem(source, line, f"'{first}' connected to itself")
        return
    network.connect(first, second)


def _device_class(named: str | None) -> tuple[type[Device], str | None]:
    """The class a type column names, falling back to generic equipment."""
    if not named:
        return Device, None

    wanted = named.strip()
    key = TYPE_ALIASES.get(wanted.lower())
    cls = TYPE_CLASSES.get(key) if key else None

    if cls is None:
        # A GIS or ADMS export usually names equipment by its CIM class.
        cls = next(
            (
                model
                for cim_class, model in CIM_TO_MODEL.items()
                if cim_class.lower() == wanted.lower()
            ),
            None,
        )

    if cls is None or not issubclass(cls, Device):
        return Device, (
            f"unknown type '{named}', loaded as generic equipment "
            "and kept in extras as source_type"
        )
    return cls, None


def _numeric_fields(cls: type) -> tuple[str, ...]:
    names = ("kva", "primary_voltage", "secondary_voltage", "kw", "kvar", "length", "ampacity")
    return tuple(name for name in names if name in _fields(cls))


def _text_fields(cls: type) -> tuple[str, ...]:
    return tuple(name for name in ("conductor",) if name in _fields(cls))


def _fields(cls: type) -> frozenset[str]:
    import dataclasses

    return frozenset(f.name for f in dataclasses.fields(cls))


def _quantity(text, attribute, report, source, line):
    if not text:
        return None
    try:
        return parse_quantity(text, canonical_unit(attribute))
    except UnitError as error:
        report.problem(source, line, f"{attribute}: {error}")
        return None


def _boolean(text: str | None) -> bool:
    return bool(text) and text.strip().lower() in _BOOL_TRUE


def _extras(row: dict[str, str], handled: set[str]) -> dict[str, Any]:
    """Columns the model has no field for, kept so they stay queryable."""
    return {
        column: _scalar(value)
        for column, value in row.items()
        if column not in handled
    }


def _scalar(text: str) -> Any:
    """Read a bare cell as a number or boolean where that is unambiguous.

    A leading zero means the value is an identifier rather than a number, so
    "00123" stays text.
    """
    if _INTEGER.fullmatch(text):
        return int(text)
    if _DECIMAL.fullmatch(text):
        return float(text)
    lowered = text.lower()
    if lowered in _BOOL_TRUE and lowered not in ("1",):
        return True
    if lowered in _BOOL_FALSE and lowered not in ("0",):
        return False
    return text


# -- writing ------------------------------------------------------------


def write_csv(network: Network, directory: str | Path) -> list[Path]:
    """Write a network out in the same shape this module reads."""
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)

    written = [
        _write(base / SUBSTATIONS, ("mrid", "name", "voltage"), network.substations),
        _write(
            base / FEEDERS,
            ("mrid", "name", "voltage", "substation", "head"),
            network.feeders,
            extra={"head": lambda f: f.head},
        ),
        _write(base / DEVICES, _device_columns(network), network.devices),
    ]

    edges = sorted(
        {
            (min(mrid, neighbor), max(mrid, neighbor))
            for mrid in network.objects
            for neighbor in network.neighbors(mrid)
        }
    )
    path = base / CONNECTIONS
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("from_device", "to_device"))
        writer.writerows(edges)
    written.append(path)

    return written


def _device_columns(network: Network) -> tuple[str, ...]:
    base = ["mrid", "name", "type", "feeder", "substation", "phases", "voltage"]
    optional = ["state", "normal_state", "is_tie", "kva", "primary_voltage",
                "secondary_voltage", "kw", "kvar", "length", "conductor", "ampacity"]
    present = [name for name in optional if any(_has(d, name) for d in network.devices)]
    extras: list[str] = []
    for device in network.devices:
        for key in device.extras:
            if key not in extras:
                extras.append(key)
    return tuple(base + present + sorted(extras))


def _has(device: GridObject, name: str) -> bool:
    value = device.attribute(name)
    return value is not None and value is not MISSING


def _write(path: Path, columns: Iterable[str], objects, extra: dict | None = None) -> Path:
    columns = tuple(columns)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for obj in sorted(objects, key=lambda o: o.mrid):
            row = []
            for column in columns:
                if extra and column in extra:
                    row.append(_cell(extra[column](obj)))
                    continue
                value = obj.attribute(column)
                if value is MISSING:
                    value = obj.extras.get(column)
                row.append(_cell(value))
            writer.writerow(row)
    return path


def _cell(value: Any) -> str:
    if value is None or value is MISSING:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return str(value)
