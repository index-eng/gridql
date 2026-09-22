"""Read CIM RDF/XML into the semantic model.

Import is the mirror of export, with one asymmetry worth knowing about.
CIM joins equipment through ConnectivityNodes, and a node may gather any
number of terminals -- a bus. The model stores plain edges, so a node with
*n* terminals becomes edges between every pair of its devices. For the
two-terminal nodes this package writes that is exactly lossless; for a real
bus it keeps the right answer for "what is connected here" and for feeder
traversal, while not pretending to preserve the node object itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..errors import GridQLError
from ..model import Breaker, Capacitor, Device, LineSegment, Load, Network, Switch, Transformer
from ..model.feeder import Feeder, Substation
from ..units import convert
from .vocabulary import (
    CIM_NS,
    EXT_EXTRAS,
    EXT_HEAD,
    EXT_IS_TIE,
    EXT_PHASES,
    GRIDQL_NS,
    RDF_NS,
    STRUCTURAL_CLASSES,
    model_class_for,
    split_tag,
    strip_reference,
)


class CimImportError(GridQLError):
    """The document could not be read as CIM."""


@dataclass
class ImportReport:
    """What the importer made of a document."""

    substations: int = 0
    feeders: int = 0
    devices: int = 0
    connections: int = 0
    ignored: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"imported {self.devices} devices, {self.feeders} feeders, "
            f"{self.substations} substations, {self.connections} connections"
        ]
        if self.ignored:
            described = ", ".join(
                f"{name} x{count}" for name, count in sorted(self.ignored.items())
            )
            lines.append(f"ignored unsupported classes: {described}")
        lines.extend(self.notes)
        return "\n".join(lines)


@dataclass
class CimDocument:
    network: Network
    report: ImportReport


def import_network(source: str | Path) -> Network:
    """Read a CIM file and return the network it describes."""
    return read_cim(source).network


def read_cim(source: str | Path) -> CimDocument:
    """Read a CIM file, returning the network and a report on what was found."""
    return _parse(_read_text(source))


def loads_cim(text: str) -> CimDocument:
    """Read CIM RDF/XML held in a string."""
    return _parse(text)


def _read_text(source: str | Path) -> str:
    path = Path(source)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CimImportError(f"cannot read {path}: {error.strerror or error}") from error


# -- parsing ------------------------------------------------------------


@dataclass
class _Record:
    """One RDF resource: its class, its identifiers and its properties."""

    cim_class: str
    xml_id: str
    mrid: str
    values: dict[str, str]
    references: dict[str, str]

    def value(self, namespace: str, name: str) -> str | None:
        return self.values.get(f"{namespace}{name}")

    def reference(self, namespace: str, name: str) -> str | None:
        return self.references.get(f"{namespace}{name}")


def _parse(text: str) -> CimDocument:
    if "<!DOCTYPE" in text:
        # Entity declarations are an attack surface and CIM has no use for them.
        raise CimImportError("refusing a document with a DOCTYPE declaration")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise CimImportError(f"not well-formed XML: {error}") from error

    if split_tag(root.tag) != (RDF_NS, "RDF"):
        raise CimImportError("the document root is not rdf:RDF")

    report = ImportReport()
    records: list[_Record] = []
    for element in root:
        record = _read_record(element)
        if record is not None:
            records.append(record)

    return _build(records, report)


def _read_record(element: ET.Element) -> _Record | None:
    namespace, cim_class = split_tag(element.tag)
    if namespace != CIM_NS:
        return None

    identifier = element.get(f"{{{RDF_NS}}}ID") or element.get(f"{{{RDF_NS}}}about")
    if identifier is None:
        return None
    identifier = strip_reference(identifier)

    values: dict[str, str] = {}
    references: dict[str, str] = {}
    for child in element:
        child_namespace, child_name = split_tag(child.tag)
        key = f"{child_namespace}{child_name}"
        resource = child.get(f"{{{RDF_NS}}}resource")
        if resource is not None:
            references[key] = strip_reference(resource)
        elif child.text is not None:
            values[key] = child.text.strip()

    mrid = values.get(f"{CIM_NS}IdentifiedObject.mRID") or identifier
    return _Record(cim_class, identifier, mrid, values, references)


# -- building the network ----------------------------------------------


def _build(records: list[_Record], report: ImportReport) -> CimDocument:
    by_xml_id = {record.xml_id: record for record in records}

    def resolve(xml_id: str | None) -> str | None:
        """An rdf:resource pointer, as the mRID it names."""
        if xml_id is None:
            return None
        target = by_xml_id.get(xml_id)
        return target.mrid if target else xml_id

    grouped: dict[str, list[_Record]] = {}
    for record in records:
        grouped.setdefault(record.cim_class, []).append(record)

    base_voltages = {
        record.xml_id: _float(record.value(CIM_NS, "BaseVoltage.nominalVoltage"))
        for record in grouped.get("BaseVoltage", [])
    }

    def voltage_of(record: _Record) -> float | None:
        """Nominal voltage in kV, from whichever association carries it."""
        for namespace, name in (
            (CIM_NS, "ConductingEquipment.BaseVoltage"),
            (GRIDQL_NS, "BaseVoltage"),
        ):
            pointer = record.reference(namespace, name)
            if pointer is not None and base_voltages.get(pointer) is not None:
                return base_voltages[pointer] / 1000.0
        return None

    network = Network()

    for record in grouped.get("Substation", []):
        network.add_substation(
            record.mrid,
            name=_name(record),
            voltage=voltage_of(record),
            extras=_extras(record),
        )
        report.substations += 1

    heads: dict[str, str | None] = {}
    for cim_class in ("Feeder", "Line"):
        for record in grouped.get(cim_class, []):
            network.add_feeder(
                record.mrid,
                name=_name(record),
                voltage=voltage_of(record),
                substation=resolve(record.reference(CIM_NS, "Feeder.NormalEnergizingSubstation")),
                extras=_extras(record),
            )
            heads[record.mrid] = resolve(record.reference(GRIDQL_NS, EXT_HEAD))
            report.feeders += 1

    ends = _transformer_ends(grouped, resolve)

    for record in records:
        cls = model_class_for(record.cim_class)
        if cls is None:
            if record.cim_class not in STRUCTURAL_CLASSES:
                report.ignored[record.cim_class] = report.ignored.get(record.cim_class, 0) + 1
            continue
        if not issubclass(cls, Device):
            continue  # feeders and substations were built above

        container = resolve(record.reference(CIM_NS, "Equipment.EquipmentContainer"))
        feeder_mrid = container if container in {f.mrid for f in network.feeders} else None
        substation_mrid = None
        if feeder_mrid is not None:
            substation_mrid = network.objects[feeder_mrid].substation
        elif container in {s.mrid for s in network.substations}:
            substation_mrid = container

        fields: dict[str, Any] = {
            "name": _name(record),
            "voltage": voltage_of(record),
            "feeder": feeder_mrid,
            "substation": substation_mrid,
            "extras": _extras(record),
        }
        phases = record.value(GRIDQL_NS, EXT_PHASES)
        if phases:
            fields["phases"] = phases
        fields.update(_type_fields(cls, record, ends))

        network.add(cls(mrid=record.mrid, **fields))
        report.devices += 1

    report.connections = _connect(network, grouped, resolve, report)
    _assign_heads(network, heads, report)
    return CimDocument(network, report)


def _transformer_ends(
    grouped: dict[str, list[_Record]], resolve
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Ratings gathered from PowerTransformerEnd, keyed by transformer mRID."""
    ends: dict[str, dict[int, dict[str, float | None]]] = {}
    for cim_class in ("PowerTransformerEnd", "TransformerEnd"):
        for record in grouped.get(cim_class, []):
            owner = resolve(record.reference(CIM_NS, "PowerTransformerEnd.PowerTransformer"))
            if owner is None:
                continue
            number = int(_float(record.value(CIM_NS, "TransformerEnd.endNumber")) or 0)
            ends.setdefault(owner, {})[number] = {
                "ratedS": _float(record.value(CIM_NS, "PowerTransformerEnd.ratedS")),
                "ratedU": _float(record.value(CIM_NS, "PowerTransformerEnd.ratedU")),
            }
    return ends


def _type_fields(cls: type, record: _Record, ends: dict) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    if issubclass(cls, Switch):
        normal_open = record.value(CIM_NS, "Switch.normalOpen")
        present_open = record.value(CIM_NS, "Switch.open")
        fields["normal_state"] = "OPEN" if _boolean(normal_open) else "CLOSED"
        fields["state"] = (
            fields["normal_state"] if present_open is None
            else ("OPEN" if _boolean(present_open) else "CLOSED")
        )
        fields["is_tie"] = _boolean(record.value(GRIDQL_NS, EXT_IS_TIE))

    elif issubclass(cls, Transformer):
        windings = ends.get(record.mrid, {})
        rated_s = next(
            (w["ratedS"] for w in windings.values() if w["ratedS"] is not None), None
        )
        fields["kva"] = None if rated_s is None else rated_s / 1000.0
        for number, key in ((1, "primary_voltage"), (2, "secondary_voltage")):
            rated_u = windings.get(number, {}).get("ratedU")
            fields[key] = None if rated_u is None else rated_u / 1000.0

    elif issubclass(cls, LineSegment):
        metres = _float(record.value(CIM_NS, "Conductor.length"))
        # Round to 0.1 mm: finer than any survey, and it keeps a round trip
        # through metres from accumulating floating-point drift.
        fields["length"] = None if metres is None else round(convert(metres, "m", "ft"), 4)
        fields["conductor"] = record.value(GRIDQL_NS, "conductor")
        fields["ampacity"] = _float(record.value(GRIDQL_NS, "ampacity"))

    elif issubclass(cls, Load):
        watts = _float(record.value(CIM_NS, "EnergyConsumer.p"))
        vars_ = _float(record.value(CIM_NS, "EnergyConsumer.q"))
        fields["kw"] = None if watts is None else watts / 1000.0
        fields["kvar"] = None if vars_ is None else vars_ / 1000.0

    elif issubclass(cls, Capacitor):
        fields["kvar"] = _float(record.value(GRIDQL_NS, "kvar"))
        normal_state = record.value(GRIDQL_NS, "normalState")
        if normal_state:
            fields["normal_state"] = normal_state
        state = record.value(GRIDQL_NS, "state")
        if state:
            fields["state"] = state

    return fields


def _connect(network: Network, grouped: dict[str, list[_Record]], resolve, report) -> int:
    """Turn Terminals and ConnectivityNodes back into edges."""
    at_node: dict[str, list[str]] = {}
    for record in grouped.get("Terminal", []):
        equipment = resolve(record.reference(CIM_NS, "Terminal.ConductingEquipment"))
        node = record.reference(CIM_NS, "Terminal.ConnectivityNode")
        if equipment is None or node is None or equipment not in network.objects:
            continue
        members = at_node.setdefault(node, [])
        if equipment not in members:
            members.append(equipment)

    edges: set[tuple[str, str]] = set()
    buses = 0
    for members in at_node.values():
        if len(members) > 2:
            buses += 1
        for first, second in combinations(sorted(members), 2):
            edges.add((first, second))

    for first, second in sorted(edges):
        network.connect(first, second)

    if buses:
        report.notes.append(
            f"{buses} connectivity node(s) joined more than two terminals; "
            "each was expanded into edges between every pair of its devices"
        )
    return len(edges)


def _assign_heads(network: Network, heads: dict[str, str | None], report: ImportReport) -> None:
    """Restore each feeder's source device, inferring it when CIM did not say."""
    for feeder in network.feeders:
        recorded = heads.get(feeder.mrid)
        if recorded and recorded in network.objects:
            feeder.head = recorded
            continue

        breakers = [
            device.mrid
            for device in network.devices
            if device.feeder == feeder.mrid and isinstance(device, Breaker)
        ]
        if len(breakers) == 1:
            feeder.head = breakers[0]
            report.notes.append(
                f"{feeder.mrid}: no head recorded, inferred {breakers[0]} "
                "as the only breaker on the feeder"
            )
        else:
            report.notes.append(
                f"{feeder.mrid}: no head recorded and none could be inferred, so "
                "DOWNSTREAM OF / UPSTREAM OF will return nothing for it; "
                "set feeder.head to fix"
            )


# -- small conversions --------------------------------------------------


def _name(record: _Record) -> str:
    return record.value(CIM_NS, "IdentifiedObject.name") or record.mrid


def _extras(record: _Record) -> dict[str, Any]:
    raw = record.value(GRIDQL_NS, EXT_EXTRAS)
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _boolean(text: str | None) -> bool:
    return str(text).strip().lower() == "true"
