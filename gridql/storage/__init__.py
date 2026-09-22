# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Storage backends. SQLite persists a network; the language never sees it."""

from .sqlite import (
    SCHEMA_VERSION,
    StorageError,
    connect,
    create_schema,
    load_network,
    object_counts,
    open_database,
    save_network,
)

__all__ = [
    "SCHEMA_VERSION",
    "StorageError",
    "connect",
    "create_schema",
    "load_network",
    "object_counts",
    "open_database",
    "save_network",
]
