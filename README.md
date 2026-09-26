# GridQL

A domain-specific query language for electric utility data and power-system models.

The grid that brings electricity to homes and businesses is a web of equipment: substations, power
lines, transformers on poles, and the switches and fuses that cut the power off when something goes
wrong. Utilities keep a record of every piece of it, and of how each piece is wired to the next.
GridQL is a way to ask questions of that record in the grid's own words (*which customers are beyond
this switch?*, *who is without power right now?*) without knowing how the database behind it is laid
out.

For engineers, planners, operators, and analysts, that means asking about a distribution network
with the concepts they already use: feeders, transformers, switches, reclosers, phases, voltage
levels, connectivity, topology, and energisation.

Instead of learning how a utility's GIS or asset database is organised, a user can ask:

``` text
FIND loads DOWNSTREAM OF "REC-1201-01" SELECT name, kw
```

and let GridQL resolve the answer from the network model: which loads, or customers, lie beyond
recloser `REC-1201-01`, an automatic circuit breaker partway along the line.

## Why GridQL?

Utility data is usually spread across GIS exports, asset databases, ADMS models, spreadsheets, CIM
files, and other system-specific representations. The same electrical question can therefore require
completely different database knowledge from one utility to another.

SQL is excellent for querying tables, but electrical topology is a domain concept. A question such
as:

> Which customers are downstream of this recloser?

requires more than finding rows. The answer depends on connectivity, feeder boundaries, normal
switch configuration, and the model's source information.

GridQL puts those semantics in a common model.

The intended flow is:

``` text
Utility data
    │
    ▼
Mapping / importer
    │
    ▼
GridQL semantic model
    │
    ├── physical connectivity
    ├── normal topology
    ├── current switch state
    ├── energization
    ├── feeder membership
    ├── protection zones
    └── utility-specific attributes
    │
    ▼
GridQL query
    │
    ▼
Table / JSON / CSV / CIM
```

Once a source has been mapped into GridQL's semantic model, the same query language can operate
across CSV, SQLite, Postgres, CIM, and OpenDSS data.

## The core idea: semantic queries, not database queries

GridQL is not intended to replace SQL.

SQL remains appropriate for billing, work orders, customer records, and other data that is
fundamentally relational rather than electrical.

GridQL is for questions where **the wiring is the point**.

For example:

``` text
FIND transformers DOWNSTREAM OF "REC-1201-01" WHERE kva >= 500
```

``` text
FIND loads WHERE NOT energized
```

``` text
FIND switches UPSTREAM OF "TX-40331" LIMIT 1
```

``` text
FIND loads SELECT protected_by, COUNT(*), SUM(kw)
GROUP BY protected_by
ORDER BY SUM(kw) DESC
```

These queries describe the question rather than the storage schema.

------------------------------------------------------------------------

## A 60-second example

A recloser is an automatic circuit breaker partway along a distribution line. Suppose a utility
wants to know which loads are downstream of it:

``` text
$ gridql 'FIND loads DOWNSTREAM OF "REC-1201-01" SELECT name, kw'

name                        kw
--------------------------  ---
Cedar Hill Plaza - Grocery  395
Cedar Hill Plaza - Retail   150
Ridge Rd Farm Supply        42
Oak St 201-211              18
Birchwood Ct 2-12           26
Quarry Ln 3-9               12
Oak St 213-219              10
Cedar Hill Elementary       210
Birchwood Ct 14-24          25
Quarry Ln 11-19             17

10 rows
```

The query does not need to know which tables contain equipment, how connections are represented, or
how the utility names those tables.

Now ask which loads are currently not energised:

``` text
$ gridql 'FIND loads WHERE NOT energized SELECT name, kw'

name             kw
---------------  --
Quarry Ln 3-9    12
Quarry Ln 11-19  17

2 rows
```

The important distinction is that `DOWNSTREAM OF` and `energized` answer different questions. GridQL
does not silently treat present switch state as the definition of downstream topology.

------------------------------------------------------------------------

## Core concepts

GridQL deliberately separates several concepts that are easy to conflate in an electrical network.

### 1. Physical connectivity

The physical graph describes what equipment is electrically connected to what other equipment.

Connectivity is represented independently of current switch state.

A connection may therefore exist physically even when a switch is currently open.

### 2. Normal topology

Normal topology describes the circuit as it is intended to operate under its normal configuration.

`DOWNSTREAM OF` and `UPSTREAM OF` use this topology.

Where a feeder is meshed and run radially by leaving a switch normally open, upstream and downstream
follow the normal configuration: nothing is fed through the normally open switch while another path
reaches the equipment beyond it.

### 3. Current topology

Current topology takes the present operating state of switches into account.

It is used when determining whether equipment is currently reachable from an energised source.

Operating a switch changes this view immediately.

### 4. Energisation

`energized` is a derived attribute.

GridQL determines energisation by starting at feeder sources and traversing the physical graph while
respecting the current state of switches.

Unlike normal-topology traversal, this traversal can cross feeder boundaries through a closed tie.

That means closing a normally open tie can cause the neighbouring feeder to become energised from
another source.

### 5. Feeder membership

A device belongs to exactly one feeder, independently of whether that feeder is currently energising
it.

Normal-topology traversal stays within the device's feeder. Feeders are tied to their neighbours, so
the physical graph runs across the whole system; it is feeder membership, not the tie's position,
that stops a downstream query on one feeder from swallowing the next.

`CONNECTED TO` describes physical adjacency and can cross feeder boundaries.

### 6. Protection zones

GridQL models a protection-zone abstraction around protective devices.

Breakers, reclosers, fuses, and sectionalizers are treated as protective devices. `PROTECTED BY "X"`
selects X's modelled protection zone: everything below X down to, and including, the next protective
devices. Each device's `protected_by` attribute names the nearest protective device above it.

This is a **topological protection abstraction**, not a protection-coordination or fault-current
calculation.

GridQL does not claim that a `PROTECTED BY` result replaces an engineering protection study.

------------------------------------------------------------------------

## A guiding principle: refuse rather than guess

Grid data is often incomplete, inconsistent, or ambiguous.

A query engine that silently chooses an answer when the model cannot establish the required
semantics can produce a result that looks authoritative but is wrong.

GridQL therefore favours explicit failure or validation findings over invented answers.

For example, if GridQL cannot establish a feeder head, it does not invent one simply to make
`DOWNSTREAM OF` return data.

The same principle runs through the rest of GridQL:

- a query that names a device that does not exist, or an attribute the type cannot have, is an error
  rather than an empty result
- `PROTECTED BY` a device that bounds no protection zone is refused, with the reason
- a CSV row that cannot be read, or a connection to a device that does not exist, is reported with
  its file and line rather than silently dropped
- a mapped value the mapping does not recognise is reported rather than reshaped
- validation reports invalid switch states, missing feeder heads, equipment on no feeder, and hard
  links between feeders

One case falls short of it. A loop inside a feeder that no normally open switch breaks gives
upstream and downstream more than one path. GridQL follows one of them, and validation warns that
the feeder is not radial, rather than refusing the query.

The goal is not to make every query return something.

The goal is to make the answer trustworthy when it does return something.

------------------------------------------------------------------------

## What GridQL is — and is not

### GridQL is

- A domain-specific query language for utility network data
- A semantic model for distribution-system equipment and topology
- A translation layer between heterogeneous utility data sources
- A way to create reusable, reviewable grid queries
- A way to export selected network data as CIM
- A Python library and command-line tool

### GridQL is not

- A SCADA system
- An ADMS
- A GIS
- An outage management system
- A billing system
- A power-flow solver
- A protection-coordination engine
- A state estimator
- A replacement for SQL

GridQL reasons over a network model. It does not perform electrical power-flow calculations.

------------------------------------------------------------------------

## Quickstart

``` bash
python3 -m gridql.cli                                  # REPL against example data
python3 -m gridql.cli 'FIND reclosers'                 # one-shot query
python3 -m gridql.cli --format json 'FIND loads'       # JSON output
python3 -m gridql.cli --color never 'FIND switches'    # disable terminal colour
python3 -m gridql.cli --explain 'FIND devices DOWNSTREAM OF "REC-1201-01"'

python3 -m gridql.cli run queries/feeder_analysis.gridql
python3 -m gridql.cli run feeder_report --feeder FDR-1202

python3 -m gridql.cli config
python3 -m gridql.cli init grid.sqlite

python3 -m gridql.cli --db grid.sqlite 'FIND feeders'

python3 -m gridql.cli export-cim feeder.xml \
    --query 'FIND devices FED BY "FDR-1201"'

python3 -m gridql.cli import-cim feeder.xml
python3 -m gridql.cli import-dss Master.dss --db grid.sqlite

python3 -m unittest discover -s tests
```

Installing the package with:

``` bash
pip install -e .
```

puts the `gridql` command on your path.

### From Python

``` python
from gridql import build_sample_network, execute, render

network = build_sample_network()

result = execute(
    network,
    'FIND switches WHERE state != normal_state'
)

print(result.mrids)
print(render(result, 'json'))
```

------------------------------------------------------------------------

## The language

The basic form is:

``` text
[ PARAM <name> [ = <value> ] ]*

FIND <type>
  [ DOWNSTREAM OF <device>
  | UPSTREAM OF <device>
  | CONNECTED TO <device>
  | FED BY <feeder-or-device>
  | PROTECTED BY <device> ]*
  [ WHERE <condition> ]
  [ SELECT <column> | <aggregate>, ... ]
  [ GROUP BY <column>, ... ]
  [ ORDER BY <column> [ASC|DESC], ... ]
  [ LIMIT <n> ]
  [ RETURN table | json | csv | cim ]
```

Clauses appear in that order.

Statements are separated by `;`.

Keywords are case-insensitive.

`--` and `#` start comments.

Parameters are described below.

------------------------------------------------------------------------

## Types

Built-in types include:

``` text
devices
switches
reclosers
breakers
fuses
sectionalizers
transformers
lines
loads
capacitors
feeders
substations
```

Common aliases such as `xfmrs`, `caps`, `conductors`, `subs`, and `circuits` are also supported.

`switches` is a supertype that includes reclosers, breakers, fuses, and ties.

`devices` matches conducting equipment but not feeder and substation containers.

The semantic model can also retain equipment that has no dedicated GridQL type, particularly when
imported from CIM.

------------------------------------------------------------------------

## Topology

| Relation | Meaning |
| --- | --- |
| `DOWNSTREAM OF "X"` | Equipment below X according to normal feeder topology; X is excluded |
| `UPSTREAM OF "X"` | Equipment between X and the feeder head according to normal topology; X is excluded |
| `CONNECTED TO "X"` | Immediate physical neighbours of X |
| `FED BY "X"` | Everything X supplies, X included; for a feeder or substation, all of its devices |
| `PROTECTED BY "X"` | X's modelled protection zone: everything below X down to, and including, the next protective devices |

### `DOWNSTREAM OF` and `UPSTREAM OF`

Normal-topology traversal deliberately stays within a feeder.

A normally open tie does not cause a downstream query on one feeder to traverse into its
neighbouring feeder.

For example:

``` text
FDR-1201 ---- normally-open tie ---- FDR-1202
```

The physical graph contains the connection, but the normal feeder trees remain separate.

This prevents:

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
```

from unexpectedly returning an entire neighbouring feeder.

### `CONNECTED TO`

`CONNECTED TO` is about physical adjacency rather than normal feeder traversal.

It can therefore cross feeder boundaries.

### Current state is separate

A switch's present state does not redefine what `DOWNSTREAM OF` means.

For example:

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
```

asks what is physically below the recloser according to the normal topology.

To ask what is currently without power:

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
WHERE NOT energized
```

or simply:

``` text
FIND loads WHERE NOT energized
```

The latter is evaluated against the current switch configuration and source reachability.

### Loops and normally-open switches

GridQL uses the normal position of switches when determining normal topology.

A normally open switch can therefore define the open point of a radial circuit without causing the
normal topology to be treated as a closed loop.

The current operating state is used separately when calculating energisation.

------------------------------------------------------------------------

## Walking order and `hops`

Topology results are returned in walking order by default:

- nearest to the target first
- then progressively farther away

`ORDER BY` can override this.

`hops` is the distance from the query's topology target.

`depth` is the distance from the feeder head.

For example:

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
SELECT mRID, type, hops
```

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
WHERE hops <= 1
```

``` text
FIND devices DOWNSTREAM OF "REC-1201-01"
SELECT MAX(hops), COUNT(*)
```

``` text
FIND devices WHERE depth = 0
```

`hops` only has meaning when a topology relation supplies a target. A query that refers to `hops`
without such a relation is refused.

------------------------------------------------------------------------

## Protection zones

Protective devices are modelled as:

- breakers
- reclosers
- fuses
- sectionalizers

`PROTECTED BY` identifies the protection zone under a protective device, stopping at the next
protective-device boundary.

For example:

``` text
FIND devices PROTECTED BY "SEC-1201-01"
SELECT mRID, type, hops
```

Each device also has a `protected_by` attribute representing its nearest modelled protective device.

That allows questions such as:

``` text
FIND loads
SELECT protected_by, COUNT(*), SUM(kw)
GROUP BY protected_by
ORDER BY SUM(kw) DESC
```

A plain switch does not bound a protection zone.

A sectionalizer is treated as protective for purposes of the modelled zone abstraction, although it
does not itself clear a fault.

Again, this is a network-model abstraction rather than a substitute for protection coordination.

------------------------------------------------------------------------

## Conditions

Supported operators include:

``` text
=
!=
>
>=
<
<=
IN (...)
CONTAINS
LIKE
```

Logical operators:

``` text
AND
OR
NOT
```

Precedence is:

1. `NOT`
2. `AND`
3. `OR`

Examples:

``` text
FIND devices WHERE name CONTAINS "Maple"
```

``` text
FIND transformers WHERE name LIKE "Pad 40%"
```

``` text
FIND fuses WHERE mrid LIKE "FU-120_-01"
```

A bare attribute is a truth test:

``` text
FIND devices WHERE energized
```

``` text
FIND devices WHERE NOT energized
```

A bare word on the right-hand side is interpreted as an attribute when the type has an attribute
with that name; otherwise it is treated as a value.

Therefore:

``` text
FIND switches WHERE state != normal_state
```

compares two attributes, while:

``` text
FIND switches WHERE state = OPEN
```

uses `OPEN` as a value.

Quoting forces value interpretation:

``` text
FIND switches WHERE state = "OPEN"
```

Attribute names that the type cannot have are errors rather than empty results.

Missing attribute values do not match comparisons.

------------------------------------------------------------------------

## SELECT

A query without `SELECT` returns the columns appropriate to its type, plus columns used for
filtering or ordering when useful to understand the result.

For explicit output:

``` text
FIND transformers
SELECT name, mRID, kva, primary_voltage, secondary_voltage
```

`SELECT *` returns the available result fields.

Selecting an attribute that exists conceptually but has no value for a particular object produces an
empty cell rather than removing the row.

Selecting an attribute that the type cannot have is an error.

------------------------------------------------------------------------

## Aggregates, grouping, and ordering

Supported aggregates:

``` text
COUNT
SUM
AVG
MIN
MAX
```

Example:

``` text
FIND loads
DOWNSTREAM OF "REC-1201-01"
SELECT COUNT(*), SUM(kw), SUM(kvar)
```

Grouping:

``` text
FIND devices
GROUP BY type
```

``` text
FIND loads
SELECT feeder, COUNT(*), SUM(kw)
GROUP BY feeder
ORDER BY SUM(kw) DESC
```

A selected column must either be grouped or aggregated.

`COUNT(*)` counts rows.

`COUNT(attribute)` counts rows that contain that attribute.

Aggregates over an empty result still return a result row: `COUNT` is zero and other aggregates are
empty.

When sorting, equipment without a value for the sort column sorts last in both directions.

------------------------------------------------------------------------

## Units

Quantities may include units:

``` text
FIND transformers WHERE kva >= 500
```

``` text
FIND transformers WHERE kva >= 500kVA
```

``` text
FIND transformers WHERE kva >= 0.5MVA
```

These express the same comparison.

Dimensions are enforced. For example:

``` text
kva >= 500kW
```

is an error.

Canonical units include:

| Quantity | Canonical unit |
| --- | --- |
| Voltage | kV |
| Transformer rating | kVA |
| Load | kW / kVAr |
| Ampacity | A |
| Length | ft |

------------------------------------------------------------------------

## Saved `.gridql` queries

A `.gridql` file can contain one or more statements:

``` sql
-- Large service transformers on Cedar Hill 1201.

FIND transformers
DOWNSTREAM OF "FDR-1201"
WHERE kva >= 500
SELECT
    name,
    mRID,
    kva,
    primary_voltage,
    secondary_voltage
RETURN table
```

Saved queries can be version-controlled, reviewed, shared, and run against different datasets.

``` bash
gridql run queries/feeder_analysis.gridql
gridql run queries/feeder_summary.gridql --format json
gridql run queries/feeder_analysis.gridql --explain
```

------------------------------------------------------------------------

## Parameters

Parameters make saved queries reusable.

``` sql
PARAM feeder  = "FDR-1201"
PARAM min_kva = 0

FIND transformers
FED BY $feeder
WHERE kva >= $min_kva
SELECT mRID, name, kva
ORDER BY kva DESC
```

Then:

``` bash
gridql run queries/feeder_report.gridql
gridql run queries/feeder_report.gridql --feeder FDR-1202
gridql run queries/feeder_report.gridql --min_kva 0.5MVA
```

Every declared parameter becomes a command-line option.

`--param name=value` is available when a parameter name conflicts with a GridQL option.

A parameter with no default is required.

An undeclared `$name` is a syntax error.

Parameters are always values. They cannot dynamically change the meaning of an attribute.

For example:

``` text
WHERE state = $position
```

with:

``` bash
--position normal_state
```

compares `state` with the text value `normal_state`; it does not resolve `normal_state` as an
attribute.

This distinction keeps parameter substitution predictable.

------------------------------------------------------------------------

## Project configuration

A utility's GridQL work can be treated as a repository containing:

- a dataset
- saved queries
- optional mappings
- default query parameters

`project.gridqlconfig` records those defaults.

Example:

``` toml
name    = "Oakdale District"
db      = "grid.sqlite"
queries = "queries"

# mapping = "gis-export.toml"
# postgres = "service=gis"

[params]
feeder = "FDR-104"
```

GridQL searches upward from the working directory for this file, similar to how Git finds repository
configuration.

Paths resolve relative to the configuration file.

Explicit command-line options override configuration.

``` bash
gridql config
gridql 'FIND reclosers'
gridql run feeder_report
```

Use:

``` bash
--config PATH
```

to choose a different configuration file, or:

``` bash
--no-config
```

to ignore project configuration.

This makes the same saved query usable interactively and in CI.

------------------------------------------------------------------------

## Output

Supported output formats are:

``` text
table
json
csv
cim
```

A query can specify its own format:

``` text
RETURN json
```

The command line can override it:

``` bash
--format json
```

When several statements are executed from a file, each result is identified in table-style output.

JSON output for a multi-statement file is a single JSON document containing the results of all
statements.

CIM output is described below.

------------------------------------------------------------------------

## Colour

Interactive terminal output highlights conditions that deserve attention:

- equipment not energised
- switches or capacitors out of normal position

Colour does not assign a universal meaning to `OPEN` or `CLOSED`.

This is intentional: utility one-line conventions differ, while "out of normal position" has a
consistent operational meaning.

Colour is disabled for JSON, CSV, and CIM output.

------------------------------------------------------------------------

## Building a network

Networks can be built through the semantic model API:

``` python
from gridql import Network

network = Network()

network.add_substation(
    "SUB-001",
    name="Oakdale",
    voltage="13.8kV",
)

feeder = network.add_feeder(
    "FDR-104",
    voltage="13.8kV",
    substation="SUB-001",
)

feeder.add_breaker("BRK-001")
feeder.add_recloser("REC-001")
feeder.add_switch("SW-001")

feeder.add_transformer(
    "XFMR-001",
    after="REC-001",
    kva=500,
    secondary_voltage="0.48kV",
)

feeder.add_load("LOAD-001", kw=310)
```

Each `add_*` operation can connect the new device behind the previous device.

`after=` can branch from an earlier device.

A network restored from persistence does not retain a "last added" device, so adding a new device
without an explicit `after=` where one is required raises an error rather than creating an
unconnected device.

------------------------------------------------------------------------

## Validation

Validation is a first-class part of GridQL because topology queries depend on the model being
internally coherent.

``` bash
gridql validate
gridql validate --db grid.sqlite
gridql validate --db grid.sqlite --strict
```

Example:

``` text
1 error, 1 warning

error   invalid-state
        SW-1: state is 'AJAR', expected OPEN or CLOSED

warning loop
        FDR-1: SW-A and SW-B close a loop; the feeder is not radial
```

Errors indicate that the model is broken in a way that can make answers incorrect.

Warnings indicate that the model is usable but a result may not behave as expected.

Examples include:

- missing containers
- invalid switch states
- self-connections
- missing feeder heads
- loops in a circuit expected to be radial
- islands unreachable from the feeder head
- equipment without feeder membership
- equipment without connections
- hard links between feeders that are not represented as ties

Warnings cause a non-zero result only when `--strict` is used.

Validation can also be called from Python:

``` python
from gridql import build_sample_network, validate

report = validate(build_sample_network())

print(report.ok, report.counts())

for finding in report:
    print(finding.severity, finding.code, finding.message)
```

------------------------------------------------------------------------

## Loading utility data

GridQL is designed around the reality that utility exports vary considerably.

A simple CSV dataset can look like:

``` text
gis-export/
  devices.csv
  connections.csv
  feeders.csv
  substations.csv
```

`devices.csv` contains the equipment model.

`connections.csv` is optional.

Feeders and substations can be supplied explicitly or inferred from device references when the data
makes that unambiguous.

Query directly:

``` bash
gridql --csv ./gis-export \
    'FIND transformers WHERE kva >= 500'
```

Or persist the model:

``` bash
gridql import-csv ./gis-export --db grid.sqlite
```

Export it again:

``` bash
gridql export-csv ./out --db grid.sqlite
```

------------------------------------------------------------------------

## Mapping a utility's schema

Real utility exports rarely use exactly the field names GridQL expects.

One utility may call a feeder:

``` text
FEEDER_ID
```

while another uses:

``` text
CIRCUIT_NO
```

A mapping file defines what those fields mean.

Example:

``` toml
[feeders]

file       = "CIRCUIT.csv"
mrid       = "FDR-{CIRCUIT_NO}"
name       = "CIRCUIT_DESC"
substation = "SUB-{STATION_NO}"
voltage    = { column = "NOM_VOLTS", unit = "V" }

[[devices]]

file   = "SWITCH.csv"
mrid   = "FACILITY_ID"
feeder = "FDR-{CIRCUIT_NO}"

type  = { column = "SW_TYPE", values = { BKR = "breaker", RCL = "recloser", LBS = "switch" } }
state = { column = "POSITION", values = { O = "OPEN", C = "CLOSED" } }

[[devices]]

file = "TRANSFORMER.csv"
type = { value = "transformer" }
mrid = "FACILITY_ID"
kva  = "KVA_RATING"

[connections]

file        = "CONNECTIVITY.csv"
from_device = "FROM_FACILITY"
to_device   = "TO_FACILITY"
```

Then:

``` bash
gridql import-csv ./gis-export --mapping gis-export.toml
gridql --csv ./gis-export --mapping gis-export.toml \
    'FIND reclosers'
```

A mapping makes the interpretation explicit.

Unmapped columns can remain available as utility-specific attributes, while mapped semantic fields
are not guessed from unrelated columns.

Unexpected values and mapping mistakes are reported rather than silently reshaped.

------------------------------------------------------------------------

## Connectivity nodes

Many GIS and ADMS exports do not explicitly list every device-to-device connection.

Instead, devices identify the nodes at their ends:

``` text
mrid,type,feeder,from_node,to_node
BRK,breaker,F1,N0,N1
L1,line,F1,N1,N2
L2,line,F1,N2,N3
L3,line,F1,N2,N4
LD1,load,F1,N3,
```

Here `L1`, `L2`, and `L3` meet at `N2`.

GridQL retains those nodes rather than discarding them after constructing connections.

That matters because the same set of neighbouring devices can represent either a legitimate junction
or a more complicated connectivity condition.

Node-based input and explicit `connections.csv` input can be used independently or together.

------------------------------------------------------------------------

## Handling imperfect exports

Utility exports are not always clean.

GridQL reports bad rows rather than silently discarding them:

``` text
loaded 6 devices, 1 feeders, 0 substations, 4 connections

4 row(s) skipped or incomplete:

  devices.csv:7: voltage: could not read 'not-a-voltage' as a quantity
  devices.csv:8: no mRID
  connections.csv:6: unknown device 'GHOST-99'
```

A query against CSV reports the problem on stderr while keeping query output suitable for downstream
use.

This allows an engineer to inspect both:

1. the answer GridQL could establish, and
2. the data-quality problems that affected the load.

------------------------------------------------------------------------

## Reading from Postgres

GridQL can read utility databases through the same mapping model.

A mapping can use:

- a table
- a schema-qualified table
- a query for joins or filters

For example:

``` toml
[[devices]]

table = "gis.switch"
mrid  = "facility_id"

type  = { column = "sw_type", values = { BKR = "breaker", RCL = "recloser", LBS = "switch" } }
state = { column = "position", values = { O = "OPEN", C = "CLOSED" } }

[[devices]]

query = """
    SELECT
        sp.*,
        count(p.premise_no) AS customer_count
    FROM gis.service_point sp
    LEFT JOIN cis.premise p
        ON p.service_point = sp.facility_id
    GROUP BY sp.facility_id
"""

type = { value = "load" }
mrid = "facility_id"
```

Install the optional Postgres support:

``` bash
pip install 'gridql[postgres]'
```

Then:

``` bash
gridql --postgres postgresql://gis@gis-db/utility \
    --mapping gis.toml \
    'FIND fuses WHERE state = OPEN'
```

Or create a snapshot:

``` bash
gridql import-postgres postgresql://gis@gis-db/utility \
    --mapping gis.toml \
    --db grid.sqlite
```

### Read-only behaviour

Postgres imports use a read-only, repeatable-read transaction.

GridQL does not modify the source database.

The transaction also gives the import a consistent view of the source data.

### Live versus snapshot

GridQL supports two useful operating modes.

#### Snapshot

``` text
Postgres
    │
    ▼
SQLite
    │
    ▼
GridQL semantic model
```

A snapshot is useful for repeatable reports, batch work, testing, and CI.

#### Live

``` text
Postgres
    │
    ▼
GridQL semantic model
```

A live query reads the mapped source data for the current run.

For example:

``` bash
gridql 'FIND fuses WHERE state = OPEN'
```

can use the project's configured snapshot, while:

``` bash
gridql --live 'FIND fuses WHERE state = OPEN'
```

can query the configured Postgres source.

The current implementation materialises the semantic network for a live query rather than
translating every GridQL operation into a database-native SQL query. This keeps topology semantics
in the GridQL model, at the cost of reading the mapped network for live operations.

For repeated reports, a SQLite snapshot avoids rebuilding the model for every invocation.

------------------------------------------------------------------------

## Persistence

GridQL can persist a network in SQLite:

``` bash
gridql init grid.sqlite
gridql --db grid.sqlite 'FIND reclosers'
```

Python:

``` python
from gridql import build_sample_network, load_network, save_network

save_network(build_sample_network(), "grid.sqlite")

network = load_network("grid.sqlite")
```

The persisted model retains:

- equipment
- connectivity
- feeder heads
- semantic attributes
- utility-specific attributes

Connectivity is stored as an undirected graph because physical connectivity and current electrical
direction are separate concepts.

The in-memory model performs topology traversal.

SQLite provides durability; it is not itself the topology engine.

------------------------------------------------------------------------

## Refreshing a database

Imports replace a database rather than merging into it.

``` bash
gridql import-csv ./gis-export --db grid.sqlite --force
```

The replacement occurs transactionally, so a failed save leaves the previous database intact.

Before replacing an existing database, GridQL checks for conditions such as:

- validation errors
- a significant reduction in device count
- a feeder disappearing from the new data

For example:

``` text
error: not saved: grid.sqlite was left as it was, because

  - it would replace 11 devices with 2, 82% fewer

Check the new data, or pass --skip-checks to replace it anyway.
```

These checks are intended to catch incomplete or accidentally filtered exports before they replace a
known-good snapshot.

------------------------------------------------------------------------

## Semantic model and storage

The semantic model is deliberately separate from storage.

The SQLite schema uses class-table inheritance:

- common equipment is stored in `devices`
- specialised attributes are stored in extension tables
- feeders and substations have their own tables
- connectivity is stored separately
- utility-specific fields can be retained in `extras`

mRIDs are the primary identifiers.

The model does not require a separate surrogate identifier.

The important architectural boundary is:

``` text
Data source
    ↓
Importer / mapper
    ↓
Semantic model
    ↓
Query interpreter
```

The query engine does not need to know whether an object came from CSV, Postgres, SQLite, CIM,
OpenDSS, or Python code.

This also leaves room for a future storage implementation to change without changing the GridQL
language.

------------------------------------------------------------------------

## CIM import and export

GridQL can use CIM as both an input and output representation.

Export a selected network slice:

``` bash
gridql export-cim feeder.xml \
    --query 'FIND devices FED BY "FDR-1201"'
```

Or directly from a query:

``` text
FIND devices FED BY "FDR-1201"
RETURN cim
```

A CIM export includes the supporting objects required by the selected network slice, including
relevant feeder, substation, voltage, and connectivity information.

Equipment is represented using appropriate CIM classes.

Values are converted to CIM's expected units.

Connectivity is represented using CIM terminals and connectivity nodes.

GridQL also retains a small set of model-specific information in its own namespace where CIM does
not directly represent the semantic-model concept.

The goal is lossless GridQL round-tripping while remaining compatible with standards-based CIM
consumers.

### CIM import

The importer understands multiple CIM namespace releases and common distribution-system
specialisations.

Equipment that GridQL does not have a dedicated class for can still be retained as generic devices
with the source CIM class preserved.

If GridQL cannot establish the feeder head needed for topology traversal, it reports that condition
rather than inventing one.

Phasing and transformer information can be derived from the CIM structures used by distribution
models.

------------------------------------------------------------------------

## OpenDSS

GridQL can import OpenDSS distribution models:

``` bash
gridql import-dss Master.dss --db grid.sqlite
```

OpenDSS models are translated into the same GridQL semantic model used by the other import paths.

This allows the same topology-oriented queries to operate against OpenDSS models without changing
the query language.

------------------------------------------------------------------------

## Example data

The repository includes example data representing two 12.47 kV feeders from the Cedar Hill
substation.

The example includes:

- a mid-line recloser
- a switched capacitor bank
- a sectionalizing switch
- a sectionalized branch
- fused single-phase laterals
- underground commercial services
- a normally open tie between feeders
- a blown lateral fuse

Example queries:

``` bash
gridql --csv examples/csv \
    'FIND devices WHERE state != normal_state'
```

``` bash
gridql --csv examples/csv \
    'FIND loads WHERE NOT energized SELECT SUM(customer_count)'
```

``` bash
gridql --csv examples/csv \
    'FIND devices UPSTREAM OF "SP-40335"'
```

The repository also contains mapped and Postgres example datasets.

[`examples/storm-morning/`](examples/storm-morning/) is a small, self-contained project that follows
one outage from the first reports to restoration through a tie. It has its own project file, before
and after snapshots, and three saved queries.

------------------------------------------------------------------------

## Testing and reference models

The repository includes tests covering the semantic model, query language, topology, persistence,
import/export paths, mappings, validation, and example data.

The distribution-model import path is also tested against IEEE feeder models, including the IEEE
13-, 123-, and 8500-node feeders.

These models are useful reference cases because they exercise non-trivial distribution topology
rather than only simple radial examples.

------------------------------------------------------------------------

## Architecture

GridQL is implemented in Python 3.11+.

The core package has no required third-party dependencies.

Postgres support adds the optional `psycopg` dependency.

The important architectural separation is:

``` text
                 ┌─────────────────────────┐
                 │        GridQL CLI       │
                 └────────────┬────────────┘
                              │
                 ┌────────────▼────────────┐
                 │      Query language     │
                 │ lexer / parser /        │
                 │ interpreter             │
                 └────────────┬────────────┘
                              │
                 ┌────────────▼────────────┐
                 │     Semantic model      │
                 │ equipment / topology /  │
                 │ state / derived fields  │
                 └────────────┬────────────┘
                              │
            ┌─────────────────┼──────────────────┐
            │                 │                  │
            ▼                 ▼                  ▼
         SQLite            CSV/GIS           Postgres
            │                 │                  │
            └─────────────────┼──────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                   CIM                OpenDSS
```

The query language talks to the semantic model rather than directly to storage.

This is intentional.

It means the same semantic rules govern:

- CSV queries
- SQLite queries
- Postgres queries
- CIM imports
- OpenDSS imports
- networks built directly in Python

------------------------------------------------------------------------

## Design principles

GridQL follows several principles.

### 1. Electrical meaning belongs in the semantic model

The query language should express a question.

The semantic model should know how that question is answered.

### 2. Storage format should not determine query semantics

A utility should not need a different query because its feeder data came from Postgres instead of a
CSV export.

### 3. Refuse rather than guess

Ambiguous electrical meaning is more dangerous than an explicit error.

### 4. Separate physical connectivity from operating state

A device can be physically connected while currently isolated by an open switch.

### 5. Separate normal topology from current energisation

`DOWNSTREAM OF` and `energized` are related but not interchangeable.

### 6. Preserve source information

Importers should avoid throwing away utility-specific attributes merely because GridQL does not have
a dedicated field for them.

### 7. Make saved queries reviewable

A `.gridql` file should be understandable by another engineer without reconstructing a SQL schema.

### 8. Keep the language independent of the storage engine

Topology traversal currently happens in memory. That is an implementation choice, not a requirement
of the GridQL language.

------------------------------------------------------------------------

## Performance and scale

GridQL currently favours a simple semantic architecture:

1. load the network
2. build the in-memory graph
3. execute topology operations against that graph

This makes topology traversal straightforward and avoids pushing electrical semantics into
storage-specific recursive queries.

The tradeoff is that the current implementation loads the semantic network into memory.

For the intended distribution-network use cases this provides a simple and predictable execution
model.

It also leaves an architectural path for future storage or execution optimisations without changing
the language.

In particular, the language does not require that future versions continue to materialise every
object for every query.

------------------------------------------------------------------------

## Command reference

Common commands include:

``` text
gridql
gridql run
gridql config
gridql init
gridql validate
gridql import-csv
gridql export-csv
gridql import-postgres
gridql import-cim
gridql export-cim
gridql import-dss
```

Run:

``` bash
gridql --help
```

for the current command-line options.

------------------------------------------------------------------------

## Repository layout

A typical GridQL repository contains:

``` text
gridql/
examples/
queries/
tests/
project.gridqlconfig
GETTING_STARTED.md
LICENSE
README.md
```

The repository's example queries demonstrate the language against the included example networks.

------------------------------------------------------------------------

## Getting started

If you are new to GridQL, start with:

1. the quickstart above
2. [`GETTING_STARTED.md`](GETTING_STARTED.md)
3. the examples in [`queries/`](queries/)
4. the [core concepts](#core-concepts) and [topology](#topology) sections above

The README intentionally introduces the concepts and workflow rather than serving as the complete
language specification.

For command-line options, run `gridql --help` or `gridql <command> --help`; in the REPL, `.help`
lists the syntax and `.types` lists every type and the CIM class it maps to.

------------------------------------------------------------------------

## Current limitations

GridQL is intentionally focused on network semantics rather than being a complete utility
engineering platform.

Current limitations include:

- no power-flow solver
- no protection-coordination calculation
- no SCADA integration
- no GIS editing environment
- no device geometry / GeoJSON output
- no JSON-native network model format
- database connectors currently focus on Postgres rather than direct Oracle or SQL Server access

There is no separate `EXPORT CIM` statement.
[`queries/export_feeder.gridql`](queries/export_feeder.gridql) exports a feeder as CIM with clauses
that already exist: a `PARAM`, `FED BY`, and `RETURN cim`.

------------------------------------------------------------------------

## Licence

GridQL is free software under the **GNU Affero General Public License, version 3 or later**.

See [`LICENSE`](LICENSE).

Copyright © 2026 Index Labs, LLC.

**Using it inside your own organisation carries no obligations.** Download it, script against it,
modify it, run it on your own grid data; the licence asks nothing of you. Copyleft applies when you
pass copies on to others, or when you offer a *modified* GridQL to users over a network, in which
case those users must be offered your modified source. That last clause is the point of the AGPL: it
keeps a modified GridQL from being resold as a closed hosted service.

**Your data is not covered.** The licence governs this software, not what you do with it. Your
network model, the CIM and CSV that GridQL reads and writes, query results, and the `.gridql` files
you write are yours. They are the program's input and output, not derivative works of it, the way a
SQL script is not a derivative of the database engine.

A commercial licence is available for organisations that cannot use the copyleft terms of the AGPL;
contact Index Labs, LLC.

------------------------------------------------------------------------

## Status

GridQL is an actively developed domain-specific query and semantic-model project for electric
utility network data.

The goal is not SQL with electrical words bolted on.

It is to make the underlying network semantics explicit enough that an engineer can ask a question,
understand what GridQL means by that question, and trust that GridQL will refuse to manufacture an
answer when the model cannot support one.
