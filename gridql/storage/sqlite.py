# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SQLite persistence for the semantic model.

The database is a storage mechanism, not the thing GridQL queries. Loading
reads every row once and hands back a :class:`~gridql.model.network.Network`;
the topology graph then lives in memory, which is the right trade at the scale
this is built for and keeps traversal free of recursive SQL.

Swapping this module for Postgres, a CIM importer or a GIS export changes
where networks come from and nothing else -- the language never sees it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import GridQLError
from ..model import (
    Capacitor,
    Device,
    Feeder,
    LineSegment,
    Load,
    Network,
    Substation,
    Switch,
    Transformer,
)
from ..model.types import TYPE_CLASSES

SCHEMA_VERSION = "1"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Device class -> (extension table, its columns). Order matters: the first
#: class an object is an instance of wins, so put subclasses before their base.
_EXTENSIONS: tuple[tuple[type[Device], str, tuple[str, ...]], ...] = (
    (Transformer, "transformers", ("kva", "primary_voltage", "secondary_voltage")),
    (LineSegment, "lines", ("length", "conductor", "ampacity")),
    (Load, "loads", ("kw", "kvar")),
    (Capacitor, "capacitors", ("kvar", "normal_state", "state")),
    (Switch, "switches", ("normal_state", "state", "is_tie")),
)

_DEVICE_COLUMNS = (
    "mrid", "name", "device_type", "phases", "voltage", "feeder", "substation", "extras",
)


class StorageError(GridQLError):
    """The database could not be read, written or understood."""


# -- connections --------------------------------------------------------


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a database with the settings this schema expects."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def open_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a database, commit on success, and always close it.

    ``with sqlite3.connect(...)`` commits but leaves the connection open, so
    prefer this when reaching for the tables directly.
    """
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


def _extension_for(device: Device) -> tuple[str, tuple[str, ...]] | None:
    for cls, table, columns in _EXTENSIONS:
        if isinstance(device, cls):
            return table, columns
    return None


# -- saving -------------------------------------------------------------


def save_network(network: Network, target: str | Path | sqlite3.Connection) -> None:
    """Write a whole network to SQLite, replacing whatever was there."""
    owned = not isinstance(target, sqlite3.Connection)
    connection = connect(target) if owned else target
    try:
        with connection:
            create_schema(connection)
            _clear(connection)
            _write_substations(connection, network.substations)
            _write_feeders(connection, network.feeders)
            _write_devices(connection, network.devices)
            _write_connections(connection, network)
    except sqlite3.Error as error:
        raise StorageError(f"could not write the network: {error}") from error
    finally:
        if owned:
            connection.close()


def _clear(connection: sqlite3.Connection) -> None:
    for table in ("connections", "switches", "transformers", "lines", "loads",
                  "capacitors", "devices", "feeders", "substations"):
        connection.execute(f"DELETE FROM {table}")


def _write_substations(connection: sqlite3.Connection, substations: Sequence[Substation]) -> None:
    connection.executemany(
        "INSERT INTO substations(mrid, name, voltage, extras) VALUES (?, ?, ?, ?)",
        [(s.mrid, s.name, s.voltage, json.dumps(s.extras)) for s in substations],
    )


def _write_feeders(connection: sqlite3.Connection, feeders: Sequence[Feeder]) -> None:
    connection.executemany(
        "INSERT INTO feeders(mrid, name, voltage, substation, head, extras) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (f.mrid, f.name, f.voltage, f.substation, f.head, json.dumps(f.extras))
            for f in feeders
        ],
    )


def _write_devices(connection: sqlite3.Connection, devices: Sequence[Device]) -> None:
    placeholders = ", ".join("?" * len(_DEVICE_COLUMNS))
    connection.executemany(
        f"INSERT INTO devices({', '.join(_DEVICE_COLUMNS)}) VALUES ({placeholders})",
        [
            (d.mrid, d.name, d.TYPE, d.phases, d.voltage, d.feeder, d.substation,
             json.dumps(d.extras))
            for d in devices
        ],
    )

    for device in devices:
        extension = _extension_for(device)
        if extension is None:
            continue
        table, columns = extension
        values = [_to_sql(getattr(device, column, None)) for column in columns]
        connection.execute(
            f"INSERT INTO {table}(device_mrid, {', '.join(columns)}) "
            f"VALUES ({', '.join('?' * (len(columns) + 1))})",
            [device.mrid, *values],
        )


def _write_connections(connection: sqlite3.Connection, network: Network) -> None:
    edges = {
        (min(mrid, neighbor), max(mrid, neighbor))
        for mrid in network.objects
        for neighbor in network.neighbors(mrid)
    }
    connection.executemany(
        "INSERT INTO connections(from_device, to_device) VALUES (?, ?)", sorted(edges)
    )


def _to_sql(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


# -- loading ------------------------------------------------------------


def load_network(source: str | Path | sqlite3.Connection) -> Network:
    """Read a whole network out of SQLite -- the NetworkLoader.

    Feeders are restored with the head device they were saved with, so the
    topology comes back identical rather than being re-guessed.
    """
    owned = not isinstance(source, sqlite3.Connection)
    if owned and not Path(source).exists():  # type: ignore[arg-type]
        raise StorageError(f"no such database: {source}")

    connection = connect(source) if owned else source
    try:
        _check_version(connection)
        network = Network()

        for row in connection.execute("SELECT * FROM substations ORDER BY mrid"):
            network.add_substation(
                row["mrid"], name=row["name"], voltage=row["voltage"],
                extras=json.loads(row["extras"]),
            )

        heads: dict[str, str | None] = {}
        for row in connection.execute("SELECT * FROM feeders ORDER BY mrid"):
            network.add_feeder(
                row["mrid"], name=row["name"], voltage=row["voltage"],
                substation=row["substation"], extras=json.loads(row["extras"]),
            )
            heads[row["mrid"]] = row["head"]

        extensions = _read_extensions(connection)
        for row in connection.execute("SELECT * FROM devices ORDER BY mrid"):
            network.add(_build_device(row, extensions))

        for row in connection.execute("SELECT * FROM connections"):
            network.connect(row["from_device"], row["to_device"])

        for feeder in network.feeders:
            feeder.head = heads.get(feeder.mrid)

        return network
    except sqlite3.Error as error:
        raise StorageError(f"could not read the network: {error}") from error
    finally:
        if owned:
            connection.close()


def _check_version(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as error:
        raise StorageError(
            f"this file does not look like a GridQL database: {error}"
        ) from error

    if row is None:
        raise StorageError("this database has no schema version")
    if row["value"] != SCHEMA_VERSION:
        raise StorageError(
            f"database schema version {row['value']} does not match "
            f"this build's version {SCHEMA_VERSION}"
        )


def _read_extensions(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every extension row, keyed by device mRID."""
    found: dict[str, dict[str, Any]] = {}
    for _cls, table, columns in _EXTENSIONS:
        for row in connection.execute(f"SELECT * FROM {table}"):
            values = {column: row[column] for column in columns}
            if "is_tie" in values:
                values["is_tie"] = bool(values["is_tie"])
            found[row["device_mrid"]] = values
    return found


def _build_device(row: sqlite3.Row, extensions: dict[str, dict[str, Any]]) -> Device:
    device_type = row["device_type"]
    cls = TYPE_CLASSES.get(device_type)
    if cls is None or not issubclass(cls, Device):
        raise StorageError(f"unknown device type '{device_type}' for {row['mrid']}")

    fields: dict[str, Any] = {
        "name": row["name"],
        "phases": row["phases"],
        "voltage": row["voltage"],
        "feeder": row["feeder"],
        "substation": row["substation"],
        "extras": json.loads(row["extras"]),
    }
    fields.update(extensions.get(row["mrid"], {}))
    return cls(mrid=row["mrid"], **fields)  # type: ignore[arg-type]


# -- convenience --------------------------------------------------------


def object_counts(source: str | Path | sqlite3.Connection) -> dict[str, int]:
    """Row counts per table, for a quick look at what a database holds."""
    owned = not isinstance(source, sqlite3.Connection)
    connection = connect(source) if owned else source
    try:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("substations", "feeders", "devices", "connections")
        }
    finally:
        if owned:
            connection.close()
