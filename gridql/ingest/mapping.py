# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mapping files: how one utility's tables become the GridQL model.

Every utility names things its own way -- ``FEEDER_ID`` at one,
``CIRCUIT_NO`` at the next. The alias table in :mod:`.csv_files` guesses
at the common spellings; a mapping says exactly what each column means, so
nothing is guessed and the queries stay the same whichever utility's data
is underneath::

    [feeders]
    file    = "CIRCUIT.csv"
    mrid    = "FDR-{CIRCUIT_NO}"          # a template: text around columns
    name    = "CIRCUIT_DESC"              # a column
    voltage = { column = "NOM_VOLTS", unit = "V" }

    [[devices]]
    file   = "SWITCH.csv"
    mrid   = "FACILITY_ID"
    feeder = "FDR-{CIRCUIT_NO}"
    type   = { column = "SW_TYPE", values = { RCL = "recloser", BKR = "breaker" } }
    state  = { column = "POSITION", values = { O = "OPEN", C = "CLOSED" } }

    [[devices]]
    file = "TRANSFORMER.csv"
    type = { value = "transformer" }      # a constant: the whole file is one type

A field is written one of three ways. A plain string is a column; a string
with ``{COLUMN}`` in it is a template; a table names a ``column``,
``template`` or ``value`` and may add a ``unit`` the cells are written in, a
``values`` table translating the source's codes, and a ``default`` for
empty cells.

Columns the mapping does not name are kept as attributes, as the alias
reader keeps them, unless the section says ``extras = false`` or lists the
ones to keep. Nothing here reads files: a section is bound to a header and
then translates one record at a time, so a database source can use the
same mapping as a CSV one. A section read from Postgres names a ``table``
in place of a ``file``, or gives a ``query`` to run::

    [[devices]]
    table = "gis.switch"
    mrid  = "facility_id"

    [[devices]]
    query = "SELECT * FROM gis.transformer WHERE status = 'IN SERVICE'"
    mrid  = "facility_id"
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping as MappingType

from ..errors import GridQLError
from ..model.types import attribute_universe, canonical_unit
from ..units import is_unit, lookup, split_quantity


class MappingError(GridQLError):
    """A mapping file is malformed, or does not fit the data it is used on."""


#: What each section may map, by the model's own field names.
TARGETS: dict[str, tuple[str, ...]] = {
    "substations": ("mrid", "name", "voltage"),
    "feeders": ("mrid", "name", "voltage", "substation", "head"),
    "devices": (
        "mrid", "name", "type", "feeder", "substation", "phases", "voltage",
        "state", "normal_state", "is_tie",
        "kva", "primary_voltage", "secondary_voltage",
        "kw", "kvar", "length", "conductor", "ampacity",
        "from_node", "to_node",
    ),
    "connections": ("from_device", "to_device"),
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    "substations": ("mrid",),
    "feeders": ("mrid",),
    "devices": ("mrid",),
    "connections": ("from_device", "to_device"),
}

#: The model type whose attributes an unmapped column must not shadow.
_TYPE_OF_KIND = {"substations": "substation", "feeders": "feeder", "devices": "device"}

#: Where a section's records come from: exactly one of these is set.
SOURCES = ("file", "table", "query")

#: Section keys that configure the section rather than map a field.
_SETTINGS = (*SOURCES, "extras")

_SPEC_KEYS = ("column", "template", "value", "unit", "values", "default")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


# -- the parts of a mapping -------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """How one model field is read from a source record."""

    target: str
    column: str | None = None
    template: str | None = None
    value: str | None = None
    unit: str | None = None
    values: MappingType[str, str] = field(default_factory=dict)
    default: str | None = None

    @property
    def columns(self) -> tuple[str, ...]:
        """The source columns this field reads."""
        if self.column is not None:
            return (self.column,)
        if self.template is not None:
            return tuple(name.strip() for name in _PLACEHOLDER.findall(self.template))
        return ()

    @property
    def source(self) -> str:
        """How the field is written, for messages."""
        if self.column is not None:
            return self.column
        if self.template is not None:
            return self.template
        return repr(self.value)


@dataclass(frozen=True)
class Section:
    """One source's worth of mapping: where it is, and what its columns mean."""

    kind: str
    #: The file name, the table name, or the SQL, as ``form`` says.
    source: str
    fields: MappingType[str, FieldSpec]
    #: True keeps every unmapped column, False none, a tuple just those.
    extras: bool | tuple[str, ...] = True
    #: The mapping file this came from, for messages.
    origin: str = "mapping"
    #: What ``source`` is: one of :data:`SOURCES`.
    form: str = "file"

    @property
    def label(self) -> str:
        """The source as messages name it: a query by its opening words."""
        if self.form != "query":
            return self.source
        words = " ".join(self.source.split())
        if len(words) <= 43:
            return f"query ({words})"
        return f"query ({words[:41].rsplit(' ', 1)[0]}...)"

    def bind(self, header: list[str]) -> "BoundSection":
        """Match the section against a file's header, or say what is missing."""
        by_name: dict[str, str] = {}
        for name in header:
            by_name.setdefault(name.strip().casefold(), name)

        def find(column: str, purpose: str) -> str:
            found = by_name.get(column.strip().casefold())
            if found is not None:
                return found
            close = difflib.get_close_matches(column, header, n=1, cutoff=0.6)
            hint = f" (did you mean {close[0]}?)" if close else ""
            raise MappingError(
                f"{self.origin}: [{self.kind}] {self.label}: no column '{column}' "
                f"for {purpose}{hint}. The {self.form} has: {', '.join(header)}"
            )

        resolved = {
            column: find(column, spec.target)
            for spec in self.fields.values()
            for column in spec.columns
        }
        used = {name.casefold() for name in resolved.values()}

        if isinstance(self.extras, tuple):
            candidates = [find(column, "extras") for column in self.extras]
        elif self.extras:
            candidates = [name for name in header if name.strip()]
        else:
            candidates = []

        reserved = (
            attribute_universe(_TYPE_OF_KIND[self.kind]) if self.kind in _TYPE_OF_KIND else None
        )
        kept: dict[str, str] = {}
        shadowed: list[str] = []
        for name in candidates:
            if name.casefold() in used or reserved is None:
                continue
            attribute = attribute_name(name)
            if attribute in reserved:
                # Kept under this name it would be hidden behind the model's
                # own field, so it is left out and reported instead.
                shadowed.append(name)
            elif attribute not in kept.values():
                kept[name] = attribute

        return BoundSection(self, resolved, kept, shadowed)


@dataclass
class BoundSection:
    """A section matched to a real header, ready to translate records."""

    section: Section
    #: Mapping column name -> the header it matched.
    resolved: dict[str, str]
    #: Unmapped header -> the attribute name it is kept under.
    kept: dict[str, str]
    #: Unmapped headers left out because a model field has their name.
    shadowed: list[str]
    #: (field, source value) -> rows that had no translation for it.
    untranslated: dict[tuple[str, str], int] = field(default_factory=dict)

    def translate(self, record: MappingType[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """One source record as model fields, plus the columns kept as extras.

        Values are text, as a CSV cell would be; the loader parses numbers,
        units and booleans exactly as it does for its own files.
        """
        values: dict[str, str] = {}
        for target, spec in self.section.fields.items():
            text = self._render(spec, record)
            if text is not None:
                values[target] = text

        extras = {
            attribute: record.get(name, "").strip()
            for name, attribute in self.kept.items()
            if record.get(name, "").strip()
        }
        return values, extras

    def _render(self, spec: FieldSpec, record: MappingType[str, str]) -> str | None:
        if spec.value is not None:
            text: str | None = spec.value
        elif spec.column is not None:
            text = record.get(self.resolved[spec.column], "").strip() or None
        else:
            text = _fill(spec.template or "", lambda c: record.get(self.resolved[c.strip()], ""))

        if text is None:
            return spec.default

        if spec.values:
            translated = _translate(spec.values, text)
            if translated is None:
                key = (spec.target, text)
                self.untranslated[key] = self.untranslated.get(key, 0) + 1
            else:
                text = translated

        if spec.unit is not None:
            quantity = split_quantity(text)
            if quantity is not None and quantity[1] is None:
                text = f"{text}{spec.unit}"
        return text

    def notes(self) -> list[str]:
        """What a person setting up the mapping should know about this file."""
        name = self.section.label
        notes: list[str] = []
        for (target, text), count in sorted(self.untranslated.items()):
            spec = self.section.fields[target]
            notes.append(
                f"{name}: {spec.source} value '{text}' has no translation for {target} "
                f"({count} row{'s' if count != 1 else ''}); used as written"
            )
        if self.shadowed:
            notes.append(
                f"{name}: not kept, because GridQL has an attribute of the same name "
                f"(map them to use them): {', '.join(self.shadowed)}"
            )
        if self.kept:
            notes.append(
                f"{name}: unmapped columns kept as attributes: {', '.join(self.kept.values())}"
            )
        return notes


@dataclass(frozen=True)
class Mapping:
    """A whole mapping file: the sections for each kind of object."""

    sections: MappingType[str, tuple[Section, ...]]
    path: Path | None = None

    def of(self, kind: str) -> tuple[Section, ...]:
        return tuple(self.sections.get(kind, ()))

    @property
    def reads_database(self) -> bool:
        """Whether the sections name tables and queries rather than files.

        A mapping is one or the other, so this is decided by any section.
        """
        return any(
            section.form != "file" for sections in self.sections.values() for section in sections
        )


def attribute_name(header: str) -> str:
    """The attribute an unmapped column is kept under: lowercase, underscores."""
    return header.strip().lower().replace(" ", "_").replace("-", "_")


# -- reading a mapping file ----------------------------------------------------


def load_mapping(path: str | Path) -> Mapping:
    """Read a TOML mapping file, reporting exactly what is wrong with it."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MappingError(f"cannot read {path}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise MappingError(f"{path}: {error}") from error
    return parse_mapping(data, path)


def parse_mapping(data: MappingType[str, Any], path: str | Path | None = None) -> Mapping:
    """Build a mapping from already-parsed TOML."""
    origin = str(path) if path is not None else "mapping"

    unknown = sorted(set(data) - set(TARGETS))
    if unknown:
        raise MappingError(
            f"{origin}: unknown section{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(unknown)}{_suggest(unknown[0], TARGETS)}; "
            f"expected {', '.join(TARGETS)}"
        )

    sections: dict[str, tuple[Section, ...]] = {}
    for kind in TARGETS:
        raw = data.get(kind)
        if raw is None:
            continue
        entries = raw if isinstance(raw, list) else [raw]
        if not all(isinstance(entry, dict) for entry in entries):
            raise MappingError(f"{origin}: [{kind}] must be a table, or [[{kind}]] tables")
        sections[kind] = tuple(_section(kind, entry, origin) for entry in entries)

    if not sections.get("devices"):
        raise MappingError(
            f"{origin}: no [[devices]] section; a mapping has to say where the equipment is"
        )

    forms = {section.form != "file" for entries in sections.values() for section in entries}
    if len(forms) > 1:
        raise MappingError(
            f"{origin}: some sections name a file and others a table or query; "
            "a mapping reads either CSV files or a database, not both"
        )
    return Mapping(sections, Path(path) if path is not None else None)


def _section(kind: str, data: MappingType[str, Any], origin: str) -> Section:
    given = [key for key in SOURCES if key in data]
    if not given:
        raise MappingError(
            f"{origin}: every [{kind}] section needs file = \"<name>.csv\", "
            f"or, reading from Postgres, table = \"<schema.table>\" or query = \"SELECT ...\""
        )
    if len(given) > 1:
        raise MappingError(
            f"{origin}: a [{kind}] section sets {' and '.join(given)}; "
            "it reads from exactly one of file, table or query"
        )
    form = given[0]
    source = data[form]
    if not isinstance(source, str) or not source.strip():
        raise MappingError(f"{origin}: a [{kind}] section's {form} must be non-empty text")
    section = Section(kind, source.strip(), {}, origin=origin, form=form)
    where = f"{origin}: [{kind}] {section.label}"

    allowed = TARGETS[kind]
    unknown = [key for key in data if key not in allowed and key not in _SETTINGS]
    if unknown:
        raise MappingError(
            f"{where}: '{unknown[0]}' is not a field of {kind}"
            f"{_suggest(unknown[0], (*allowed, *_SETTINGS))}; "
            f"it can map {', '.join(allowed)}"
        )

    fields = {
        target: _field(target, data[target], where)
        for target in allowed
        if target in data
    }
    missing = [target for target in _REQUIRED[kind] if target not in fields]
    if missing:
        raise MappingError(f"{where}: nothing maps {', '.join(missing)}")

    extras = data.get("extras", True)
    if kind == "connections" and "extras" in data:
        raise MappingError(f"{where}: connections carry no attributes, so extras does not apply")
    if isinstance(extras, list) and all(isinstance(item, str) for item in extras):
        extras = tuple(extras)
    elif not isinstance(extras, bool):
        raise MappingError(f"{where}: extras must be true, false, or a list of column names")

    return Section(kind, section.source, fields, extras, origin, form)


def _field(target: str, raw: Any, where: str) -> FieldSpec:
    if isinstance(raw, str):
        raw = {"template": raw} if "{" in raw or "}" in raw else {"column": raw}
    if not isinstance(raw, dict):
        raise MappingError(
            f"{where}: {target} must be a column name, a \"{{COLUMN}}\" template, "
            "or a table"
        )

    unknown = [key for key in raw if key not in _SPEC_KEYS]
    if unknown:
        raise MappingError(
            f"{where}: {target} has unknown key '{unknown[0]}'"
            f"{_suggest(unknown[0], _SPEC_KEYS)}; expected {', '.join(_SPEC_KEYS)}"
        )

    sources = [key for key in ("column", "template", "value") if key in raw]
    if len(sources) != 1:
        raise MappingError(
            f"{where}: {target} needs exactly one of column, template or value"
        )

    column = _text(raw.get("column"), f"{where}: {target}.column")
    template = _text(raw.get("template"), f"{where}: {target}.template")
    value = _scalar_text(raw["value"], f"{where}: {target}.value") if "value" in raw else None
    if column is not None and not column.strip():
        raise MappingError(f"{where}: {target}.column is empty")
    if template is not None:
        _check_template(template, f"{where}: {target}")

    unit = _text(raw.get("unit"), f"{where}: {target}.unit")
    if unit is not None:
        _check_unit(target, unit, where)

    values = raw.get("values", {})
    if not isinstance(values, dict):
        raise MappingError(f"{where}: {target}.values must be a table of source = target")
    translations = {
        str(key): _scalar_text(result, f"{where}: {target}.values.{key}")
        for key, result in values.items()
    }

    default = raw.get("default")
    if default is not None:
        default = _scalar_text(default, f"{where}: {target}.default")

    return FieldSpec(target, column, template, value, unit, translations, default)


def _check_template(template: str, where: str) -> None:
    names = _PLACEHOLDER.findall(template)
    stray = _PLACEHOLDER.sub("", template)
    if not names or "{" in stray or "}" in stray:
        raise MappingError(
            f"{where}: template '{template}' must name columns in single braces, "
            "as in \"SW-{OBJECTID}\""
        )
    if any(not name.strip() for name in names):
        raise MappingError(f"{where}: template '{template}' has an empty {{}}")


def _check_unit(target: str, unit: str, where: str) -> None:
    canonical = canonical_unit(target)
    if canonical is None:
        raise MappingError(f"{where}: {target} takes no unit, so unit = '{unit}' has no meaning")
    if not is_unit(unit):
        raise MappingError(f"{where}: {target}: unknown unit '{unit}'")
    wanted, _ = lookup(canonical)
    given, _ = lookup(unit)
    if given != wanted:
        raise MappingError(
            f"{where}: {target} is stored in {canonical}, and '{unit}' is "
            f"{given.replace('_', ' ')}, not {wanted.replace('_', ' ')}"
        )


def _text(value: Any, where: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise MappingError(f"{where} must be text")


def _scalar_text(value: Any, where: str) -> str:
    """A TOML scalar as the text a CSV cell would hold."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise MappingError(f"{where} must be text, a number or a boolean")


def _suggest(word: str, choices) -> str:
    close = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.6)
    return f" (did you mean {close[0]}?)" if close else ""


def _fill(template: str, lookup) -> str | None:
    """A template with its columns filled in, or None if any of them is empty.

    An identifier built around a hole -- "SW-" -- would be a wrong answer
    that looks like a real one, so an empty column empties the whole field.
    """
    empty = False

    def replace(match: re.Match) -> str:
        nonlocal empty
        text = lookup(match.group(1)).strip()
        if not text:
            empty = True
        return text

    filled = _PLACEHOLDER.sub(replace, template)
    return None if empty else filled


def _translate(table: MappingType[str, str], text: str) -> str | None:
    if text in table:
        return table[text]
    folded = text.casefold()
    for key, result in table.items():
        if key.casefold() == folded:
            return result
    return None
