-- SPDX-FileCopyrightText: 2026 Index Labs, LLC
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- GridQL storage schema.
--
-- Class-table inheritance: every piece of equipment has a row in `devices`,
-- and types with extra attributes have a matching row in an extension table
-- keyed by the same mRID. That mirrors how CIM specialises ConductingEquipment,
-- and it keeps the shared columns in one place to query and index.
--
-- Nothing in the language layer knows these tables exist. The loader turns
-- them into a Network; GridQL only ever sees the Network.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS substations (
    mrid    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    voltage REAL,
    extras  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feeders (
    mrid       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    voltage    REAL,
    substation TEXT REFERENCES substations(mrid) ON DELETE SET NULL,
    head       TEXT,  -- mRID of the source device; the root of the topology
    extras     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS devices (
    mrid        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    device_type TEXT NOT NULL,
    phases      TEXT,
    voltage     REAL,
    feeder      TEXT REFERENCES feeders(mrid) ON DELETE SET NULL,
    substation  TEXT REFERENCES substations(mrid) ON DELETE SET NULL,
    extras      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS switches (
    device_mrid  TEXT PRIMARY KEY REFERENCES devices(mrid) ON DELETE CASCADE,
    normal_state TEXT NOT NULL,
    state        TEXT NOT NULL,
    is_tie       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transformers (
    device_mrid       TEXT PRIMARY KEY REFERENCES devices(mrid) ON DELETE CASCADE,
    kva               REAL,
    primary_voltage   REAL,
    secondary_voltage REAL
);

CREATE TABLE IF NOT EXISTS lines (
    device_mrid TEXT PRIMARY KEY REFERENCES devices(mrid) ON DELETE CASCADE,
    length      REAL,
    conductor   TEXT,
    ampacity    REAL
);

CREATE TABLE IF NOT EXISTS loads (
    device_mrid TEXT PRIMARY KEY REFERENCES devices(mrid) ON DELETE CASCADE,
    kw          REAL,
    kvar        REAL
);

CREATE TABLE IF NOT EXISTS capacitors (
    device_mrid  TEXT PRIMARY KEY REFERENCES devices(mrid) ON DELETE CASCADE,
    kvar         REAL,
    normal_state TEXT NOT NULL,
    state        TEXT NOT NULL
);

-- Connectivity is undirected. Each edge is stored once, with the lower mRID
-- first, so a circuit cannot pick up mirrored duplicates.
CREATE TABLE IF NOT EXISTS connections (
    from_device TEXT NOT NULL REFERENCES devices(mrid) ON DELETE CASCADE,
    to_device   TEXT NOT NULL REFERENCES devices(mrid) ON DELETE CASCADE,
    PRIMARY KEY (from_device, to_device),
    CHECK (from_device < to_device)
);

-- Connectivity nodes, where the source recorded them: each terminal of a
-- device, in order, and the node it is attached to. Every pair of devices on
-- a node is also in `connections`, so a reader that ignores this table still
-- sees the same adjacency; this one is what tells a branch point from a loop.
CREATE TABLE IF NOT EXISTS terminals (
    device   TEXT NOT NULL REFERENCES devices(mrid) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    node     TEXT NOT NULL,
    PRIMARY KEY (device, sequence)
);

CREATE INDEX IF NOT EXISTS devices_by_feeder ON devices(feeder);
CREATE INDEX IF NOT EXISTS devices_by_type ON devices(device_type);
CREATE INDEX IF NOT EXISTS connections_by_to ON connections(to_device);
CREATE INDEX IF NOT EXISTS terminals_by_node ON terminals(node);
