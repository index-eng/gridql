"""The type vocabulary GridQL exposes, and its mapping onto CIM.

``FIND switches`` matches every switching device -- reclosers, breakers, fuses
and ties included -- because that is both what CIM says (they all specialise
``Switch``) and what an engineer means when they ask for the switches on a
feeder. ``FIND devices`` matches all conducting equipment, but not the feeder
and substation containers.
"""

from __future__ import annotations

import difflib

from ..errors import GridQLNameError
from .device import (
    DERIVED_ATTRS,
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
    field_names,
)
from .feeder import Feeder, Substation

#: Canonical type key -> the class it selects (subclasses included).
TYPE_CLASSES: dict[str, type[GridObject]] = {
    "device": Device,
    "switch": Switch,
    "recloser": Recloser,
    "breaker": Breaker,
    "fuse": Fuse,
    "sectionalizer": Sectionalizer,
    "transformer": Transformer,
    "line": LineSegment,
    "load": Load,
    "capacitor": Capacitor,
    "feeder": Feeder,
    "substation": Substation,
}

#: What users may type -> canonical type key. Singular and plural both work.
TYPE_ALIASES: dict[str, str] = {
    "device": "device", "devices": "device",
    "equipment": "device", "everything": "device",
    "switch": "switch", "switches": "switch",
    "recloser": "recloser", "reclosers": "recloser",
    "breaker": "breaker", "breakers": "breaker",
    "fuse": "fuse", "fuses": "fuse",
    "sectionalizer": "sectionalizer", "sectionalizers": "sectionalizer",
    "sectionaliser": "sectionalizer", "sectionalisers": "sectionalizer",
    "transformer": "transformer", "transformers": "transformer",
    "xfmr": "transformer", "xfmrs": "transformer",
    "line": "line", "lines": "line",
    "conductor": "line", "conductors": "line",
    "segment": "line", "segments": "line",
    "load": "load", "loads": "load",
    "customer": "load", "customers": "load",
    "capacitor": "capacitor", "capacitors": "capacitor",
    "cap": "capacitor", "caps": "capacitor",
    "feeder": "feeder", "feeders": "feeder",
    "circuit": "feeder", "circuits": "feeder",
    "substation": "substation", "substations": "substation",
    "sub": "substation", "subs": "substation",
}

#: Attribute -> the unit its stored value is expressed in.
CANONICAL_UNITS: dict[str, str] = {
    "voltage": "kV",
    "primary_voltage": "kV",
    "secondary_voltage": "kV",
    "kva": "kVA",
    "kw": "kW",
    "kvar": "kVAr",
    "ampacity": "A",
    "length": "ft",
}


def resolve_type(name: str) -> str:
    """Map a word from a query onto a canonical type key."""
    key = TYPE_ALIASES.get(name.lower())
    if key is None:
        raise GridQLNameError(
            f"unknown type '{name}'",
            tuple(difflib.get_close_matches(name.lower(), TYPE_ALIASES, n=3, cutoff=0.5)),
        )
    return key


def class_for(type_key: str) -> type[GridObject]:
    return TYPE_CLASSES[type_key]


def cim_class_for(type_key: str) -> str:
    return TYPE_CLASSES[type_key].CIM_CLASS


def attribute_universe(type_key: str) -> frozenset[str]:
    """Every attribute name a query on this type could legitimately mention.

    Includes subclass attributes, so ``FIND devices WHERE kva >= 500`` works,
    and so a bare word on the right of a comparison can be told apart from a
    value (``state != normal_state`` versus ``state = OPEN``).
    """
    cls = TYPE_CLASSES[type_key]
    names = set(DERIVED_ATTRS) | set(field_names(cls))
    for candidate in TYPE_CLASSES.values():
        if issubclass(candidate, cls):
            names |= field_names(candidate)
    return frozenset(names)


def canonical_unit(attribute: str) -> str | None:
    return CANONICAL_UNITS.get(attribute.lower())
