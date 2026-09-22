"""Containers: substations and feeders.

The feeder doubles as the builder API from the design doc::

    feeder = network.add_feeder("FDR-104", voltage="13.8kV")
    feeder.add_recloser("REC-104-01")
    feeder.add_switch("SW-104-17", normal_state="OPEN")
    feeder.add_transformer("XFMR-104-22", kva=500)

Each ``add_*`` call connects the new device in series behind the previous one,
which is what makes that flat sequence describe a real circuit. Pass
``after="SW-104-17"`` to branch off an earlier device instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..units import parse_quantity
from .device import (
    Breaker,
    Capacitor,
    Device,
    Fuse,
    GridObject,
    LineSegment,
    Load,
    Recloser,
    Sectionalizer,
    Switch,
    Transformer,
)

#: Marker meaning "connect behind whatever was added last" (the default).
_SERIES = object()


@dataclass(repr=False)
class Substation(GridObject):
    TYPE = "substation"
    CIM_CLASS = "Substation"
    COLUMNS = ("mrid", "name", "type", "voltage")

    mrid: str
    name: str = ""
    voltage: float | None = None  # kV
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.mrid
        if self.voltage is not None:
            self.voltage = parse_quantity(self.voltage, "kV")


@dataclass(repr=False)
class Feeder(GridObject):
    TYPE = "feeder"
    CIM_CLASS = "Feeder"
    COLUMNS = ("mrid", "name", "type", "substation", "voltage")

    mrid: str
    name: str = ""
    voltage: float | None = None  # kV
    substation: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.mrid
        if self.voltage is not None:
            self.voltage = parse_quantity(self.voltage, "kV")
        # Runtime wiring, deliberately not dataclass fields so it stays out of
        # the queryable attribute surface.
        self.network: Any = None
        self.head: str | None = None
        self.last: str | None = None

    # -- builder API ----------------------------------------------------

    def add(self, device: Device, after: Any = _SERIES) -> Device:
        """Register ``device`` on this feeder and wire it into the circuit."""
        if self.network is None:
            raise RuntimeError(
                f"feeder {self.mrid} is not attached to a Network; "
                "create it with Network.add_feeder()"
            )

        device.feeder = self.mrid
        if device.substation is None:
            device.substation = self.substation
        if device.voltage is None:
            device.voltage = self.voltage

        self.network.add(device)

        if self.head is None:
            self.head = device.mrid
        else:
            if after is _SERIES and self.last is None:
                raise RuntimeError(
                    f"feeder {self.mrid} was loaded from storage, so there is no "
                    f"'previous device' to chain {device.mrid} onto; pass after=..."
                )
            target = self.last if after is _SERIES else after
            if target is not None:
                target_mrid = target.mrid if isinstance(target, GridObject) else str(target)
                self.network.connect(target_mrid, device.mrid)

        self.last = device.mrid
        return device

    def _add(self, cls: type[Device], mrid: str, after: Any, kwargs: dict) -> Device:
        return self.add(cls(mrid=mrid, **kwargs), after=after)

    def add_device(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Device:
        return self._add(Device, mrid, after, kwargs)

    def add_switch(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Switch:
        return self._add(Switch, mrid, after, kwargs)  # type: ignore[return-value]

    def add_recloser(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Recloser:
        return self._add(Recloser, mrid, after, kwargs)  # type: ignore[return-value]

    def add_breaker(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Breaker:
        return self._add(Breaker, mrid, after, kwargs)  # type: ignore[return-value]

    def add_fuse(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Fuse:
        return self._add(Fuse, mrid, after, kwargs)  # type: ignore[return-value]

    def add_sectionalizer(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Sectionalizer:
        return self._add(Sectionalizer, mrid, after, kwargs)  # type: ignore[return-value]

    def add_transformer(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Transformer:
        return self._add(Transformer, mrid, after, kwargs)  # type: ignore[return-value]

    def add_line(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> LineSegment:
        return self._add(LineSegment, mrid, after, kwargs)  # type: ignore[return-value]

    def add_load(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Load:
        return self._add(Load, mrid, after, kwargs)  # type: ignore[return-value]

    def add_capacitor(self, mrid: str, *, after: Any = _SERIES, **kwargs) -> Capacitor:
        return self._add(Capacitor, mrid, after, kwargs)  # type: ignore[return-value]
