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

Connectivity comes one of two ways, or both. ``connections.csv`` pairs
devices directly. Most GIS and ADMS exports instead give each device the
nodes at its two ends -- ``from_node`` and ``to_node`` columns in
``devices.csv`` -- and devices are connected wherever their nodes match.

Only ``devices.csv`` is required; feeders and substations are created from
whatever the devices refer to, and a feeder with exactly one breaker gets
that breaker as its head.

Real exports never use the column names you expect, so headers are matched
case-insensitively against a table of aliases, and any column that is not
recognised is kept as an extra attribute rather than dropped -- which means
a utility's own fields stay queryable.

When the guessing is not good enough, a mapping file (:mod:`.mapping`)
says exactly which of the utility's files and columns mean what, and the
files can be called anything.
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
from .mapping import TARGETS, Mapping, load_mapping

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
    "device1": "from_device",
    "to_device": "to_device", "to": "to_device", "to_mrid": "to_device",
    "device2": "to_device",
    "from_node": "from_node", "fromnode": "from_node", "from_node_id": "from_node",
    "fromnodeid": "from_node", "node1": "from_node", "from_bus": "from_node",
    "frombus": "from_node", "bus1": "from_node", "upstream_node": "from_node",
    "node": "from_node", "bus": "from_node",
    "to_node": "to_node", "tonode": "to_node", "to_node_id": "to_node",
    "tonodeid": "to_node", "node2": "to_node", "to_bus": "to_node",
    "tobus": "to_node", "bus2": "to_node", "downstream_node": "to_node",
}

#: The device columns that name connectivity nodes, in terminal order.
NODE_COLUMNS = ("from_node", "to_node")

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
    nodes: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def problem(self, source: str, line: int, message: str) -> None:
        self.problems.append(f"{source}:{line}: {message}")

    def counts(self) -> str:
        through = f" through {self.nodes} nodes" if self.nodes else ""
        return (
            f"{self.devices} devices, {self.feeders} feeders, "
            f"{self.substations} substations, {self.connections} connections{through}"
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


@dataclass
class _Row:
    """One source row, keyed by model field, with where it came from."""

    values: dict[str, str]
    line: int
    source: str
    #: Columns a mapping kept as attributes. None when the row was read by
    #: alias, where anything unrecognised in ``values`` is an extra.
    extras: dict[str, Any] | None = None


# -- reading ------------------------------------------------------------


def load_csv(
    source: str | Path, mapping: str | Path | Mapping | None = None, **overrides: str | Path
) -> Network:
    """Read a network from CSV files."""
    return read_csv(source, mapping, **overrides).network


def read_csv(
    source: str | Path, mapping: str | Path | Mapping | None = None, **overrides: str | Path
) -> CsvDocument:
    """Read CSV files, returning the network and a report on what was found.

    ``source`` is a directory of conventionally named files, or a single
    devices file. Individual paths may be given as keyword overrides
    (``devices=``, ``connections=``, ``feeders=``, ``substations=``).

    With a ``mapping`` (a path, or a loaded :class:`Mapping`), ``source`` is
    the directory holding the files the mapping names, and their columns
    mean what the mapping says rather than what their headers suggest.
    """
    report = CsvReport()
    if mapping is None:
        tables = _conventional_tables(source, overrides)
    else:
        if overrides:
            raise CsvError("a mapping names its own files; set file = ... in the mapping instead")
        tables = _mapped_tables(source, mapping, report)

    network = Network(source=str(source))

    for row in tables["substations"]:
        _add_substation(network, row, report)
    for row in tables["feeders"]:
        _add_feeder(network, row, report)

    heads: dict[str, str] = {}
    for feeder in network.feeders:
        if feeder.head:
            heads[feeder.mrid] = feeder.head
            feeder.head = None  # set again once the devices exist

    devices = list(tables["devices"])
    _ensure_containers(network, devices)

    for row in devices:
        if _add_device(network, row, report):
            _attach(network, row, report)

    for row in tables["connections"]:
        _add_connection(network, row, report)

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
    report.nodes = len(network.nodes)
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


def _conventional_tables(source: str | Path, overrides: dict) -> dict[str, Iterator[_Row]]:
    paths = _resolve_paths(source, overrides)
    if not paths["devices"].exists():
        raise CsvError(f"no device file at {paths['devices']}")
    return {kind: _rows(path) for kind, path in (
        ("substations", paths["substations"]),
        ("feeders", paths["feeders"]),
        ("devices", paths["devices"]),
        ("connections", paths["connections"]),
    )}


def _rows(path: Path) -> Iterator[_Row]:
    """Each row keyed by canonical attribute name, guessed from the header."""
    header, records = _table(path)
    columns = [_canonical(name) for name in header]
    for line, values in records:
        row = {
            column: value.strip()
            for column, value in zip(columns, values)
            if column and value.strip()
        }
        yield _Row(row, line, path.name)


def _mapped_tables(
    source: str | Path, mapping: str | Path | Mapping, report: CsvReport
) -> dict[str, Iterator[_Row]]:
    directory = Path(source)
    if not directory.is_dir():
        raise CsvError(
            f"with a mapping, {source} must be the directory holding the files it names"
        )
    if not isinstance(mapping, Mapping):
        mapping = load_mapping(mapping)

    missing = [
        section.file
        for kind in TARGETS
        for section in mapping.of(kind)
        if not (directory / section.file).is_file()
    ]
    if missing:
        where = f" (from {mapping.path})" if mapping.path else ""
        raise CsvError(f"{directory} has no {', '.join(missing)}, which the mapping{where} names")

    return {kind: _mapped_rows(directory, mapping.of(kind), report) for kind in TARGETS}


def _mapped_rows(directory: Path, sections, report: CsvReport) -> Iterator[_Row]:
    """Each row of each file a mapping names, translated by that mapping."""
    for section in sections:
        header, records = _table(directory / section.file)
        if not header:
            report.notes.append(f"{section.file}: empty file")
            continue
        bound = section.bind(header)
        for line, values in records:
            record = dict(zip(header, values))
            fields, extras = bound.translate(record)
            yield _Row(
                fields, line, section.file, {key: _scalar(text) for key, text in extras.items()}
            )
        report.notes.extend(bound.notes())


def _table(path: Path) -> tuple[list[str], Iterator[tuple[int, list[str]]]]:
    """A file's header, and its non-blank rows with their line numbers.

    A missing or empty file is an empty table: the conventional files other
    than devices.csv are optional.
    """
    if not path.exists():
        return [], iter(())
    handle = path.open(newline="", encoding="utf-8-sig")
    reader = csv.reader(handle)
    try:
        header = next(reader)
    except StopIteration:
        handle.close()
        return [], iter(())

    def records() -> Iterator[tuple[int, list[str]]]:
        with handle:
            for line, values in enumerate(reader, start=2):
                if any(value.strip() for value in values):
                    yield line, values

    return header, records()


def _canonical(header: str) -> str:
    key = header.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


# -- building objects ---------------------------------------------------


def _add_substation(network, source_row: _Row, report) -> None:
    row, line, source = source_row.values, source_row.line, source_row.source
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return
    if mrid in network.objects:
        report.problem(source, line, f"duplicate mRID '{mrid}'")
        return
    network.add_substation(
        mrid,
        name=row.get("name", ""),
        voltage=_quantity(row.get("voltage"), "voltage", report, source, line),
        extras=_extras(row, {"mrid", "name", "voltage"}, source_row.extras),
    )


def _add_feeder(network, source_row: _Row, report) -> None:
    row, line, source = source_row.values, source_row.line, source_row.source
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return
    if mrid in network.objects:
        report.problem(source, line, f"duplicate mRID '{mrid}'")
        return
    feeder = network.add_feeder(
        mrid,
        name=row.get("name", ""),
        voltage=_quantity(row.get("voltage"), "voltage", report, source, line),
        substation=row.get("substation"),
        extras=_extras(row, {"mrid", "name", "voltage", "substation", "head"}, source_row.extras),
    )
    feeder.head = row.get("head")


def _ensure_containers(network: Network, devices: list) -> None:
    """Create any feeder or substation the files refer to but none declared.

    Feeders count as well as devices: a feeder naming a substation that is
    not in substations.csv would otherwise point at nothing.
    """
    for feeder in network.feeders:
        if feeder.substation and feeder.substation not in network.objects:
            network.add_substation(feeder.substation)
    for source_row in devices:
        row = source_row.values
        substation = row.get("substation")
        if substation and substation not in network.objects:
            network.add_substation(substation)
        feeder = row.get("feeder")
        if feeder and feeder not in network.objects:
            network.add_feeder(feeder, substation=substation)


def _add_device(network, source_row: _Row, report) -> bool:
    """Add the row's device, returning whether it was added."""
    row, line, source = source_row.values, source_row.line, source_row.source
    mrid = row.get("mrid")
    if not mrid:
        report.problem(source, line, "no mRID")
        return False
    if mrid in network.objects:
        report.problem(source, line, f"duplicate mRID '{mrid}'")
        return False

    cls, note = _device_class(row.get("type"))
    if note:
        report.problem(source, line, note)

    handled = {"mrid", "name", "type", "feeder", "substation", "phases", "voltage",
               *NODE_COLUMNS}
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

    # Switches and capacitors both have a normal and a present position.
    if "state" in _fields(cls):
        handled |= {"state", "normal_state"}
        if row.get("normal_state"):
            fields["normal_state"] = row["normal_state"].upper()
        if row.get("state"):
            fields["state"] = row["state"].upper()
    if issubclass(cls, Switch):
        handled.add("is_tie")
        fields["is_tie"] = _boolean(row.get("is_tie"))

    if "conductor" in _text_fields(cls):
        handled.add("conductor")
        if row.get("conductor"):
            fields["conductor"] = row["conductor"]

    extras = _extras(row, handled, source_row.extras)
    if note:
        extras["source_type"] = row.get("type", "")
    fields["extras"] = extras

    try:
        network.add(cls(mrid=mrid, **fields))
    except (TypeError, ValueError) as error:
        report.problem(source, line, f"{mrid}: {error}")
        return False
    return True


def _attach(network, source_row: _Row, report) -> None:
    """Attach a device to the nodes its row names.

    Devices are connected wherever their nodes match, so the order the rows
    come in does not matter.
    """
    row, line, source = source_row.values, source_row.line, source_row.source
    nodes = [row[column] for column in NODE_COLUMNS if row.get(column)]
    if len(nodes) == 2 and nodes[0] == nodes[1]:
        report.problem(
            source, line,
            f"{row['mrid']}: both ends are on node '{nodes[0]}', so it connects nothing "
            "to anything; attached once",
        )
    for node in nodes:
        network.attach(row["mrid"], node)


def _add_connection(network, source_row: _Row, report) -> None:
    row, line, source = source_row.values, source_row.line, source_row.source
    first, second = row.get("from_device"), row.get("to_device")
    if not first or not second:
        report.problem(source, line, "needs both from_device and to_device")
        return
    for mrid in (first, second):
        if mrid not in network.objects:
            report.problem(source, line, f"unknown device '{mrid}'")
            return
        if not isinstance(network.objects[mrid], Device):
            kind = network.objects[mrid].TYPE
            report.problem(source, line, f"'{mrid}' is a {kind}, not equipment to connect")
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


def _extras(
    row: dict[str, str], handled: set[str], kept: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Columns the model has no field for, kept so they stay queryable.

    ``kept`` is what a mapping set aside itself. A field it mapped that this
    kind of object has no use for -- kva on a switch -- is kept alongside.
    """
    extras = {
        column: _scalar(value)
        for column, value in row.items()
        if column not in handled
    }
    return extras | (kept or {})


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
        _write(
            base / SUBSTATIONS,
            _with_extras(["mrid", "name", "voltage"], network.substations),
            network.substations,
        ),
        _write(
            base / FEEDERS,
            _with_extras(["mrid", "name", "voltage", "substation", "head"], network.feeders),
            network.feeders,
            extra={"head": lambda f: f.head},
        ),
        _write(
            base / DEVICES, _device_columns(network), network.devices,
            extra=_node_cells(network),
        ),
    ]

    # A pair on a node both devices have in their node columns needs no row.
    columns = len(NODE_COLUMNS)
    in_columns = {mrid: set(network.nodes_of(mrid)[:columns]) for mrid in network.objects}
    edges = sorted(
        (mrid, neighbor)
        for mrid in network.objects
        for neighbor in network.neighbors(mrid)
        if mrid < neighbor and not in_columns[mrid] & in_columns[neighbor]
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
    nodes = list(NODE_COLUMNS) if network.nodes else []
    return _with_extras(base + present + nodes, network.devices)


def _node_cells(network: Network) -> dict:
    """Writers for the node columns: a device's first and second node.

    A device on more than two nodes -- a three-winding transformer read from
    CIM -- keeps its first two here, and its connections through the rest
    are written to connections.csv as pairs.
    """
    return {
        column: (lambda device, index=index: _nth(network.nodes_of(device.mrid), index))
        for index, column in enumerate(NODE_COLUMNS)
    }


def _nth(values: list[str], index: int) -> str | None:
    return values[index] if index < len(values) else None


def _with_extras(columns: list[str], objects: Iterable[GridObject]) -> tuple[str, ...]:
    """``columns``, then every extra the objects carry that is not already one.

    An extra can share a column's name -- a generic device keeps a ``kva``
    cell in extras -- and writing it twice would give the file two headers
    with one meaning.
    """
    taken = {column.lower() for column in columns}
    extras = sorted(
        {key for obj in objects for key in obj.extras if key.lower() not in taken}
    )
    return tuple(columns + extras)


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
