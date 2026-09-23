# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The CIM vocabulary GridQL reads and writes, and the mapping to the model.

CIM is the interchange vocabulary, not the internal one. Two consequences run
through this package:

* **Connectivity.** CIM never joins equipment directly. A device has Terminals,
  and Terminals meet at a ConnectivityNode. The model stores plain edges, so
  export synthesises those objects and import collapses them back.
* **Extensions.** A few things the model needs have no home in CIM -- which
  device heads a feeder, a phase string, the ``extras`` bag. Those are written
  in a private ``gridql:`` namespace so a round trip loses nothing while a
  standards-only consumer can ignore them entirely.
"""

from __future__ import annotations

import hashlib
import re

from ..model import (
    Breaker,
    Capacitor,
    Device,
    Feeder,
    Fuse,
    GridObject,
    LineSegment,
    Load,
    Recloser,
    Sectionalizer,
    Substation,
    Switch,
    Transformer,
)

CIM_NS = "http://iec.ch/TC57/2013/CIM-schema-cim16#"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
GRIDQL_NS = "urn:gridql:extension#"

NAMESPACES = {"cim": CIM_NS, "rdf": RDF_NS, "gridql": GRIDQL_NS}

#: Every CIM release has its own namespace -- cim16's dated URI, then
#: ``http://iec.ch/TC57/CIM100#`` from CIM17 on. The classes and properties
#: this package reads are the same in all of them, so any is accepted.
_CIM_NAMESPACE = re.compile(
    r"^http://iec\.ch/TC57/(?:\d{4}/)?CIM-schema-cim\d+#$|^http://iec\.ch/TC57/CIM\d+#$"
)

#: The header namespace of IEC 61970-552 files (md:FullModel). Expected, and
#: carries nothing about the network.
MODEL_DESCRIPTION_PREFIX = "http://iec.ch/TC57/61970-552/ModelDescription/"


def is_cim_namespace(namespace: str) -> bool:
    return namespace == CIM_NS or bool(_CIM_NAMESPACE.match(namespace))

#: CIM classes this importer understands, including the specialisations other
#: tools commonly emit for things the model keeps as one class.
CIM_TO_MODEL: dict[str, type[GridObject]] = {
    "Substation": Substation,
    "Feeder": Feeder,
    "Line": Feeder,
    "ConductingEquipment": Device,
    "Equipment": Device,
    "Switch": Switch,
    "LoadBreakSwitch": Switch,
    "Disconnector": Switch,
    "Jumper": Switch,
    "ProtectedSwitch": Recloser,
    "Recloser": Recloser,
    "Breaker": Breaker,
    "Fuse": Fuse,
    "Sectionaliser": Sectionalizer,
    "Sectionalizer": Sectionalizer,
    "PowerTransformer": Transformer,
    "ACLineSegment": LineSegment,
    "Conductor": LineSegment,
    "EnergyConsumer": Load,
    "ConformLoad": Load,
    "NonConformLoad": Load,
    "EnergyConsumerPhase": None,  # handled as a phase annotation, not an object
    "LinearShuntCompensator": Capacitor,
    "ShuntCompensator": Capacitor,
}
CIM_TO_MODEL = {name: cls for name, cls in CIM_TO_MODEL.items() if cls is not None}

#: CIM classes that carry structure rather than equipment. Seeing one is
#: expected, so it is consumed rather than reported as ignored.
STRUCTURAL_CLASSES = frozenset(
    {
        "Terminal",
        "ConnectivityNode",
        "BaseVoltage",
        "PowerTransformerEnd",
        "TransformerEnd",
        "TransformerTank",
        "TransformerTankEnd",
        "TransformerTankInfo",
        "TransformerEndInfo",
        "ACLineSegmentPhase",
        "EnergyConsumerPhase",
        "SwitchPhase",
        "ShuntCompensatorPhase",
        "LinearShuntCompensatorPhase",
        "VoltageLevel",
        "Bay",
        "GeographicalRegion",
        "SubGeographicalRegion",
        "FullModel",
    }
)


def cim_class_of(obj: GridObject) -> str:
    """The CIM class an object exports as."""
    return obj.CIM_CLASS


def model_class_for(cim_class: str) -> type[GridObject] | None:
    return CIM_TO_MODEL.get(cim_class)


# -- identifiers --------------------------------------------------------

_NCNAME_START = re.compile(r"[A-Za-z_]")
_NCNAME_BAD = re.compile(r"[^A-Za-z0-9_.\-]")


def xml_id(mrid: str) -> str:
    """An mRID as a valid ``rdf:ID``.

    Most utility mRIDs already qualify and are used unchanged. One that does
    not is adjusted here, and the true mRID still travels in
    ``cim:IdentifiedObject.mRID``, so import recovers it exactly.

    An adjusted identifier carries a digest of the original, so two mRIDs
    that clean to the same text -- ``SW 1`` and ``SW_1``, or ``123`` and
    ``_123`` -- cannot end up sharing one rdf:ID.
    """
    cleaned = _NCNAME_BAD.sub("_", mrid)
    if not cleaned or not _NCNAME_START.match(cleaned[0]):
        cleaned = f"_{cleaned}"
    if cleaned == mrid:
        return mrid
    digest = hashlib.sha1(mrid.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}_{digest}"


def strip_reference(value: str) -> str:
    """Turn ``#FDR-104`` or ``urn:uuid:abc`` into a bare identifier."""
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    for prefix in ("urn:uuid:", "urn:", "uuid:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def split_tag(qualified: str) -> tuple[str, str]:
    """``{ns}Local`` -> ``(ns, 'Local')``."""
    if qualified.startswith("{"):
        namespace, _, local = qualified[1:].partition("}")
        return namespace, local
    return "", qualified


# -- extension attributes ----------------------------------------------

EXT_PHASES = "phases"
EXT_IS_TIE = "isTie"
EXT_HEAD = "headTerminalEquipment"
EXT_EXTRAS = "extras"
EXT_DEVICE_TYPE = "deviceType"
