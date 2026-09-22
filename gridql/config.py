# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``project.gridqlconfig``: where a project's data and queries live.

A utility's GridQL work is a repository -- a dataset, a directory of saved
queries, and the circuit those queries are usually about. Without somewhere
to record that, every command has to repeat it, and ``gridql run`` has no
idea which network the file is meant to be run against.

The file is TOML, found by walking up from the working directory the way git
finds its own::

    name    = "Oakdale District"
    db      = "grid.sqlite"      # or: csv = "gis-export"
    queries = "queries"

    [params]
    feeder = "FDR-104"

Paths resolve relative to the file, so the same config works from any
subdirectory. Everything in it is a *default*: an explicit ``--db``, ``--csv``
or ``--<param>`` on the command line wins.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import GridQLError
from .script import SUFFIX, find_scripts

#: The name searched for, from the working directory upwards.
CONFIG_NAME = "project.gridqlconfig"

#: Keys the file may set. Anything else is a mistake worth reporting, since
#: a silently ignored key looks exactly like a setting that does not work.
KEYS = ("name", "db", "csv", "queries", "params")


@dataclass(frozen=True)
class Config:
    """A loaded project config, or the empty one when there is no file."""

    path: Path | None = None
    name: str | None = None
    db: str | None = None
    csv: str | None = None
    queries: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.path is not None

    @property
    def root(self) -> Path | None:
        """The directory the config file sits in; paths resolve against it."""
        return None if self.path is None else self.path.parent

    def dataset(self) -> str:
        """How to describe the data this project points at."""
        if self.db:
            return _readable(self.db)
        if self.csv:
            return f"{_readable(self.csv)} (CSV)"
        return "the bundled sample network FDR-104"

    def describe(self) -> str:
        if self.path is None:
            return f"no {CONFIG_NAME} found; using the bundled sample network FDR-104"

        lines = [f"config:  {_readable(self.path)}"]
        if self.name:
            lines.append(f"project: {self.name}")
        lines.append(f"data:    {self.dataset()}")
        if self.queries:
            lines.append(f"queries: {_readable(self.queries)}")
        for key, value in sorted(self.params.items()):
            lines.append(f"param:   {key} = {value}")
        return "\n".join(lines)

    def scripts(self) -> list[Path]:
        """The .gridql files in the configured queries directory."""
        if not self.queries or not Path(self.queries).is_dir():
            return []
        return find_scripts(self.queries)


def _readable(path: str | Path) -> str:
    """A path as short as it can be without becoming ambiguous."""
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def find_config(start: str | Path | None = None) -> Path | None:
    """Search for a config file from ``start`` upwards, as git does."""
    directory = Path(start or Path.cwd()).resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        path = candidate / CONFIG_NAME
        if path.is_file():
            return path
    return None


def load_config(path: str | Path) -> Config:
    """Read a config file, reporting what is wrong with it rather than guessing."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise GridQLError(f"cannot read {path}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise GridQLError(f"{path}: {error}") from error

    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise GridQLError(
            f"{path}: unknown setting{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(unknown)}; expected {', '.join(KEYS)}"
        )

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise GridQLError(f"{path}: [params] must be a table of name = value")
    if data.get("db") and data.get("csv"):
        raise GridQLError(f"{path}: set either db or csv, not both")

    root = path.parent

    def _path(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise GridQLError(f"{path}: {key} must be a path")
        # Relative to the config, so the project works from any subdirectory.
        return str(root / value)

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise GridQLError(f"{path}: name must be a string")

    return Config(path, name, _path("db"), _path("csv"), _path("queries"), dict(params))


def project_config(
    explicit: str | Path | None = None,
    enabled: bool = True,
    start: str | Path | None = None,
) -> Config:
    """The config a command should use: the named one, the found one, or none."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise GridQLError(f"no such config file: {path}")
        return load_config(path)
    if not enabled:
        return Config()
    found = find_config(start)
    return Config() if found is None else load_config(found)


def resolve_script(config: Config, name: str) -> str:
    """Find a .gridql file: as given, or by name in the project's queries."""
    given = Path(name)
    if given.exists():
        return str(given)

    candidates: list[Path] = []
    if given.suffix != SUFFIX:
        candidates.append(given.with_suffix(SUFFIX))
    if config.queries:
        queries = Path(config.queries)
        candidates.append(queries / given.name)
        if given.suffix != SUFFIX:
            candidates.append(queries / (given.name + SUFFIX))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    if not candidates:
        return name
    tried = ", ".join(str(candidate) for candidate in (given, *candidates))
    raise GridQLError(f"no such query: {name}. Tried {tried}")
