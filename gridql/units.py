"""Unit parsing and conversion.

Engineers write ``13.8kV`` and ``500 kVA``; the model stores plain floats in one
canonical unit per attribute. Everything needed to move between the two lives
here, with no dependency on the rest of the package.

Dimensions are kept deliberately narrow -- apparent power (VA) and real power
(W) are *different* dimensions, so comparing a transformer's ``kva`` against a
value written in kW is an error rather than a silent wrong answer.
"""

from __future__ import annotations

import re

from .errors import UnitError

# unit (lowercased) -> (dimension, factor to that dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {
    "v": ("voltage", 1.0),
    "kv": ("voltage", 1e3),
    "mv": ("voltage", 1e6),
    "va": ("apparent_power", 1.0),
    "kva": ("apparent_power", 1e3),
    "mva": ("apparent_power", 1e6),
    "w": ("real_power", 1.0),
    "kw": ("real_power", 1e3),
    "mw": ("real_power", 1e6),
    "var": ("reactive_power", 1.0),
    "kvar": ("reactive_power", 1e3),
    "mvar": ("reactive_power", 1e6),
    "a": ("current", 1.0),
    "amp": ("current", 1.0),
    "amps": ("current", 1.0),
    "ka": ("current", 1e3),
    "ft": ("length", 1.0),
    "feet": ("length", 1.0),
    "mi": ("length", 5280.0),
    "mile": ("length", 5280.0),
    "miles": ("length", 5280.0),
    # The international foot is exactly 0.3048 m, so derive metric lengths
    # from that instead of a rounded decimal.
    "m": ("length", 1.0 / 0.3048),
    "km": ("length", 1000.0 / 0.3048),
}

_QUANTITY_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)?\s*$")


def lookup(unit: str) -> tuple[str, float]:
    """Return ``(dimension, factor)`` for a unit, raising on unknown units."""
    try:
        return _UNITS[unit.lower()]
    except KeyError:
        raise UnitError(f"unknown unit '{unit}'") from None


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value`` between two units of the same dimension."""
    from_dimension, from_factor = lookup(from_unit)
    to_dimension, to_factor = lookup(to_unit)
    if from_dimension != to_dimension:
        raise UnitError(
            f"cannot compare {from_dimension.replace('_', ' ')} ('{from_unit}') "
            f"with {to_dimension.replace('_', ' ')} ('{to_unit}')"
        )
    return value * from_factor / to_factor


def parse_quantity(text: str | int | float, canonical: str | None = None) -> float:
    """Parse ``"13.8kV"`` (or a bare number) into a float in ``canonical`` units.

    A value with no unit suffix is assumed to already be in the canonical unit,
    which is what makes ``kva=500`` and ``kva="0.5MVA"`` both mean 500 kVA.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)

    match = _QUANTITY_RE.match(str(text))
    if match is None:
        raise UnitError(f"could not read '{text}' as a quantity")

    value = float(match.group(1))
    unit = match.group(2)
    if unit is None or canonical is None:
        return value
    return convert(value, unit, canonical)
