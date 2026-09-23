# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read a network straight out of a utility's Postgres database.

GIS and asset systems keep the network in a database, in a schema of their
own. There is no standard one to guess at, so a mapping file (:mod:`.mapping`)
says which tables hold what -- the same mapping a CSV export would use, with
``table`` where it has ``file``, or a ``query`` for a join or a filter::

    [[devices]]
    table  = "gis.switch"
    mrid   = "facility_id"
    state  = { column = "position", values = { O = "OPEN", C = "CLOSED" } }

    [[devices]]
    query  = "SELECT * FROM gis.transformer WHERE status = 'IN SERVICE'"
    mrid   = "facility_id"

Everything is read in one read-only, repeatable-read transaction. The import
cannot change the database, and every table is read as it stood at the same
moment, so an edit made while the import runs cannot leave a device on a
feeder the feeder table has not caught up with.

Values come back typed. Each is written as the text a CSV cell would hold,
so the mapping and the loader treat a database exactly as they treat an
export of it. Columns that hold no plain value -- geometry, bytea, json,
arrays -- are not kept as attributes unless a section's ``extras`` names
them.

The connection is a libpq connection string or URL; whatever it leaves out
comes from the PG* environment variables, ``~/.pgpass`` and
``pg_service.conf``, so a password need never be written into a project.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..errors import GridQLError
from ..model import Network
from .csv_files import CsvDocument, CsvReport, _Row, _scalar, assemble
from .mapping import TARGETS, Mapping, Section, load_mapping


class PostgresError(GridQLError):
    """The database could not be read as a network."""


#: pg_type categories plain enough to keep as attributes: strings, numbers,
#: booleans, dates and times, and enums.
_PLAIN_CATEGORIES = frozenset("SNBDE")

#: Types outside those categories that still hold one plain value.
_PLAIN_TYPES = frozenset({"uuid"})

#: Rows fetched from the server at a time.
_BATCH = 2000

_RELATIONS = """
    SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid), c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname <> 'information_schema'
      AND n.nspname NOT LIKE 'pg\\_%'
    ORDER BY 1, 2
"""

_PRIMARY_KEY = """
    SELECT a.attname
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
    WHERE i.indrelid = %s AND i.indisprimary
    ORDER BY array_position(i.indkey::int2[], a.attnum)
"""

_TYPES = """
    SELECT oid, typname, pg_catalog.format_type(oid, NULL), typcategory
    FROM pg_catalog.pg_type WHERE oid = ANY (%s)
"""


# -- reading ------------------------------------------------------------


def load_postgres(conninfo: str, mapping: str | Path | Mapping) -> Network:
    """Read a network from the Postgres tables a mapping names."""
    return read_postgres(conninfo, mapping).network


def read_postgres(conninfo: str, mapping: str | Path | Mapping) -> CsvDocument:
    """Read the tables a mapping names, returning the network and a report.

    ``conninfo`` is a libpq connection string or URL, such as
    ``postgresql://gis@gis-db/utility`` or ``service=gis``; an empty one
    means the PG* environment variables say everything.
    """
    if not isinstance(mapping, Mapping):
        mapping = load_mapping(mapping)
    if not mapping.reads_database:
        where = f"{mapping.path} names" if mapping.path else "the mapping names"
        raise PostgresError(
            f"{where} CSV files, not tables; read them with --csv or 'gridql import-csv'"
        )

    psycopg = _driver()
    try:
        connection = psycopg.connect(conninfo)
    except psycopg.Error as error:
        raise PostgresError(f"cannot connect to Postgres: {_message(error)}") from error

    report = CsvReport()
    with connection:
        # Set before the first statement, so they apply to the one
        # transaction everything below is read in.
        connection.read_only = True
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        source = describe(connection)
        reader = _Reader(connection, psycopg, report)
        tables = {
            kind: [row for section in mapping.of(kind) for row in reader.rows(section)]
            for kind in TARGETS
        }
    return assemble(tables, source, report)


def describe(connection) -> str:
    """Which database a connection reads, for reports. Never the password."""
    info = connection.info
    host = "localhost" if not info.host or info.host.startswith("/") else info.host
    return f"Postgres database {info.dbname} on {host}:{info.port}"


class _Reader:
    """Reads mapping sections over one connection."""

    def __init__(self, connection, psycopg, report: CsvReport) -> None:
        self.connection = connection
        self.psycopg = psycopg
        self.report = report
        self._relations: list[tuple[str, str, bool, int]] | None = None

    def rows(self, section: Section) -> list[_Row]:
        """Every record a section reads, translated by its mapping."""
        where = f"{section.origin}: [{section.kind}] {section.label}"
        try:
            if section.form == "table":
                statement, key = self._select(section.source, where)
            else:
                # DECLARE takes one statement, without the semicolon.
                statement, key = section.source.rstrip().rstrip(";"), ()

            with self.connection.cursor(name="gridql_rows") as cursor:
                cursor.itersize = _BATCH
                cursor.execute(statement)
                header = [column.name for column in cursor.description]
                _check_unique(header, where)
                bound = section.bind(header)
                if section.extras is True:
                    self._leave_out_values_that_are_not_plain(bound, cursor.description)

                rows = []
                for number, values in enumerate(cursor, start=1):
                    record = {name: _text(value) for name, value in zip(header, values)}
                    fields, extras = bound.translate(record)
                    rows.append(
                        _Row(
                            fields,
                            _which(record, key, number),
                            section.label,
                            {name: _scalar(text) for name, text in extras.items()},
                        )
                    )
        except self.psycopg.Error as error:
            hint = (
                "; a query must be a single SELECT"
                if isinstance(error, self.psycopg.errors.SyntaxError)
                else ""
            )
            raise PostgresError(f"{where}: {_message(error)}{hint}") from error

        self.report.notes.extend(bound.notes())
        return rows

    def _select(self, name: str, where: str):
        """The statement reading a table, in primary-key order, and that key.

        The order makes an import repeatable: when two rows share an mRID,
        the same one is kept every time.
        """
        from psycopg import sql

        schema, table, oid = self._relation(name, where)
        key = tuple(row[0] for row in self.connection.execute(_PRIMARY_KEY, [oid]))
        statement = sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, table))
        if key:
            statement += sql.SQL(" ORDER BY {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in key)
            )
        return statement, key

    def _relation(self, name: str, where: str) -> tuple[str, str, int]:
        """Find the table or view a mapping names, whatever its case."""
        parts = name.split(".")
        if len(parts) > 2 or not all(part.strip() for part in parts):
            raise PostgresError(f"{where}: '{name}' is not a table name; write table or schema.table")
        parts = [part.strip() for part in parts]

        if self._relations is None:
            self._relations = [tuple(row) for row in self.connection.execute(_RELATIONS)]

        if len(parts) == 2:
            wanted = (parts[0].casefold(), parts[1].casefold())
            matches = [r for r in self._relations if (r[0].casefold(), r[1].casefold()) == wanted]
            exact = [r for r in matches if (r[0], r[1]) == tuple(parts)]
        else:
            matches = [r for r in self._relations if r[2] and r[1].casefold() == name.casefold()]
            exact = [r for r in matches if r[1] == name]

        if exact or len(matches) == 1:
            schema, table, _, oid = (exact or matches)[0]
            return schema, table, oid
        if matches:
            found = ", ".join(_quoted(r[0], r[1]) for r in matches)
            raise PostgresError(
                f"{where}: '{name}' matches {found}, which differ only in case; "
                "write it exactly as the database does"
            )

        everywhere = [f"{r[0]}.{r[1]}" for r in self._relations]
        elsewhere = [q for q in everywhere if q.split(".", 1)[1].casefold() == name.casefold()]
        if len(parts) == 1 and elsewhere:
            hint = f"; it is not on the search path (did you mean {elsewhere[0]}?)"
        else:
            close = difflib.get_close_matches(name.casefold(), everywhere, n=1, cutoff=0.6)
            hint = f" (did you mean {close[0]}?)" if close else ""
        raise PostgresError(
            f"{where}: no table or view '{name}' in {self.connection.info.dbname}{hint}"
        )

    def _leave_out_values_that_are_not_plain(self, bound, description) -> None:
        """Keep geometry, bytea, json and arrays out of the attributes.

        A hex string of geometry is no use in a query and bloats every
        object; naming such a column in ``extras`` keeps it as text anyway.
        """
        oids = sorted({column.type_code for column in description})
        types = {
            oid: (typname, shown, category)
            for oid, typname, shown, category in self.connection.execute(_TYPES, [oids])
        }
        left_out = []
        for column in description:
            typname, shown, category = types.get(column.type_code, ("unknown", "unknown", "X"))
            if column.name in bound.kept and not (
                category in _PLAIN_CATEGORIES or typname in _PLAIN_TYPES
            ):
                del bound.kept[column.name]
                left_out.append(f"{column.name} ({shown})")
        if left_out:
            self.report.notes.append(
                f"{bound.section.label}: not kept as attributes, as they hold no plain value "
                f"(name them in extras to keep them as text): {', '.join(left_out)}"
            )


def _check_unique(header: list[str], where: str) -> None:
    seen: set[str] = set()
    for name in header:
        if name.casefold() in seen:
            raise PostgresError(
                f"{where}: two columns are named '{name}'; give one of them an alias with AS"
            )
        seen.add(name.casefold())


def _which(record: dict[str, str], key: tuple[str, ...], number: int) -> str:
    """How a report names a record: by its primary key, or its place."""
    if key and all(record.get(column) for column in key):
        return ", ".join(f"{column} {record[column]}" for column in key)
    return f"row {number}"


def _text(value: Any) -> str:
    """A database value as the text a CSV cell would hold for it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Without an exponent, so 1e16 reads as the number it is.
        return format(Decimal(repr(value)), "f") if math.isfinite(value) else repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _quoted(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def _message(error: Exception) -> str:
    """A driver error as one line, with the server's hint when it gave one."""
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if not primary:
        return " ".join(str(error).split())
    hint = getattr(diag, "message_hint", None)
    return f"{primary} ({hint})" if hint else primary


def _driver():
    try:
        import psycopg
    except ImportError as error:
        raise PostgresError(
            "reading from Postgres needs the psycopg driver; "
            "install it with: pip install 'gridql[postgres]'"
        ) from error
    return psycopg


# -- connection strings -------------------------------------------------

_PASSWORD = re.compile(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|[^\s&]+)", re.IGNORECASE)


def redact(conninfo: str) -> str:
    """A connection string safe to print: any password replaced by ***."""
    if "://" in conninfo:
        parts = urlsplit(conninfo)
        userinfo, at, hosts = parts.netloc.rpartition("@")
        if at and ":" in userinfo:
            user = userinfo.split(":", 1)[0]
            conninfo = urlunsplit(parts._replace(netloc=f"{user}:***@{hosts}"))
    return _PASSWORD.sub(r"\1***", conninfo)


def has_password(conninfo: str) -> bool:
    """Whether a connection string carries a password of its own."""
    return redact(conninfo) != conninfo
