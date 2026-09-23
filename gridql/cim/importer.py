# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read CIM RDF/XML into the semantic model.

Import is the mirror of export. CIM joins equipment through
ConnectivityNodes, and a node may gather any number of terminals -- a bus.
Each one is kept as a connectivity node in the model, identified by its
mRID, so a bus reads as one junction rather than as a ring of the devices
on it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..errors import GridQLError
from ..model import Capacitor, Device, LineSegment, Load, Network, Switch, Transformer
from ..model.feeder import Feeder, Substation
from ..units import convert
from .vocabulary import (
    CIM_NS,
    CIM_TO_MODEL,
    EXT_EXTRAS,
    EXT_HEAD,
    EXT_IS_TIE,
    EXT_PHASES,
    GRIDQL_NS,
    MODEL_DESCRIPTION_PREFIX,
    RDF_NS,
    STRUCTURAL_CLASSES,
    is_cim_namespace,
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
    document = _parse(_read_text(source))
    document.network.source = str(source)
    return document


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
    by_identifier: dict[str, _Record] = {}
    unread: dict[str, int] = {}
    for element in root:
        namespace, _local = split_tag(element.tag)
        if not is_cim_namespace(namespace):
            if namespace and not namespace.startswith(MODEL_DESCRIPTION_PREFIX):
                unread[namespace] = unread.get(namespace, 0) + 1
            continue
        record = _read_record(element)
        if record is None:
            continue
        earlier = by_identifier.get(record.xml_id)
        if earlier is None:
            by_identifier[record.xml_id] = record
        else:
            # RDF lets one resource be described more than once -- an
            # rdf:ID, then rdf:about blocks adding to it. They are one object.
            _merge(earlier, record)

    if unread:
        described = ", ".join(f"{ns} x{count}" for ns, count in sorted(unread.items()))
        report.notes.append(f"ignored elements in namespaces GridQL does not read: {described}")

    return _build(list(by_identifier.values()), report)


def _merge(record: _Record, more: _Record) -> None:
    """Fold a further description of a resource into the first one."""
    record.values.update(more.values)
    record.references.update(more.references)
    if model_class_for(record.cim_class) is None and model_class_for(more.cim_class):
        record.cim_class = more.cim_class
    record.mrid = record.values.get(f"{CIM_NS}IdentifiedObject.mRID") or record.xml_id


def _read_record(element: ET.Element) -> _Record | None:
    _namespace, cim_class = split_tag(element.tag)

    identifier = element.get(f"{{{RDF_NS}}}ID") or element.get(f"{{{RDF_NS}}}about")
    if identifier is None:
        return None
    identifier = strip_reference(identifier)

    values: dict[str, str] = {}
    references: dict[str, str] = {}
    for child in element:
        child_namespace, child_name = split_tag(child.tag)
        if is_cim_namespace(child_namespace):
            child_namespace = CIM_NS  # one spelling, whichever release wrote it
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

    def unique(record: _Record) -> bool:
        """Two distinct resources may still claim one mRID; keep the first."""
        if record.mrid not in network.objects:
            return True
        report.notes.append(
            f"{record.cim_class} {record.xml_id}: mRID '{record.mrid}' is already "
            "used by another object, so it was skipped"
        )
        return False

    for record in grouped.get("Substation", []):
        if not unique(record):
            continue
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
            if not unique(record):
                continue
            substation = resolve(record.reference(CIM_NS, "Feeder.NormalEnergizingSubstation"))
            if substation is not None and substation not in network.objects:
                # Named but not described: keep the link with a bare
                # substation, rather than a feeder pointing at nothing.
                network.add_substation(substation)
                report.substations += 1
                report.notes.append(
                    f"{record.mrid}: substation '{substation}' is not described in the "
                    "document, so it was created with no attributes"
                )
            elif substation is not None and not isinstance(
                network.objects[substation], Substation
            ):
                report.notes.append(
                    f"{record.mrid}: NormalEnergizingSubstation '{substation}' is a "
                    f"{network.objects[substation].TYPE}, not a substation; ignored"
                )
                substation = None
            network.add_feeder(
                record.mrid,
                name=_name(record),
                voltage=voltage_of(record),
                substation=substation,
                extras=_extras(record),
            )
            heads[record.mrid] = resolve(record.reference(GRIDQL_NS, EXT_HEAD))
            report.feeders += 1

    ends = _transformer_ends(grouped, resolve, base_voltages)
    phases_of = _phases(grouped, resolve)
    wired = {
        resolve(record.reference(CIM_NS, "Terminal.ConductingEquipment"))
        for record in grouped.get("Terminal", [])
    }

    for record in records:
        cls = model_class_for(record.cim_class)
        if (
            cls is None
            and record.cim_class not in STRUCTURAL_CLASSES
            and (record.mrid in wired or record.value(GRIDQL_NS, EXT_EXTRAS))
        ):
            # Equipment GridQL has no class for -- an EnergySource, a series
            # compensator, an inverter -- still has terminals, and dropping
            # it would cut the circuit wherever it stands in series.
            cls = Device
        if cls is None:
            if record.cim_class not in STRUCTURAL_CLASSES:
                report.ignored[record.cim_class] = report.ignored.get(record.cim_class, 0) + 1
            continue
        if not issubclass(cls, Device):
            continue  # feeders and substations were built above
        if not unique(record):
            continue

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
        if cls is Device and record.cim_class not in CIM_TO_MODEL:
            fields["extras"] = {"cim_class": record.cim_class, **fields["extras"]}
        phases = record.value(GRIDQL_NS, EXT_PHASES) or phases_of.get(record.mrid)
        if phases:
            fields["phases"] = phases
        fields.update(_type_fields(cls, record, ends))
        if fields["voltage"] is None and issubclass(cls, Transformer):
            fields["voltage"] = ends.get(record.mrid, {}).get(1, {}).get("base_voltage")

        network.add(cls(mrid=record.mrid, **fields))
        report.devices += 1

    report.connections = _connect(network, grouped, resolve, report)
    _assign_heads(network, heads, report)
    return CimDocument(network, report)


def _transformer_ends(
    grouped: dict[str, list[_Record]], resolve, base_voltages: dict[str, float | None]
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Ratings gathered from a transformer's windings, keyed by its mRID.

    CIM describes a transformer one of two ways. A PowerTransformerEnd
    carries its own ratedS and ratedU. A transformer built from tanks -- a
    bank of single-phase units, or one pole-top unit -- carries none: each
    TransformerTank points at a TransformerTankInfo datasheet, and the
    ratings are on that datasheet's TransformerEndInfo. A bank's rating is
    the sum of its tanks'.
    """
    ends: dict[str, dict[int, dict[str, float | None]]] = {}

    def winding(owner: str, number: int) -> dict[str, float | None]:
        return ends.setdefault(owner, {}).setdefault(
            number, {"ratedS": None, "ratedU": None, "base_voltage": None}
        )

    def base_voltage(record: _Record) -> float | None:
        volts = base_voltages.get(record.reference(CIM_NS, "TransformerEnd.BaseVoltage"))
        return None if volts is None else volts / 1000.0

    for cim_class in ("PowerTransformerEnd", "TransformerEnd"):
        for record in grouped.get(cim_class, []):
            owner = resolve(record.reference(CIM_NS, "PowerTransformerEnd.PowerTransformer"))
            if owner is None:
                continue
            number = int(_float(record.value(CIM_NS, "TransformerEnd.endNumber")) or 0)
            end = winding(owner, number)
            end["ratedS"] = _float(record.value(CIM_NS, "PowerTransformerEnd.ratedS"))
            end["ratedU"] = _float(record.value(CIM_NS, "PowerTransformerEnd.ratedU"))
            end["base_voltage"] = base_voltage(record)

    datasheets: dict[str, dict[int, _Record]] = {}
    for record in grouped.get("TransformerEndInfo", []):
        sheet = record.reference(CIM_NS, "TransformerEndInfo.TransformerTankInfo")
        number = int(_float(record.value(CIM_NS, "TransformerEndInfo.endNumber")) or 0)
        if sheet is not None:
            datasheets.setdefault(sheet, {})[number] = record

    self_rated = {
        owner for owner, windings in ends.items()
        if any(end["ratedS"] is not None for end in windings.values())
    }
    tank_owner: dict[str, str] = {}
    for tank in grouped.get("TransformerTank", []):
        owner = resolve(tank.reference(CIM_NS, "TransformerTank.PowerTransformer"))
        if owner is None or owner in self_rated:
            continue
        tank_owner[tank.xml_id] = owner
        sheet = tank.reference(CIM_NS, "TransformerTank.TransformerTankInfo") or tank.reference(
            CIM_NS, "PowerSystemResource.AssetDatasheet"
        )
        for number, info in datasheets.get(sheet, {}).items():
            end = winding(owner, number)
            rated_s = _float(info.value(CIM_NS, "TransformerEndInfo.ratedS"))
            if rated_s is not None:
                end["ratedS"] = (end["ratedS"] or 0.0) + rated_s
            if end["ratedU"] is None:
                end["ratedU"] = _float(info.value(CIM_NS, "TransformerEndInfo.ratedU"))

    for record in grouped.get("TransformerTankEnd", []):
        owner = tank_owner.get(record.reference(CIM_NS, "TransformerTankEnd.TransformerTank") or "")
        number = int(_float(record.value(CIM_NS, "TransformerEnd.endNumber")) or 0)
        if owner is not None and winding(owner, number)["base_voltage"] is None:
            winding(owner, number)["base_voltage"] = base_voltage(record)

    return ends


#: Where each kind of per-phase object names its equipment and its phase.
_PHASE_RECORDS = (
    ("ACLineSegmentPhase", "ACLineSegmentPhase.ACLineSegment", "ACLineSegmentPhase.phase"),
    ("EnergyConsumerPhase", "EnergyConsumerPhase.EnergyConsumer", "EnergyConsumerPhase.phase"),
    ("SwitchPhase", "SwitchPhase.Switch", "SwitchPhase.phaseSide1"),
    ("ShuntCompensatorPhase", "ShuntCompensatorPhase.ShuntCompensator",
     "ShuntCompensatorPhase.phase"),
    ("LinearShuntCompensatorPhase", "ShuntCompensatorPhase.ShuntCompensator",
     "ShuntCompensatorPhase.phase"),
)

#: Phase letters in the order GridQL writes them. The neutral is left out,
#: as it is in "ABC"; s1 and s2 are the two legs of a split-phase service.
_PHASE_ORDER = ("A", "B", "C", "s1", "s2")
_PHASE_TOKEN = re.compile(r"s1|s2|[ABCN]")


def _phases(grouped: dict[str, list[_Record]], resolve) -> dict[str, str]:
    """Each device's phasing, read from the per-phase objects that describe it.

    CIM gives equipment no phase attribute. Equipment on fewer than three
    phases has a child object per phase instead, and equipment with none is
    three-phase -- which is also GridQL's default, so only the devices that
    have them need an answer here. A transformer takes the phasing of its
    primary tank ends.
    """
    found: dict[str, set[str]] = {}
    for cim_class, owner_name, phase_name in _PHASE_RECORDS:
        for record in grouped.get(cim_class, []):
            owner = resolve(record.reference(CIM_NS, owner_name))
            phase = _enumeration(record.reference(CIM_NS, phase_name))
            if owner is not None and phase in _PHASE_ORDER:
                found.setdefault(owner, set()).add(phase)

    tanks = {
        tank.xml_id: resolve(tank.reference(CIM_NS, "TransformerTank.PowerTransformer"))
        for tank in grouped.get("TransformerTank", [])
    }
    for record in grouped.get("TransformerTankEnd", []):
        if int(_float(record.value(CIM_NS, "TransformerEnd.endNumber")) or 0) != 1:
            continue
        owner = tanks.get(record.reference(CIM_NS, "TransformerTankEnd.TransformerTank") or "")
        ordered = _enumeration(record.reference(CIM_NS, "TransformerTankEnd.orderedPhases"))
        if owner is not None and ordered:
            found.setdefault(owner, set()).update(
                token for token in _PHASE_TOKEN.findall(ordered) if token != "N"
            )

    return {
        owner: "".join(phase for phase in _PHASE_ORDER if phase in phases)
        for owner, phases in found.items()
        if phases
    }


def _enumeration(reference: str | None) -> str | None:
    """``http://iec.ch/TC57/CIM100#SinglePhaseKind.A`` -> ``A``."""
    if not reference:
        return None
    return reference.rsplit("#", 1)[-1].rsplit(".", 1)[-1]


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
        if fields["kvar"] is None:
            fields["kvar"] = _capacitor_kvar(record)
        normal_state = record.value(GRIDQL_NS, "normalState") or _in_service(
            record.value(CIM_NS, "ShuntCompensator.normalSections")
        )
        if normal_state:
            fields["normal_state"] = normal_state
        state = record.value(GRIDQL_NS, "state") or _in_service(
            record.value(CIM_NS, "ShuntCompensator.sections")
        )
        if state:
            fields["state"] = state

    return fields


def _capacitor_kvar(record: _Record) -> float | None:
    """A bank's rating from its susceptance: Q = B * U^2 over every section.

    bPerSection is the positive-sequence susceptance, so with nomU line to
    line this is the three-phase total, and with a single-phase bank's
    nomU it is that bank's rating.
    """
    susceptance = _float(record.value(CIM_NS, "LinearShuntCompensator.bPerSection"))
    volts = _float(record.value(CIM_NS, "ShuntCompensator.nomU"))
    if susceptance is None or volts is None:
        return None
    sections = _float(record.value(CIM_NS, "ShuntCompensator.maximumSections")) or 1.0
    return round(susceptance * volts * volts * sections / 1000.0, 3)


def _in_service(sections: str | None) -> str | None:
    """A capacitor with no sections switched in is open."""
    count = _float(sections)
    if count is None:
        return None
    return "CLOSED" if count > 0 else "OPEN"


def _connect(network: Network, grouped: dict[str, list[_Record]], resolve, report) -> int:
    """Attach each piece of equipment to the ConnectivityNodes its Terminals name."""
    terminals: list[tuple[str, str, str]] = []
    for record in grouped.get("Terminal", []):
        equipment = resolve(record.reference(CIM_NS, "Terminal.ConductingEquipment"))
        node = resolve(record.reference(CIM_NS, "Terminal.ConnectivityNode"))
        if equipment is None or node is None:
            continue
        if not isinstance(network.objects.get(equipment), Device):
            continue  # unknown, or a container: only equipment has terminals
        sequence = record.value(CIM_NS, "ACDCTerminal.sequenceNumber") or ""
        terminals.append((equipment, sequence.zfill(9), node))

    # In terminal order where the document numbers them, so a device's
    # first node is its first terminal's; otherwise in document order.
    for equipment, _sequence, node in sorted(terminals, key=lambda t: (t[0], t[1])):
        network.attach(equipment, node)

    return sum(len(network.neighbors(m)) for m in network.objects) // 2


def _assign_heads(network: Network, heads: dict[str, str | None], report: ImportReport) -> None:
    """Restore each feeder's source device, inferring it when CIM did not say."""
    for feeder in network.feeders:
        recorded = heads.get(feeder.mrid)
        if recorded and recorded in network.objects:
            feeder.head = recorded
            continue
        # An EnergySource is the document saying where power enters, which
        # beats any guess from the equipment around it.
        sources = [
            device.mrid
            for device in network.devices
            if device.feeder == feeder.mrid and device.extras.get("cim_class") == "EnergySource"
        ]
        if len(sources) == 1:
            feeder.head = sources[0]
            report.notes.append(f"{feeder.mrid}: headed by its EnergySource {sources[0]}")
    report.notes.extend(network.infer_heads())


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
    # xsd:boolean allows 1 and 0 as well as true and false.
    return str(text).strip().lower() in ("true", "1")
