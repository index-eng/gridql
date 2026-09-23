# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read an OpenDSS script into the objects it defines.

OpenDSS is a scripting language, not a data format: a model is whatever a
run of its commands leaves defined. This module runs the commands that
define things -- New, Edit, Like, Redirect, Open, Close, Enable, Disable
and the ``Class.name.property=value`` shorthand -- and records every other
command as seen but not acted on. It never solves the circuit.

What it produces is deliberately close to OpenDSS's own view: classes,
names and property values as written. Turning that into grid equipment is
the importer's job.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import GridQLError


class DssParseError(GridQLError):
    """The script could not be read."""


@dataclass
class DssObject:
    """One object a script defined: its class, name and properties as written."""

    cls: str  # lowercase class name: "line", "transformer", ...
    name: str  # as first written
    properties: dict[str, str] = field(default_factory=dict)  # lowercase keys
    #: Per-winding properties of a transformer or XfmrCode, keyed by winding number.
    windings: dict[int, dict[str, str]] = field(default_factory=dict)
    #: Where it was defined, for messages.
    source: str = ""

    @property
    def key(self) -> str:
        return self.name.lower()

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.properties.get(name, default)


@dataclass
class DssScript:
    """Everything a script left defined."""

    circuit: str | None = None
    objects: dict[tuple[str, str], DssObject] = field(default_factory=dict)
    #: In definition order, so the network is built in the order it was written.
    order: list[tuple[str, str]] = field(default_factory=list)
    #: Terminals opened or closed by Open and Close commands: (class, name) -> "OPEN"/"CLOSED".
    positions: dict[tuple[str, str], str] = field(default_factory=dict)
    voltage_bases: list[float] = field(default_factory=list)
    #: Commands that were read but that define nothing, by verb.
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def find(self, cls: str, name: str) -> DssObject | None:
        return self.objects.get((cls.lower(), name.lower()))

    def of_class(self, cls: str) -> list[DssObject]:
        return [self.objects[k] for k in self.order if k[0] == cls and k in self.objects]


# -- properties by position ----------------------------------------------
#
# OpenDSS lets a value stand without its name: it then sets the property
# after the last one set, in the class's own property order. Only the
# leading properties of the classes GridQL reads are listed; a positional
# value beyond them is kept under a placeholder and never read.

_ORDER = {
    "line": ["bus1", "bus2", "linecode", "length", "phases", "r1", "x1", "r0", "x0",
             "c1", "c0", "rmatrix", "xmatrix", "cmatrix", "switch", "rg", "xg", "rho",
             "geometry", "units", "spacing", "wires", "earthmodel", "cncables",
             "tscables", "b1", "b0", "seasons", "ratings", "linetype", "normamps",
             "emergamps"],
    "load": ["phases", "bus1", "kv", "kw", "pf", "model", "yearly", "daily", "duty",
             "growth", "conn", "kvar"],
    "capacitor": ["bus1", "bus2", "phases", "kvar", "kv", "conn", "cmatrix", "cuf", "r",
                  "xl", "harm", "numsteps", "states"],
    "reactor": ["bus1", "bus2", "phases", "kvar", "kv", "conn", "rmatrix", "xmatrix",
                "parallel", "r", "x"],
    "vsource": ["bus1", "basekv", "pu", "angle", "frequency", "phases", "mvasc3", "mvasc1"],
    "transformer": ["phases", "windings", "wdg", "bus", "conn", "kv", "kva", "tap", "%r",
                    "rneut", "xneut", "buses", "conns", "kvs", "kvas", "taps", "xhl",
                    "xht", "xlt"],
    "linecode": ["nphases", "r1", "x1", "r0", "x0", "c1", "c0", "units", "rmatrix",
                 "xmatrix", "cmatrix", "basefreq", "normamps", "emergamps"],
}
_ORDER["xfmrcode"] = _ORDER["transformer"]
_ORDER["circuit"] = _ORDER["vsource"]

#: Transformer properties that belong to the active winding.
_WINDING = {"bus", "conn", "kv", "kva", "tap", "%r", "rneut", "xneut", "rdcohms"}
#: ... and the array forms that set every winding at once.
_WINDING_ARRAYS = {"buses": "bus", "conns": "conn", "kvs": "kv", "kvas": "kva", "taps": "tap"}

_ALIASES = {"~": "more", "m": "more"}


def read_script(path: str | Path) -> DssScript:
    """Run the defining commands of the script at ``path`` and those it redirects to."""
    script = DssScript()
    _Runner(script).run_file(Path(path))
    return script


def loads_script(text: str, name: str = "<string>") -> DssScript:
    script = DssScript()
    _Runner(script).run_text(text, Path.cwd(), name)
    return script


class _Runner:
    def __init__(self, script: DssScript) -> None:
        self.script = script
        self.active: DssObject | None = None
        self.depth = 0

    # -- input ----------------------------------------------------------

    def run_file(self, path: Path) -> None:
        resolved = _find(path)
        if resolved is None:
            raise DssParseError(f"cannot read {path}: no such file")
        if self.depth > 32:
            raise DssParseError(f"{path}: redirects nest too deeply; is there a cycle?")
        try:
            text = resolved.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as error:
            raise DssParseError(f"cannot read {path}: {error.strerror or error}") from error
        self.script.files.append(str(resolved))
        self.depth += 1
        try:
            self.run_text(text, resolved.parent, resolved.name)
        finally:
            self.depth -= 1

    def run_text(self, text: str, directory: Path, name: str) -> None:
        for number, command in _commands(text):
            try:
                self.run(command, directory)
            except DssParseError as error:
                raise DssParseError(f"{name}, line {number}: {error}") from None

    # -- commands -------------------------------------------------------

    def run(self, command: str, directory: Path) -> None:
        tokens = _tokens(command)
        if not tokens:
            return
        verb_name, verb_value = tokens[0]
        verb = (verb_value if verb_name is None else f"{verb_name}={verb_value}").lower()
        verb = _ALIASES.get(verb, verb)
        rest = tokens[1:]

        if verb == "more":
            if self.active is None:
                raise DssParseError("a continuation line with nothing to continue")
            self.edit(self.active, rest)
        elif verb == "new":
            self.new(rest)
        elif verb == "edit":
            target, rest = self.target(rest)
            self.edit(self.require(target), rest)
        elif verb in ("redirect", "compile"):
            if not rest:
                raise DssParseError(f"{verb} needs a file name")
            # Models are mostly written on Windows: subdir\file.dss.
            self.run_file(directory / _unquote(rest[0][1]).replace("\\", "/"))
        elif verb == "clear":
            self.script.__init__()  # a Clear starts the model again
            self.active = None
        elif verb in ("open", "close"):
            target, _ = self.target(rest)
            obj = self.require(target)
            self.script.positions[(obj.cls, obj.key)] = "OPEN" if verb == "open" else "CLOSED"
        elif verb in ("enable", "disable"):
            target, _ = self.target(rest)
            self.require(target).properties["enabled"] = "yes" if verb == "enable" else "no"
        elif verb == "set":
            self.set(rest)
        elif verb_name is not None and verb_name.count(".") >= 2:
            # Line.Sw7.enabled=no: an edit written as an assignment.
            cls, _, rest_of_name = verb_name.partition(".")
            name, _, prop = rest_of_name.rpartition(".")
            obj = self.script.find(cls, name)
            if obj is None:
                self.skip("assignment")
            else:
                self.edit(obj, [(prop, verb_value), *rest])
        else:
            self.skip(verb.split("=")[0])

    def new(self, tokens: list[tuple[str | None, str]]) -> None:
        if not tokens:
            raise DssParseError("New needs an object to create")
        first_name, first_value = tokens[0]
        if first_name is not None and first_name.lower() != "object":
            raise DssParseError(f"New expects Class.name, not '{first_name}='")
        cls, _, name = first_value.partition(".")
        cls = cls.lower()
        if not name:
            raise DssParseError(f"'{first_value}' is not Class.name")
        if cls == "circuit":
            # A circuit is its source: New Circuit makes Vsource.source.
            self.script.circuit = name
            cls, name = "vsource", "source"

        obj = DssObject(cls, name)
        key = (cls, obj.key)
        if key in self.script.objects:
            self.note(f"{cls}.{name} is defined twice; the later definition wins")
        else:
            self.script.order.append(key)
        self.script.objects[key] = obj
        self.active = obj
        self.edit(obj, tokens[1:])

    def edit(self, obj: DssObject, tokens: list[tuple[str | None, str]]) -> None:
        order = _ORDER.get(obj.cls, [])
        last = -1
        for name, value in tokens:
            if name is None:
                last += 1
                name = order[last] if last < len(order) else f"#{last}"
            else:
                name = name.lower()
                if name in order:
                    last = order.index(name)
            value = _unquote(value)

            if name == "like":
                self.like(obj, value)
            elif name == "xfmrcode" and obj.cls == "transformer":
                code = self.script.find("xfmrcode", value)
                if code is None:
                    self.note(f"{obj.cls}.{obj.name}: XfmrCode '{value}' is not defined")
                else:
                    _copy(code, obj)
                obj.properties[name] = value
            elif obj.cls in ("transformer", "xfmrcode") and name == "wdg":
                obj.properties["wdg"] = value
            elif obj.cls in ("transformer", "xfmrcode") and name in _WINDING:
                winding = int(_number(obj.properties.get("wdg", "1")) or 1)
                obj.windings.setdefault(winding, {})[name] = value
            elif obj.cls in ("transformer", "xfmrcode") and name in _WINDING_ARRAYS:
                for index, item in enumerate(array(value), start=1):
                    obj.windings.setdefault(index, {})[_WINDING_ARRAYS[name]] = item
            else:
                obj.properties[name] = value

    def like(self, obj: DssObject, other: str) -> None:
        source = self.script.find(obj.cls, other)
        if source is None:
            self.note(f"{obj.cls}.{obj.name}: like={other} names nothing defined")
            return
        _copy(source, obj)

    def set(self, tokens: list[tuple[str | None, str]]) -> None:
        for name, value in tokens:
            if name is not None and name.lower() in ("voltagebases", "voltagebase"):
                self.script.voltage_bases = [
                    number for number in (_number(v) for v in array(_unquote(value)))
                    if number is not None
                ]

    def target(self, tokens: list[tuple[str | None, str]]):
        if not tokens:
            raise DssParseError("expected Class.name")
        name, value = tokens[0]
        if name is not None and name.lower() != "object":
            raise DssParseError(f"expected Class.name, not '{name}='")
        return value, tokens[1:]

    def require(self, target: str) -> DssObject:
        cls, _, name = target.partition(".")
        if cls.lower() == "circuit":
            cls, name = "vsource", "source"
        obj = self.script.find(cls, name)
        if obj is None:
            raise DssParseError(f"'{target}' is not defined")
        return obj

    def skip(self, verb: str) -> None:
        self.script.skipped[verb] = self.script.skipped.get(verb, 0) + 1

    def note(self, message: str) -> None:
        self.script.notes.append(message)


def _copy(source: DssObject, target: DssObject) -> None:
    target.properties.update(source.properties)
    for number, values in source.windings.items():
        target.windings.setdefault(number, {}).update(values)


def _find(path: Path) -> Path | None:
    """The file, matching its name without regard to case as OpenDSS on Windows does."""
    if path.is_file():
        return path
    directory = path.parent if str(path.parent) else Path(".")
    if directory.is_dir():
        wanted = path.name.lower()
        for candidate in directory.iterdir():
            if candidate.name.lower() == wanted and candidate.is_file():
                return candidate
    return None


# -- lexing ---------------------------------------------------------------

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_OPENERS = {'"': '"', "'": "'", "[": "]", "(": ")", "{": "}"}


def _commands(text: str):
    """(line number, command) pairs, comments removed and continuations joined."""
    text = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if line:
            yield number, line


def _strip_comment(line: str) -> str:
    """Drop a ``!`` or ``//`` comment, but not one inside a quoted or bracketed value."""
    closing: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if closing:
            if char == closing[-1]:
                closing.pop()
            elif char in _OPENERS and char not in "\"'":
                closing.append(_OPENERS[char])
        elif char in _OPENERS:
            closing.append(_OPENERS[char])
        elif char == "!" or line.startswith("//", index):
            return line[:index]
        index += 1
    return line


def _tokens(command: str) -> list[tuple[str | None, str]]:
    """``name=value`` pairs and bare values. Commas separate as spaces do."""
    tokens: list[tuple[str | None, str]] = []
    index, length = 0, len(command)
    while index < length:
        while index < length and (command[index].isspace() or command[index] == ","):
            index += 1
        if index >= length:
            break
        name = None
        start = index
        while index < length and not command[index].isspace() and command[index] not in "=,":
            if command[index] in _OPENERS:
                break
            index += 1
        word = command[start:index]
        # A name, then '=' perhaps with spaces round it.
        look = index
        while look < length and command[look].isspace():
            look += 1
        if word and look < length and command[look] == "=":
            name = word
            index = look + 1
            while index < length and command[index].isspace():
                index += 1
            value, index = _value(command, index)
        elif word:
            value = word
            if index < length and command[index] in _OPENERS:
                more, index = _value(command, index)
                value += more
        else:
            value, index = _value(command, index)
        tokens.append((name, value))
    return tokens


def _value(command: str, index: int) -> tuple[str, int]:
    length = len(command)
    if index < length and command[index] in _OPENERS:
        closing = [_OPENERS[command[index]]]
        start = index
        index += 1
        while index < length and closing:
            char = command[index]
            if char == closing[-1]:
                closing.pop()
            elif char in _OPENERS and char not in "\"'":
                closing.append(_OPENERS[char])
            index += 1
        return command[start:index], index
    start = index
    while index < length and not command[index].isspace() and command[index] != ",":
        index += 1
    return command[start:index], index


def _unquote(value: str) -> str:
    """Strip the quotes a single value was written in; leave arrays bracketed."""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


# -- values ---------------------------------------------------------------


def array(value: str) -> list[str]:
    """``[a b c]``, ``(a, b)`` or ``"a b"`` as its items. A matrix's rows run together."""
    value = value.strip()
    if value and value[0] in "[({\"'" and value[-1] in "])}\"'":
        value = value[1:-1]
    return [item for item in re.split(r"[\s,|]+", value) if item]


def number(value: str | None) -> float | None:
    """A property value as a number, evaluating OpenDSS's RPN form ``(1 2 +)``."""
    return _number(value)


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    text = _unquote(value.strip())
    if text and text[0] in "([{" and text[-1] in ")]}":
        items = array(text)
        if len(items) > 1:
            return _rpn(items)
        text = items[0] if items else ""
    try:
        return float(text)
    except ValueError:
        return None


_BINARY = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "^": lambda a, b: a ** b,
}
_UNARY = {"sqr": lambda a: a * a, "sqrt": math.sqrt, "inv": lambda a: 1 / a}


def _rpn(items: list[str]) -> float | None:
    stack: list[float] = []
    try:
        for item in items:
            if item in _BINARY:
                b, a = stack.pop(), stack.pop()
                stack.append(_BINARY[item](a, b))
            elif item.lower() in _UNARY:
                stack.append(_UNARY[item.lower()](stack.pop()))
            else:
                stack.append(float(item))
    except (IndexError, ValueError, ZeroDivisionError):
        return None
    return stack[-1] if len(stack) == 1 else None
