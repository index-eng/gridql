# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The conducting equipment of the semantic model.

Every class here is CIM-*mapped*, not CIM-shaped: the field names are the ones
a distribution engineer would say out loud, while ``CIM_CLASS`` records the
standard class each one exports to when CIM export lands.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..units import parse_quantity

CLOSED = "CLOSED"
OPEN = "OPEN"


class _Missing:
    """Sentinel for 'this object has no such attribute'."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

#: Attributes every grid object answers to, regardless of its dataclass fields.
DERIVED_ATTRS = frozenset(
    {"mrid", "id", "name", "type", "cim_class", "energized", "depth", "hops", "protected_by"}
)


@lru_cache(maxsize=None)
def field_names(cls: type) -> frozenset[str]:
    """The queryable dataclass fields of a grid object class."""
    return frozenset(
        f.name
        for f in dataclasses.fields(cls)
        if f.name != "extras" and not f.name.startswith("_")
    )


class GridObject:
    """Shared attribute protocol for anything GridQL can return."""

    TYPE = "object"
    CIM_CLASS = "IdentifiedObject"
    COLUMNS: tuple[str, ...] = ("mrid", "name", "type")

    #: Set by Network.add(). Not a dataclass field, so it stays out of the
    #: queryable attribute surface and out of dataclasses.asdict().
    _network: Any = None

    mrid: str
    name: str
    extras: dict[str, Any]

    def attribute(self, name: str) -> Any:
        """Look up ``name``, returning :data:`MISSING` when it does not apply.

        ``energized`` is intentionally *not* handled here -- it depends on the
        rest of the network, so :meth:`Network.attribute` resolves it.
        """
        key = name.lower()
        if key == "id":
            return self.mrid
        if key == "type":
            return self.TYPE
        if key == "cim_class":
            # Equipment GridQL has no class for keeps the one its source named.
            if self.CIM_CLASS == "ConductingEquipment":
                return self.extras.get("cim_class") or self.CIM_CLASS
            return self.CIM_CLASS
        if key in field_names(type(self)):
            return getattr(self, key)
        if key in self.extras:
            return self.extras[key]
        return MISSING

    def attribute_names(self) -> frozenset[str]:
        return field_names(type(self)) | DERIVED_ATTRS | frozenset(self.extras)

    def to_row(self) -> dict[str, Any]:
        """The object as an ordered mapping, using its class's display columns."""
        row: dict[str, Any] = {}
        for column in self.COLUMNS:
            value = self.attribute(column)
            row[column] = None if value is MISSING else value
        return row

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.mrid}>"


class Observable:
    """Mixin for objects whose fields feed the network's cached topology.

    The topology cache is rebuilt when the model changes. Adding equipment
    and making connections go through Network, which can see them; operating
    a switch is a plain attribute assignment, which it cannot. This makes
    those assignments announce themselves, so a cached answer cannot survive
    the change that invalidates it.

    Only the classes that own such a field mix this in -- defining
    ``__setattr__`` costs a little on every assignment, and most equipment
    has nothing the topology depends on.
    """

    #: Fields whose value the derived topology is computed from.
    TOPOLOGY_FIELDS: frozenset[str] = frozenset()

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name in self.TOPOLOGY_FIELDS:
            network = self._network
            if network is not None:
                network.invalidate()


@dataclass(repr=False)
class Device(GridObject):
    """Base conducting equipment."""

    TYPE = "device"
    CIM_CLASS = "ConductingEquipment"
    COLUMNS = ("mrid", "name", "type", "feeder", "phases", "voltage")
    #: Whether the device bounds a protection zone: it opens by itself to
    #: isolate a fault below it, so a fault there takes out only its zone.
    PROTECTIVE = False

    mrid: str
    name: str = ""
    phases: str = "ABC"
    voltage: float | None = None  # kV
    feeder: str | None = None
    substation: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.mrid
        if self.voltage is not None:
            self.voltage = parse_quantity(self.voltage, "kV")


@dataclass(repr=False)
class Switch(Observable, Device):
    """A load-break switch, and the base for every switching device."""

    TYPE = "switch"
    CIM_CLASS = "Switch"
    # Opening or closing a switch changes what is energised downstream.
    TOPOLOGY_FIELDS = frozenset({"state"})
    COLUMNS = ("mrid", "name", "type", "feeder", "phases", "voltage", "state", "normal_state")

    normal_state: str = CLOSED
    state: str | None = None
    is_tie: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.normal_state = str(self.normal_state).upper()
        # An unstated present state means the device is where it belongs.
        self.state = self.normal_state if self.state is None else str(self.state).upper()

    @property
    def is_open(self) -> bool:
        return self.state == OPEN


@dataclass(repr=False)
class Recloser(Switch):
    TYPE = "recloser"
    CIM_CLASS = "ProtectedSwitch"
    PROTECTIVE = True


@dataclass(repr=False)
class Breaker(Switch):
    TYPE = "breaker"
    CIM_CLASS = "Breaker"
    PROTECTIVE = True


@dataclass(repr=False)
class Fuse(Switch):
    TYPE = "fuse"
    CIM_CLASS = "Fuse"
    PROTECTIVE = True


@dataclass(repr=False)
class Sectionalizer(Switch):
    TYPE = "sectionalizer"
    CIM_CLASS = "Sectionaliser"
    # It clears nothing itself, but it counts the recloser behind it and
    # opens in the dead time, so a permanent fault below it takes out only
    # what is below it. That outage boundary is what a zone is for.
    PROTECTIVE = True


@dataclass(repr=False)
class Transformer(Device):
    TYPE = "transformer"
    CIM_CLASS = "PowerTransformer"
    COLUMNS = (
        "mrid", "name", "type", "feeder", "phases",
        "kva", "primary_voltage", "secondary_voltage",
    )

    kva: float | None = None
    primary_voltage: float | None = None  # kV
    secondary_voltage: float | None = None  # kV

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.kva is not None:
            self.kva = parse_quantity(self.kva, "kVA")
        if self.primary_voltage is not None:
            self.primary_voltage = parse_quantity(self.primary_voltage, "kV")
        if self.secondary_voltage is not None:
            self.secondary_voltage = parse_quantity(self.secondary_voltage, "kV")
        if self.voltage is None:
            self.voltage = self.primary_voltage


@dataclass(repr=False)
class LineSegment(Device):
    TYPE = "line"
    CIM_CLASS = "ACLineSegment"
    COLUMNS = ("mrid", "name", "type", "feeder", "phases", "voltage", "length", "conductor")

    length: float | None = None  # ft
    conductor: str | None = None
    ampacity: float | None = None  # A

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.length is not None:
            self.length = parse_quantity(self.length, "ft")
        if self.ampacity is not None:
            self.ampacity = parse_quantity(self.ampacity, "A")


@dataclass(repr=False)
class Load(Device):
    TYPE = "load"
    CIM_CLASS = "EnergyConsumer"
    COLUMNS = ("mrid", "name", "type", "feeder", "phases", "voltage", "kw", "kvar")

    kw: float | None = None
    kvar: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.kw is not None:
            self.kw = parse_quantity(self.kw, "kW")
        if self.kvar is not None:
            self.kvar = parse_quantity(self.kvar, "kVAr")


@dataclass(repr=False)
class Capacitor(Device):
    TYPE = "capacitor"
    CIM_CLASS = "LinearShuntCompensator"
    COLUMNS = ("mrid", "name", "type", "feeder", "phases", "voltage", "kvar", "state")

    kvar: float | None = None
    normal_state: str = CLOSED
    state: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.kvar is not None:
            self.kvar = parse_quantity(self.kvar, "kVAr")
        self.normal_state = str(self.normal_state).upper()
        self.state = self.normal_state if self.state is None else str(self.state).upper()
