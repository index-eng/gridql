"""Write a network -- or any slice of one -- as CIM RDF/XML.

This is the half of GridQL the overview is really about: pull out the part of
the system you care about with a query, and get a standards-based CIM document
back without hand-building the structure.

Two things are synthesised on the way out, because CIM models connectivity
indirectly:

* every edge becomes a ``ConnectivityNode`` joining two ``Terminal`` objects;
* every distinct voltage becomes a ``BaseVoltage`` the equipment refers to.

Identifiers are derived from mRIDs rather than freshly minted UUIDs, so
exporting the same network twice produces the same document and a diff means
the network actually changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET

from ..version import __version__
from ..model import (
    Capacitor,
    Device,
    Feeder,
    GridObject,
    LineSegment,
    Load,
    Network,
    Substation,
    Switch,
    Transformer,
)
from ..units import convert
from .vocabulary import (
    CIM_NS,
    EXT_EXTRAS,
    EXT_HEAD,
    EXT_IS_TIE,
    EXT_PHASES,
    GRIDQL_NS,
    NAMESPACES,
    RDF_NS,
    tag,
    xml_id,
)


def export_summary(
    network: Network, objects: Iterable[GridObject] | None = None
) -> dict[str, int]:
    """What an export of this selection would contain."""
    selection = _Selection(network, objects)
    return {
        "devices": len(selection.devices),
        "feeders": len(selection.feeders),
        "substations": len(selection.substations),
        "connections": len(selection.connectivity_nodes()),
    }


def export_network(
    network: Network,
    objects: Iterable[GridObject] | None = None,
    path: str | Path | None = None,
) -> str:
    """Serialise a network, or the slice named by ``objects``, as CIM RDF/XML.

    Passing a query result as ``objects`` exports just that equipment, with
    the containers, voltages and connectivity needed for the document to
    stand on its own.
    """
    selection = _Selection(network, objects)
    root = ET.Element(tag(RDF_NS, "RDF"))
    root.append(ET.Comment(_header(selection)))

    for volts, identifier in selection.base_voltages():
        element = _identified(root, "BaseVoltage", identifier, mrid=identifier)
        _text(element, CIM_NS, "BaseVoltage.nominalVoltage", _number(volts))

    for substation in selection.substations:
        element = _identified(root, "Substation", substation.mrid)
        _text(element, CIM_NS, "IdentifiedObject.name", substation.name)
        _base_voltage(element, selection, substation.voltage, extension=True)
        _extras(element, substation)

    for feeder in selection.feeders:
        element = _identified(root, "Feeder", feeder.mrid)
        _text(element, CIM_NS, "IdentifiedObject.name", feeder.name)
        if feeder.substation and feeder.substation in selection.by_mrid:
            _ref(element, CIM_NS, "Feeder.NormalEnergizingSubstation", feeder.substation)
        _base_voltage(element, selection, feeder.voltage, extension=True)
        if feeder.head and feeder.head in selection.by_mrid:
            _ref(element, GRIDQL_NS, EXT_HEAD, feeder.head)
        _extras(element, feeder)

    for device in selection.devices:
        _write_device(root, selection, device)

    for device in selection.devices:
        if isinstance(device, Transformer):
            _write_transformer_ends(root, selection, device)

    for node, _first, _second in selection.connectivity_nodes():
        _identified(root, "ConnectivityNode", node)

    for node, first, second in selection.connectivity_nodes():
        for device_mrid, other in ((first, second), (second, first)):
            terminal = _identified(root, "Terminal", _terminal_id(device_mrid, other))
            _ref(terminal, CIM_NS, "Terminal.ConductingEquipment", device_mrid)
            _ref(terminal, CIM_NS, "Terminal.ConnectivityNode", node)

    ET.indent(root, space="  ")
    document = ET.tostring(root, encoding="unicode", xml_declaration=True)
    if path is not None:
        Path(path).write_text(document + "\n", encoding="utf-8")
    return document


# -- what goes in the document -----------------------------------------


class _Selection:
    """The objects to export, plus everything they need to make sense."""

    def __init__(self, network: Network, objects: Iterable[GridObject] | None) -> None:
        self.network = network

        chosen = list(network.objects.values()) if objects is None else list(objects)
        self.by_mrid: dict[str, GridObject] = {obj.mrid: obj for obj in chosen}

        # A slice is only meaningful with its containers, so pull them in.
        for obj in list(self.by_mrid.values()):
            for container in (getattr(obj, "feeder", None), getattr(obj, "substation", None)):
                if container and container not in self.by_mrid and container in network.objects:
                    self.by_mrid[container] = network.objects[container]

        self.devices: list[Device] = sorted(
            (o for o in self.by_mrid.values() if isinstance(o, Device)), key=lambda o: o.mrid
        )
        self.feeders: list[Feeder] = sorted(
            (o for o in self.by_mrid.values() if isinstance(o, Feeder)), key=lambda o: o.mrid
        )
        self.substations: list[Substation] = sorted(
            (o for o in self.by_mrid.values() if isinstance(o, Substation)), key=lambda o: o.mrid
        )

        self._voltages: dict[float, str] = {}
        for obj in (*self.devices, *self.feeders, *self.substations):
            voltage = getattr(obj, "voltage", None)
            if voltage is not None and voltage not in self._voltages:
                self._voltages[voltage] = _base_voltage_id(voltage)

    def base_voltages(self) -> list[tuple[float, str]]:
        return [(kv * 1000.0, identifier) for kv, identifier in sorted(self._voltages.items())]

    def base_voltage_id(self, kilovolts: float | None) -> str | None:
        return None if kilovolts is None else self._voltages.get(kilovolts)

    def connectivity_nodes(self) -> list[tuple[str, str, str]]:
        """One node per edge that lies wholly inside the selection."""
        included = {device.mrid for device in self.devices}
        edges = {
            (min(mrid, neighbor), max(mrid, neighbor))
            for mrid in included
            for neighbor in self.network.neighbors(mrid)
            if neighbor in included
        }
        return [(_node_id(a, b), a, b) for a, b in sorted(edges)]


def _header(selection: _Selection) -> str:
    return (
        f" Generated by GridQL {__version__}: "
        f"{len(selection.devices)} devices, {len(selection.feeders)} feeders, "
        f"{len(selection.substations)} substations. "
        f"The gridql: namespace carries attributes CIM has no place for. "
    )


# -- equipment ----------------------------------------------------------


def _write_device(root: ET.Element, selection: _Selection, device: Device) -> None:
    element = _identified(root, device.CIM_CLASS, device.mrid)
    _text(element, CIM_NS, "IdentifiedObject.name", device.name)

    if device.feeder and device.feeder in selection.by_mrid:
        _ref(element, CIM_NS, "Equipment.EquipmentContainer", device.feeder)
    _base_voltage(element, selection, device.voltage)

    if device.phases:
        _text(element, GRIDQL_NS, EXT_PHASES, device.phases)

    if isinstance(device, Switch):
        _text(element, CIM_NS, "Switch.normalOpen", _boolean(device.normal_state == "OPEN"))
        _text(element, CIM_NS, "Switch.open", _boolean(device.state == "OPEN"))
        if device.is_tie:
            _text(element, GRIDQL_NS, EXT_IS_TIE, _boolean(True))

    elif isinstance(device, LineSegment):
        if device.length is not None:
            _text(element, CIM_NS, "Conductor.length", _number(convert(device.length, "ft", "m")))
        if device.conductor:
            _text(element, GRIDQL_NS, "conductor", device.conductor)
        if device.ampacity is not None:
            _text(element, GRIDQL_NS, "ampacity", _number(device.ampacity))

    elif isinstance(device, Load):
        if device.kw is not None:
            _text(element, CIM_NS, "EnergyConsumer.p", _number(device.kw * 1000.0))
        if device.kvar is not None:
            _text(element, CIM_NS, "EnergyConsumer.q", _number(device.kvar * 1000.0))

    elif isinstance(device, Capacitor):
        if device.voltage is not None:
            _text(element, CIM_NS, "ShuntCompensator.nomU", _number(device.voltage * 1000.0))
        if device.kvar is not None:
            _text(element, GRIDQL_NS, "kvar", _number(device.kvar))
        _text(element, GRIDQL_NS, "normalState", device.normal_state)
        _text(element, GRIDQL_NS, "state", device.state)

    _extras(element, device)


def _write_transformer_ends(
    root: ET.Element, selection: _Selection, transformer: Transformer
) -> None:
    """CIM keeps a transformer's ratings on its ends, not on the transformer."""
    windings = (
        (1, transformer.primary_voltage),
        (2, transformer.secondary_voltage),
    )
    for number, kilovolts in windings:
        if kilovolts is None and transformer.kva is None:
            continue
        end_mrid = f"{transformer.mrid}_END_{number}"
        element = _identified(root, "PowerTransformerEnd", end_mrid)
        _text(element, CIM_NS, "TransformerEnd.endNumber", number)
        _ref(element, CIM_NS, "PowerTransformerEnd.PowerTransformer", transformer.mrid)
        if transformer.kva is not None:
            _text(element, CIM_NS, "PowerTransformerEnd.ratedS", _number(transformer.kva * 1000.0))
        if kilovolts is not None:
            _text(element, CIM_NS, "PowerTransformerEnd.ratedU", _number(kilovolts * 1000.0))


# -- element helpers ----------------------------------------------------


def _identified(
    root: ET.Element, cim_class: str, identifier: str, mrid: str | None = None
) -> ET.Element:
    element = ET.SubElement(root, tag(CIM_NS, cim_class))
    element.set(tag(RDF_NS, "ID"), xml_id(identifier))
    _text(element, CIM_NS, "IdentifiedObject.mRID", mrid or identifier)
    return element


def _text(parent: ET.Element, namespace: str, name: str, value: object) -> None:
    ET.SubElement(parent, tag(namespace, name)).text = str(value)


def _ref(parent: ET.Element, namespace: str, name: str, mrid: str) -> None:
    element = ET.SubElement(parent, tag(namespace, name))
    element.set(tag(RDF_NS, "resource"), f"#{xml_id(mrid)}")


def _base_voltage(
    element: ET.Element, selection: _Selection, kilovolts: float | None, extension: bool = False
) -> None:
    identifier = selection.base_voltage_id(kilovolts)
    if identifier is None:
        return
    if extension:
        # Containers are not ConductingEquipment, so CIM gives them no
        # BaseVoltage association; keep the link in the private namespace.
        _ref(element, GRIDQL_NS, "BaseVoltage", identifier)
    else:
        _ref(element, CIM_NS, "ConductingEquipment.BaseVoltage", identifier)


def _extras(element: ET.Element, obj: GridObject) -> None:
    if obj.extras:
        _text(element, GRIDQL_NS, EXT_EXTRAS, json.dumps(obj.extras, sort_keys=True))


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _base_voltage_id(kilovolts: float) -> str:
    return f"BaseVoltage_{_number(kilovolts * 1000.0)}V".replace(".", "_")


def _node_id(first: str, second: str) -> str:
    return f"CN_{xml_id(first)}_{xml_id(second)}"


def _terminal_id(device: str, other: str) -> str:
    return f"T_{xml_id(device)}_{xml_id(other)}"


def register_namespaces() -> None:
    """Make ElementTree emit readable cim:/rdf: prefixes rather than ns0:."""
    for prefix, uri in NAMESPACES.items():
        ET.register_namespace(prefix, uri)


register_namespaces()
