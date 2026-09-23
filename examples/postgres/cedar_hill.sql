-- The Cedar Hill example as a utility's GIS database would hold it: the same
-- two feeders as examples/mapped, in typed tables in a gis schema, with the
-- customers behind each service point kept apart in the billing system's own
-- cis schema, as they usually are.
--
--   createdb cedar_hill
--   psql -d cedar_hill -f examples/postgres/cedar_hill.sql
--   gridql import-postgres postgresql:///cedar_hill \
--       --mapping examples/postgres/mapping.toml --db cedar-hill.sqlite
--
-- Running it again starts the two schemas over.

SET client_min_messages = warning;
DROP SCHEMA IF EXISTS gis CASCADE;
DROP SCHEMA IF EXISTS cis CASCADE;
CREATE SCHEMA gis;
CREATE SCHEMA cis;

CREATE TABLE gis.station (
    station_no    integer PRIMARY KEY,
    station_name  text,
    bus_volts     integer,
    source_kv     integer
);
INSERT INTO gis.station (station_no, station_name, bus_volts, source_kv) VALUES
    (12, 'Cedar Hill', 12470, 115);

CREATE TABLE gis.circuit (
    circuit_no    integer PRIMARY KEY,
    circuit_desc  text,
    station_no    integer,
    nom_volts     integer
);
INSERT INTO gis.circuit (circuit_no, circuit_desc, station_no, nom_volts) VALUES
    (1201, 'Cedar Hill 1201', 12, 12470),
    (1202, 'Cedar Hill 1202', 12, 12470);

CREATE TABLE gis.switch (
    facility_id      varchar(32) PRIMARY KEY,
    circuit_no       integer,
    station_no       integer,
    phasing          text,
    description      text,
    sw_type          text,
    op_volts         integer,
    from_node        integer,
    to_node          integer,
    position         char(1),
    normal_position  char(1),
    tie_flag         boolean
);
INSERT INTO gis.switch (facility_id, circuit_no, station_no, phasing, description, sw_type, op_volts, from_node, to_node, position, normal_position, tie_flag) VALUES
    ('BKR-1201', 1201, 12, 'ABC', 'Cedar Hill 1201 Breaker', 'BKR', 12470, 40001, 40101, 'C', 'C', false),
    ('REC-1201-01', 1201, 12, 'ABC', 'Midline Recloser', 'RCL', 12470, 40111, 40112, 'C', 'C', false),
    ('SW-1201-01', 1201, 12, 'ABC', 'Mainline Sectionalizing Switch', 'LBS', 12470, 40113, 40119, 'C', 'C', false),
    ('SEC-1201-01', 1201, 12, 'ABC', 'Ridge Rd Sectionalizer', 'SECT', 12470, 40120, 40121, 'C', 'C', false),
    ('BKR-1202', 1202, 12, 'ABC', 'Cedar Hill 1202 Breaker', 'BKR', 12470, 50001, 50101, 'C', 'C', false),
    ('REC-1202-01', 1202, 12, 'ABC', 'Midline Recloser', 'RCL', 12470, 50103, 50109, 'C', 'C', false),
    ('TIE-1201-1202', 1201, 12, 'ABC', 'Tie to Cedar Hill 1202', 'LBS', 12470, 40145, 50114, 'O', 'O', true);

CREATE TABLE gis.fuse (
    facility_id      varchar(32) PRIMARY KEY,
    circuit_no       integer,
    station_no       integer,
    phasing          text,
    description      text,
    op_volts         integer,
    from_node        integer,
    to_node          integer,
    position         char(1),
    normal_position  char(1),
    link_rating      text
);
INSERT INTO gis.fuse (facility_id, circuit_no, station_no, phasing, description, op_volts, from_node, to_node, position, normal_position, link_rating) VALUES
    ('FU-1201-01', 1201, 12, 'A', 'Maple Ave tap', 12470, 40103, 40104, 'C', 'C', '40T'),
    ('FU-1201-02', 1201, 12, 'ABC', 'Cedar Hill Plaza riser', 12470, 40113, 40114, 'C', 'C', '100E'),
    ('FU-1201-03', 1201, 12, 'B', 'Birchwood Ct tap', 12470, 40122, 40123, 'C', 'C', '40T'),
    ('FU-1201-04', 1201, 12, 'C', 'Quarry Ln tap', 12470, 40122, 40128, 'O', 'C', '25T'),
    ('FU-1201-05', 1201, 12, 'C', 'Oak St tap', 12470, 40135, 40136, 'C', 'C', '40T'),
    ('FU-1201-06', 1201, 12, 'ABC', 'Cedar Hill Elementary riser', 12470, 40141, 40142, 'C', 'C', '65E'),
    ('FU-1202-01', 1202, 12, 'A', 'Hillcrest Rd tap', 12470, 50103, 50104, 'C', 'C', '40T'),
    ('FU-1202-02', 1202, 12, 'ABC', 'Hillcrest Medical riser', 12470, 50110, 50111, 'C', 'C', '65E');

CREATE TABLE gis.capacitor (
    facility_id      varchar(32) PRIMARY KEY,
    circuit_no       integer,
    station_no       integer,
    phasing          text,
    description      text,
    op_volts         integer,
    node             integer,
    kvar_rating      integer,
    position         char(1),
    normal_position  char(1),
    control          text
);
INSERT INTO gis.capacitor (facility_id, circuit_no, station_no, phasing, description, op_volts, node, kvar_rating, position, normal_position, control) VALUES
    ('CAP-1201-01', 1201, 12, 'ABC', 'Mainline cap bank', 12470, 40111, 300, 'C', 'C', 'VAR');

CREATE TABLE gis.transformer (
    facility_id      varchar(32) PRIMARY KEY,
    circuit_no       integer,
    station_no       integer,
    phasing          text,
    description      text,
    op_volts         integer,
    from_node        integer,
    to_node          integer,
    kva_rating       numeric(8,1),
    high_side_volts  integer,
    low_side_volts   integer,
    mounting         text,
    install_year     smallint
);
INSERT INTO gis.transformer (facility_id, circuit_no, station_no, phasing, description, op_volts, from_node, to_node, kva_rating, high_side_volts, low_side_volts, mounting, install_year) VALUES
    ('TX-40117', 1201, 12, 'A', 'Pole 40117', 12470, 40105, 40106, 50, 7200, 240, 'POLE', 1994),
    ('TX-40121', 1201, 12, 'A', 'Pole 40121', 12470, 40107, 40108, 37.5, 7200, 240, 'POLE', 2003),
    ('TX-40126', 1201, 12, 'A', 'Pole 40126', 12470, 40109, 40110, 25, 7200, 240, 'POLE', 1988),
    ('TX-40210', 1201, 12, 'ABC', 'Pad 40210', 12470, 40115, 40116, 750, 12470, 480, 'PAD', 2006),
    ('TX-40214', 1201, 12, 'ABC', 'Pad 40214', 12470, 40117, 40118, 300, 12470, 208, 'PAD', 2006),
    ('TX-40318', 1201, 12, 'B', 'Pole 40318', 12470, 40124, 40125, 50, 7200, 240, 'POLE', 2015),
    ('TX-40322', 1201, 12, 'B', 'Pole 40322', 12470, 40126, 40127, 50, 7200, 240, 'POLE', 2015),
    ('TX-40331', 1201, 12, 'C', 'Pole 40331', 12470, 40129, 40130, 25, 7200, 240, 'POLE', 1976),
    ('TX-40335', 1201, 12, 'C', 'Pole 40335', 12470, 40131, 40132, 37.5, 7200, 240, 'POLE', 1991),
    ('TX-40340', 1201, 12, 'ABC', 'Pole 40340', 12470, 40133, 40134, 75, 12470, 208, 'POLE', 1999),
    ('TX-40412', 1201, 12, 'C', 'Pole 40412', 12470, 40137, 40138, 37.5, 7200, 240, 'POLE', 1987),
    ('TX-40416', 1201, 12, 'C', 'Pole 40416', 12470, 40139, 40140, 25, 7200, 240, 'POLE', 1979),
    ('TX-40512', 1201, 12, 'ABC', 'Pad 40512', 12470, 40143, 40144, 500, 12470, 480, 'PAD', 2012),
    ('TX-50117', 1202, 12, 'A', 'Pole 50117', 12470, 50105, 50106, 50, 7200, 240, 'POLE', 2001),
    ('TX-50120', 1202, 12, 'A', 'Pole 50120', 12470, 50107, 50108, 25, 7200, 240, 'POLE', 1983),
    ('TX-50214', 1202, 12, 'ABC', 'Pad 50214', 12470, 50112, 50113, 150, 12470, 208, 'PAD', 2019);

CREATE TABLE gis.conductor (
    facility_id   varchar(32) PRIMARY KEY,
    circuit_no    integer,
    station_no    integer,
    phasing       text,
    op_volts      integer,
    from_node     integer,
    to_node       integer,
    length_ft     integer,
    wire_size     text,
    rated_amps    integer,
    construction  text
);
INSERT INTO gis.conductor (facility_id, circuit_no, station_no, phasing, op_volts, from_node, to_node, length_ft, wire_size, rated_amps, construction) VALUES
    ('UG-1201-01', 1201, 12, 'ABC', 12470, 40101, 40102, 650, '1000 AL XLP', 575, 'UG'),
    ('OH-1201-01', 1201, 12, 'ABC', 12470, 40102, 40103, 1850, '477 ACSR Hawk', 659, 'OH'),
    ('OH-1201-11', 1201, 12, 'A', 12470, 40104, 40105, 780, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-12', 1201, 12, 'A', 12470, 40105, 40107, 640, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-13', 1201, 12, 'A', 12470, 40107, 40109, 520, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-02', 1201, 12, 'ABC', 12470, 40103, 40111, 2200, '477 ACSR Hawk', 659, 'OH'),
    ('OH-1201-03', 1201, 12, 'ABC', 12470, 40112, 40113, 1600, '336.4 ACSR Linnet', 529, 'OH'),
    ('UG-1201-02', 1201, 12, 'ABC', 12470, 40114, 40115, 420, '1/0 AL XLP', 175, 'UG'),
    ('UG-1201-03', 1201, 12, 'ABC', 12470, 40115, 40117, 260, '1/0 AL XLP', 175, 'UG'),
    ('OH-1201-04', 1201, 12, 'ABC', 12470, 40119, 40120, 2400, '336.4 ACSR Linnet', 529, 'OH'),
    ('OH-1201-21', 1201, 12, 'ABC', 12470, 40121, 40122, 1900, '1/0 ACSR Raven', 242, 'OH'),
    ('OH-1201-22', 1201, 12, 'B', 12470, 40123, 40124, 560, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-23', 1201, 12, 'B', 12470, 40124, 40126, 480, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-24', 1201, 12, 'C', 12470, 40128, 40129, 700, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-25', 1201, 12, 'C', 12470, 40129, 40131, 390, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-26', 1201, 12, 'ABC', 12470, 40122, 40133, 1100, '1/0 ACSR Raven', 242, 'OH'),
    ('OH-1201-05', 1201, 12, 'ABC', 12470, 40120, 40135, 1750, '336.4 ACSR Linnet', 529, 'OH'),
    ('OH-1201-31', 1201, 12, 'C', 12470, 40136, 40137, 620, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-32', 1201, 12, 'C', 12470, 40137, 40139, 540, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1201-06', 1201, 12, 'ABC', 12470, 40135, 40141, 1300, '336.4 ACSR Linnet', 529, 'OH'),
    ('UG-1201-04', 1201, 12, 'ABC', 12470, 40142, 40143, 310, '1/0 AL XLP', 175, 'UG'),
    ('OH-1201-07', 1201, 12, 'ABC', 12470, 40141, 40145, 950, '336.4 ACSR Linnet', 529, 'OH'),
    ('UG-1202-01', 1202, 12, 'ABC', 12470, 50101, 50102, 540, '1000 AL XLP', 575, 'UG'),
    ('OH-1202-01', 1202, 12, 'ABC', 12470, 50102, 50103, 2600, '477 ACSR Hawk', 659, 'OH'),
    ('OH-1202-11', 1202, 12, 'A', 12470, 50104, 50105, 900, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1202-12', 1202, 12, 'A', 12470, 50105, 50107, 450, '#2 ACSR Sparrow', 184, 'OH'),
    ('OH-1202-02', 1202, 12, 'ABC', 12470, 50109, 50110, 2100, '336.4 ACSR Linnet', 529, 'OH'),
    ('UG-1202-02', 1202, 12, 'ABC', 12470, 50111, 50112, 280, '1/0 AL XLP', 175, 'UG'),
    ('OH-1202-03', 1202, 12, 'ABC', 12470, 50110, 50114, 1500, '336.4 ACSR Linnet', 529, 'OH');

CREATE TABLE gis.service_point (
    facility_id    varchar(32) PRIMARY KEY,
    circuit_no     integer,
    station_no     integer,
    phasing        text,
    description    text,
    service_volts  integer,
    node           integer,
    demand_kw      integer,
    demand_kvar    integer
);
INSERT INTO gis.service_point (facility_id, circuit_no, station_no, phasing, description, service_volts, node, demand_kw, demand_kvar) VALUES
    ('SP-40117', 1201, 12, 'A', 'Maple Ave 100-114', 240, 40106, 27, 7),
    ('SP-40121', 1201, 12, 'A', 'Maple Ave 116-128', 240, 40108, 19, 5),
    ('SP-40126', 1201, 12, 'A', 'Maple Ave 130-136', 240, 40110, 11, 3),
    ('SP-40210', 1201, 12, 'ABC', 'Cedar Hill Plaza - Grocery', 480, 40116, 395, 130),
    ('SP-40214', 1201, 12, 'ABC', 'Cedar Hill Plaza - Retail', 208, 40118, 150, 48),
    ('SP-40318', 1201, 12, 'B', 'Birchwood Ct 2-12', 240, 40125, 26, 7),
    ('SP-40322', 1201, 12, 'B', 'Birchwood Ct 14-24', 240, 40127, 25, 7),
    ('SP-40331', 1201, 12, 'C', 'Quarry Ln 3-9', 240, 40130, 12, 3),
    ('SP-40335', 1201, 12, 'C', 'Quarry Ln 11-19', 240, 40132, 17, 5),
    ('SP-40340', 1201, 12, 'ABC', 'Ridge Rd Farm Supply', 208, 40134, 42, 14),
    ('SP-40412', 1201, 12, 'C', 'Oak St 201-211', 240, 40138, 18, 5),
    ('SP-40416', 1201, 12, 'C', 'Oak St 213-219', 240, 40140, 10, 3),
    ('SP-40512', 1201, 12, 'ABC', 'Cedar Hill Elementary', 480, 40144, 210, 70),
    ('SP-50117', 1202, 12, 'A', 'Hillcrest Rd 5-17', 240, 50106, 30, 8),
    ('SP-50120', 1202, 12, 'A', 'Hillcrest Rd 19-23', 240, 50108, 12, 3),
    ('SP-50214', 1202, 12, 'ABC', 'Hillcrest Medical Offices', 208, 50113, 88, 26);

CREATE TABLE cis.premise (
    premise_no     serial PRIMARY KEY,
    service_point  varchar(32) NOT NULL
);
-- One premise per customer, generated from how many each service point feeds.
INSERT INTO cis.premise (service_point)
SELECT service_point
FROM (VALUES
    ('SP-40117', 6),
    ('SP-40121', 5),
    ('SP-40126', 3),
    ('SP-40210', 1),
    ('SP-40214', 8),
    ('SP-40318', 6),
    ('SP-40322', 6),
    ('SP-40331', 4),
    ('SP-40335', 5),
    ('SP-40340', 1),
    ('SP-40412', 5),
    ('SP-40416', 3),
    ('SP-40512', 1),
    ('SP-50117', 7),
    ('SP-50120', 3),
    ('SP-50214', 4)
) AS served (service_point, customers), generate_series(1, customers);
