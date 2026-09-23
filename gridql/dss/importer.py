# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn an OpenDSS model into the semantic model.

OpenDSS describes a circuit for simulation, so several things GridQL treats
as facts are left for the reader to work out:

* **Equipment kinds.** A switch is a Line with ``switch=yes``. Whether it is a
  breaker, fuse or recloser is said by the control attached to it -- a Relay,
  Fuse or Recloser -- not by the switch itself.
* **Banks.** A three-phase regulator is three single-phase Transformers sharing
  a ``bank``. They are one piece of equipment, and become one transformer, as
  they do in CIM.
* **Voltage.** A bus has no voltage until a power flow gives it one. Each bus's
  nominal voltage is carried out from the source and every transformer
  winding along the lines, and settled on the nearest of the script's
  ``voltagebases``, as OpenDSS's own CalcVoltageBases would.
* **Split-phase service.** A 120/240 V secondary is written on nodes 1 and 2,
  indistinguishable from phases A and B. Buses fed from a center-tapped
  transformer's secondary are found by walking out from it, and equipment on
  them is ``s1s2``, as CIM names it.
* **Containers.** A script is one circuit, which becomes one feeder, headed
  by its source. A transformer marked ``sub=yes`` names the substation.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cim.importer import ImportReport
from ..model import (
    Breaker,
    Capacitor,
    Device,
    Fuse,
    LineSegment,
    Load,
    Network,
    Recloser,
    Switch,
    Transformer,
)
from .parser import DssObject, DssScript, array, loads_script, number, read_script

#: Classes that describe or control equipment rather than being it. Reading
#: one is expected, so it is never reported as ignored.
_DESCRIPTIVE = frozenset({
    "linecode", "linegeometry", "linespacing", "wiredata", "cndata", "tsdata", "xfmrcode",
    "loadshape", "tshape", "priceshape", "growthshape", "spectrum", "xycurve", "tcc_curve",
    "regcontrol", "capcontrol", "swtcontrol", "fuse", "recloser", "relay", "invcontrol",
    "expcontrol", "storagecontroller", "gendispatcher", "upfccontrol", "esccontrol",
    "energymeter", "monitor", "sensor", "fmonitor",
})

#: Equipment GridQL has no class for, kept as plain devices. The CIM class is
#: the one a CIM tool writes for it, so export says the same thing twice over.
_GENERIC = {
    "vsource": "EnergySource",
    "isource": "EnergySource",
    "generator": "SynchronousMachine",
    "windgen": "PowerElectronicsConnection",
    "pvsystem": "PowerElectronicsConnection",
    "storage": "PowerElectronicsConnection",
    "reactor": None,  # series or shunt, decided per object
    "upfc": None,
    "gicsource": None,
    "vccs": None,
    "fault": None,
}

_PROTECTION = {"fuse": Fuse, "recloser": Recloser, "relay": Breaker}

#: Length units OpenDSS knows, in feet.
_FEET = {"ft": 1.0, "kft": 1000.0, "mi": 5280.0, "m": 1 / 0.3048, "km": 1000 / 0.3048,
         "in": 1 / 12, "cm": 1 / 30.48, "mm": 1 / 304.8}

_PHASE_LETTERS = {"1": "A", "2": "B", "3": "C"}

_YES = {"y", "yes", "true", "t", "1"}


@dataclass
class DssDocument:
    network: Network
    report: ImportReport


def read_dss(source: str | Path) -> DssDocument:
    """Read an OpenDSS model -- its master file and whatever that redirects to."""
    script = read_script(source)
    document = build(script)
    document.network.source = str(source)
    return document


def loads_dss(text: str) -> DssDocument:
    """Read an OpenDSS script held in a string. Redirects resolve from the working directory."""
    return build(loads_script(text))


def import_network(source: str | Path) -> Network:
    return read_dss(source).network


# -- building -------------------------------------------------------------


def build(script: DssScript) -> DssDocument:
    return _Builder(script).build()


class _Builder:
    def __init__(self, script: DssScript) -> None:
        self.script = script
        self.network = Network()
        self.report = ImportReport()
        #: Device mRID -> its terminals' buses, in terminal order, as written.
        self.terminals: dict[str, list[str]] = {}
        #: Device mRID -> the objects it was built from (a bank has several).
        self.parts: dict[str, list[DssObject]] = {}
        #: (class, lowercase name) -> the mRID of the device built from it.
        self.mrid_of: dict[tuple[str, str], str] = {}
        self.feeder: str | None = None

    def build(self) -> DssDocument:
        self.containers()
        protection = self.protection()
        banks = self.banks()
        disabled = 0

        for key in self.script.order:
            obj = self.script.objects.get(key)
            if obj is None:
                continue
            if not _enabled(obj) and not _open_when_disabled(obj):
                disabled += 1
                continue
            if obj.cls in _DESCRIPTIVE:
                continue
            if obj.cls == "line":
                self.line(obj, protection)
            elif obj.cls == "transformer":
                bank = banks.get(obj.key)
                if bank is None or bank[0] is obj:
                    self.transformer(bank or [obj])
            elif obj.cls == "load":
                self.load(obj)
            elif obj.cls == "capacitor":
                self.capacitor(obj)
            elif obj.cls in _GENERIC:
                self.generic(obj)
            else:
                self.report.ignored[obj.cls] = self.report.ignored.get(obj.cls, 0) + 1

        self.connect()
        self.phases()
        self.voltages()
        self.head()

        if disabled:
            self.report.notes.append(
                f"{disabled} disabled element{'s' if disabled != 1 else ''} not imported "
                "(a disabled switch or capacitor is imported, open)"
            )
        self.report.notes.extend(self.script.notes)
        if self.script.skipped:
            described = ", ".join(
                f"{verb} x{count}" for verb, count in sorted(self.script.skipped.items())
            )
            self.report.notes.append(f"commands read but not acted on: {described}")
        return DssDocument(self.network, self.report)

    # -- containers -------------------------------------------------------

    def containers(self) -> None:
        # Equipment keeps its own name as its mRID: it is what a topology
        # query names. A container whose name some equipment also has --
        # a substation named for its transformer -- is the one qualified.
        names = {key[1] for key in self.script.order}
        substation = None
        for transformer in self.script.of_class("transformer"):
            if _truthy(transformer.get("sub")):
                name = transformer.get("subname") or transformer.name
                substation = f"substation.{name}" if name.lower() in names else name
                self.network.add_substation(substation, name=name)
                self.report.substations += 1
                break
        name = self.script.circuit or "circuit"
        self.feeder = f"feeder.{name}" if name.lower() in names else name
        self.network.add_feeder(self.feeder, name=name, substation=substation)
        self.report.feeders += 1

    def head(self) -> None:
        source = self.script.find("vsource", "source")
        feeder = self.network.objects[self.feeder]
        mrid = self.mrid_of.get(("vsource", "source")) if source else None
        if mrid is not None:
            feeder.head = mrid
        else:
            self.report.notes.extend(self.network.infer_heads())

    # -- equipment --------------------------------------------------------

    def add(self, cls: type, objects: list[DssObject], name: str, buses: list[str],
            **fields: Any) -> Device:
        mrid = name
        taken = {m.casefold() for m in self.network.objects}
        if mrid.casefold() in taken:
            mrid = f"{objects[0].cls}.{name}"
            self.report.notes.append(
                f"{objects[0].cls}.{name}: the name is taken by other equipment, "
                f"so its mRID is '{mrid}'"
            )
        device = cls(mrid=mrid, name=name, feeder=self.feeder,
                     substation=self.network.objects[self.feeder].substation, **fields)
        self.network.add(device)
        for obj in objects:
            self.mrid_of[(obj.cls, obj.key)] = mrid
        self.terminals[mrid] = buses
        self.parts[mrid] = objects
        self.report.devices += 1
        return device

    def line(self, obj: DssObject, protection: dict[tuple[str, str], type]) -> None:
        buses = [obj.get("bus1", ""), obj.get("bus2", "")]
        if _truthy(obj.get("switch")):
            cls = protection.pop(("line", obj.key), Switch)
            normal = self.script.positions.get(("line", obj.key))
            if not _enabled(obj):
                normal = "OPEN"
            control = self.switch_control(obj)
            state = None
            if control is not None:
                normal = control[0] or normal
                state = control[1]
            normal = normal or "CLOSED"
            self.add(cls, [obj], obj.name, buses, normal_state=normal, state=state or normal)
            return

        code = self.script.find("linecode", obj.get("linecode", "")) if obj.get("linecode") else None
        units = (obj.get("units") or (code.get("units") if code else None) or "none").lower()
        length = number(obj.get("length", "1"))
        feet = None if length is None or units not in _FEET else round(length * _FEET[units], 4)
        amps = number(obj.get("normamps")) or (number(code.get("normamps")) if code else None)
        conductor = obj.get("linecode") or obj.get("geometry") or obj.get("wires")
        self.add(LineSegment, [obj], obj.name, buses,
                 length=feet, conductor=conductor, ampacity=amps)

    def switch_control(self, obj: DssObject) -> tuple[str | None, str | None] | None:
        for control in self.script.of_class("swtcontrol"):
            if _reference(control.get("switchedobj")) == ("line", obj.key):
                normal = _position(control.get("normal"))
                return normal, _position(control.get("state")) or normal
        return None

    def transformer(self, tanks: list[DssObject]) -> None:
        first = tanks[0]
        name = first.get("bank") if len(tanks) > 1 or first.get("bank") else first.name
        windings = [_windings(tank) for tank in tanks]
        buses = [w.get("bus", "") for tank in windings for w in tank]
        rated = [number(tank[0].get("kva")) for tank in windings if tank]
        kva = sum(r for r in rated if r is not None) if any(r is not None for r in rated) else None
        primary = number(windings[0][0].get("kv")) if windings[0] else None
        secondary = number(windings[0][1].get("kv")) if len(windings[0]) > 1 else None
        self.add(Transformer, tanks, name, buses, kva=kva, primary_voltage=primary,
                 secondary_voltage=secondary)

    def load(self, obj: DssObject) -> None:
        kw = number(obj.get("kw"))
        kva = number(obj.get("kva"))
        pf = number(obj.get("pf"))
        if kw is None and kva is not None:
            kw = kva * abs(pf if pf is not None else 0.88)
        if kw is None:
            kw = 10.0  # OpenDSS's default
        kvar = number(obj.get("kvar"))
        if kvar is None:
            pf = 0.88 if pf is None else pf
            kvar = 0.0 if abs(pf) >= 1 else math.copysign(kw * math.tan(math.acos(abs(pf))), pf)
        self.add(Load, [obj], obj.name, [obj.get("bus1", "")],
                 kw=round(kw, 3), kvar=round(kvar, 3))

    def capacitor(self, obj: DssObject) -> None:
        steps = [number(v) for v in array(obj.get("kvar", "1200"))]
        kvar = sum(v for v in steps if v is not None)
        states = [number(v) for v in array(obj.get("states", ""))]
        closed = _enabled(obj) and (not states or any(s for s in states))
        state = "CLOSED" if closed else "OPEN"
        buses = [obj.get("bus1", "")]
        if obj.get("bus2") and not _grounded(obj.get("bus2")):
            buses.append(obj.get("bus2"))
        self.add(Capacitor, [obj], obj.name, buses, kvar=kvar, normal_state=state, state=state)

    def generic(self, obj: DssObject) -> None:
        buses = [obj.get("bus1") or ("sourcebus" if obj.cls == "vsource" else "")]
        series = obj.get("bus2") and not _grounded(obj.get("bus2"))
        if series:
            buses.append(obj.get("bus2"))
        cim_class = _GENERIC[obj.cls]
        if obj.cls == "reactor":
            cim_class = "SeriesCompensator" if series else None
        extras: dict[str, Any] = {"dss_class": obj.cls}
        if cim_class:
            extras["cim_class"] = cim_class
        name = obj.name
        self.add(Device, [obj], name, buses, extras=extras)

    # -- what the controls say --------------------------------------------

    def protection(self) -> dict[tuple[str, str], type]:
        """The switches a Fuse, Recloser or Relay makes a fuse, recloser or breaker."""
        kinds: dict[tuple[str, str], type] = {}
        for cls, kind in _PROTECTION.items():
            for control in self.script.of_class(cls):
                target = _reference(control.get("switchedobj") or control.get("monitoredobj"))
                if target is None:
                    continue
                line = self.script.find(*target)
                if line is None or line.cls != "line" or not _truthy(line.get("switch")):
                    self.report.notes.append(
                        f"{cls}.{control.name} protects {target[0]}.{target[1]}, which is not "
                        "a switch, so it is not modelled as its own device"
                    )
                    continue
                kinds[target] = kind
        return kinds

    def banks(self) -> dict[str, list[DssObject]]:
        """Transformers grouped by the bank they belong to, keyed by each tank's name."""
        grouped: dict[str, list[DssObject]] = {}
        for transformer in self.script.of_class("transformer"):
            bank = transformer.get("bank")
            if bank and _enabled(transformer):
                grouped.setdefault(bank.lower(), []).append(transformer)
        return {tank.key: tanks for tanks in grouped.values() for tank in tanks}

    # -- topology ---------------------------------------------------------

    def connect(self) -> None:
        for mrid, buses in self.terminals.items():
            for bus in buses:
                node = _bus(bus)
                if node and not _grounded(bus):
                    self.network.attach(mrid, node)
        self.report.connections = (
            sum(len(self.network.neighbors(m)) for m in self.network.objects) // 2
        )

    def phases(self) -> None:
        secondary = self.split_phase_buses()
        for mrid, buses in self.terminals.items():
            device = self.network.objects[mrid]
            parts = self.parts[mrid]
            if not isinstance(device, Transformer) and buses and all(
                _bus(bus) in secondary for bus in buses if bus
            ):
                device.phases = "s1s2"
                continue
            if isinstance(device, Transformer):
                primary = [_windings(tank)[0].get("bus", "") for tank in parts if _windings(tank)]
                letters = set().union(*(_letters(bus, _phase_count(tank))
                                        for bus, tank in zip(primary, parts)))
            else:
                letters = _letters(buses[0] if buses else "", _phase_count(parts[0]))
            device.phases = "".join(p for p in "ABC" if p in letters) or device.phases

    def split_phase_buses(self) -> set[str]:
        """Buses on the secondary side of a center-tapped service transformer."""
        found: set[str] = set()
        for transformer in self.script.of_class("transformer"):
            windings = _windings(transformer)
            if len(windings) >= 3 and _phase_count(transformer) == 1:
                second, third = _bus(windings[1].get("bus", "")), _bus(windings[2].get("bus", ""))
                if second and second == third:
                    found.add(second)
        return self.spread(found, through=lambda device: not isinstance(device, Transformer))

    def spread(self, buses: set[str], through) -> set[str]:
        """Every bus reachable from ``buses`` across devices ``through`` accepts."""
        by_bus: dict[str, list[str]] = {}
        for mrid, terminals in self.terminals.items():
            for bus in terminals:
                by_bus.setdefault(_bus(bus), []).append(mrid)
        seen = set(buses)
        queue = deque(buses)
        while queue:
            bus = queue.popleft()
            for mrid in by_bus.get(bus, []):
                if not through(self.network.objects[mrid]):
                    continue
                for other in self.terminals[mrid]:
                    other = _bus(other)
                    if other and other not in seen:
                        seen.add(other)
                        queue.append(other)
        return seen

    def voltages(self) -> None:
        """Each bus's nominal line-to-line voltage, and each device's from its first bus."""
        known: dict[str, float] = {}
        source = self.script.find("vsource", "source")
        if source is not None:
            known[_bus(source.get("bus1") or "sourcebus")] = number(source.get("basekv")) or 115.0
        for transformer in self.script.of_class("transformer"):
            if not _enabled(transformer):
                continue
            for winding in _windings(transformer):
                kv = number(winding.get("kv"))
                bus = winding.get("bus", "")
                if kv is None or not bus:
                    continue
                if _phase_count(transformer) == 1 and len(_nodes(bus) - {"0"}) <= 1:
                    kv *= math.sqrt(3)  # a single-phase winding to neutral
                known.setdefault(_bus(bus), kv)

        by_bus: dict[str, list[str]] = {}
        for mrid, terminals in self.terminals.items():
            for bus in terminals:
                by_bus.setdefault(_bus(bus), []).append(mrid)
        queue = deque(known)
        while queue:
            bus = queue.popleft()
            for mrid in by_bus.get(bus, []):
                if isinstance(self.network.objects[mrid], Transformer):
                    continue
                for other in self.terminals[mrid]:
                    other = _bus(other)
                    if other and other not in known:
                        known[other] = known[bus]
                        queue.append(other)

        bases = self.script.voltage_bases
        for mrid, terminals in self.terminals.items():
            if not terminals:
                continue
            kv = known.get(_bus(terminals[0]))
            if kv is not None:
                self.network.objects[mrid].voltage = _settle(kv, bases)


# -- small readers ----------------------------------------------------------


def _windings(transformer: DssObject) -> list[dict[str, str]]:
    count = int(number(transformer.get("windings")) or 2)
    return [transformer.windings.get(number, {}) for number in range(1, count + 1)]


def _phase_count(obj: DssObject) -> int:
    return int(number(obj.get("phases")) or 3)


def _bus(spec: str) -> str:
    """``650.1.2.3`` -> ``650``: the bus a terminal is on, as a node identifier."""
    return spec.split(".", 1)[0].strip().lower()


def _nodes(spec: str) -> set[str]:
    return set(spec.split(".")[1:])


def _grounded(spec: str) -> bool:
    nodes = spec.split(".")[1:]
    return bool(nodes) and all(node == "0" for node in nodes)


def _letters(spec: str, phases: int) -> set[str]:
    written = [_PHASE_LETTERS[n] for n in spec.split(".")[1:] if n in _PHASE_LETTERS]
    if written:
        return set(written)
    if "." in spec:  # nodes given, but none of them a phase
        return set()
    return set("ABC"[: max(1, min(phases, 3))])


def _settle(kv: float, bases: list[float]) -> float:
    """The voltage base nearest ``kv``, if one is close; otherwise ``kv`` itself."""
    if bases:
        nearest = min(bases, key=lambda base: abs(base - kv) / base)
        if abs(nearest - kv) / nearest < 0.1:
            return nearest
    return round(kv, 3)


def _reference(value: str | None) -> tuple[str, str] | None:
    if not value or "." not in value:
        return None
    cls, _, name = value.partition(".")
    return cls.lower(), name.lower()


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _YES


def _enabled(obj: DssObject) -> bool:
    value = obj.get("enabled")
    return value is None or value.strip().lower() in _YES


def _open_when_disabled(obj: DssObject) -> bool:
    """A disabled switch or capacitor is still there, just open; anything else is gone."""
    return obj.cls == "capacitor" or (obj.cls == "line" and _truthy(obj.get("switch")))


def _position(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("o", "open"):
        return "OPEN"
    if lowered in ("c", "closed", "close"):
        return "CLOSED"
    return None
