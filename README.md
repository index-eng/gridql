# GridQL

A domain-specific query language for electric utility data and power-system models.

GridQL lets engineers query the grid with the concepts they already use — feeders, substations,
transformers, switches, reclosers, phases, voltage levels and electrical topology — instead of
learning a database schema.

```
$ gridql 'FIND transformers DOWNSTREAM OF "REC-001" WHERE kva >= 500'
mrid      name         type         feeder   phases  kva  primary_voltage  secondary_voltage
--------  -----------  -----------  -------  ------  ---  ---------------  -----------------
XFMR-001  Elm St Bank  transformer  FDR-104  ABC     500  13.8             0.48

1 row
```

This is the first milestone: the semantic model and the interpreter, with no storage layer and no
CIM serialisation yet. Pure Python 3.11+, no dependencies.

New here? [**Getting started**](GETTING_STARTED.md) walks through installing it and what it can do.

## Quickstart

```bash
python3 -m gridql.cli                                  # REPL against the sample feeder
python3 -m gridql.cli 'FIND reclosers'                 # one-shot query
python3 -m gridql.cli --format json 'FIND loads'       # table (default), json or csv
python3 -m gridql.cli --explain 'FIND devices DOWNSTREAM OF "REC-001"'
python3 -m gridql.cli run queries/feeder_analysis.gridql   # run a saved query
python3 -m gridql.cli init grid.sqlite                 # create a database
python3 -m gridql.cli --db grid.sqlite 'FIND feeders'  # query it
python3 -m gridql.cli export-cim feeder.xml --query 'FIND devices FED BY "FDR-104"'
python3 -m gridql.cli import-cim feeder.xml
python3 -m unittest discover -s tests                  # the test suite
```

Installing the package (`pip install -e .`) puts a `gridql` command on your path that does the same
thing.

From Python:

```python
from gridql import build_sample_network, execute, render

network = build_sample_network()
result = execute(network, 'FIND switches WHERE state != normal_state')

print(result.mrids)            # ['SW-002']
print(render(result, 'json'))
```

## The language

```
FIND <type>
  [ DOWNSTREAM OF <device> | UPSTREAM OF <device> | CONNECTED TO <device> | FED BY <feeder> ]*
  [ WHERE <condition> ]
  [ SELECT <column>, ... ]
  [ RETURN table | json | csv | cim ]
```

Clauses come in that order. Statements are separated by `;`.

Keywords are case-insensitive. `--` and `#` start a comment.

### Types

`devices`, `switches`, `reclosers`, `breakers`, `fuses`, `sectionalizers`, `transformers`, `lines`,
`loads`, `capacitors`, `feeders`, `substations`. Singular and common aliases (`xfmrs`, `caps`,
`conductors`, `subs`, `circuits`) all work.

`switches` is a supertype: it matches reclosers, breakers, fuses and ties too, the same way CIM
specialises `Switch`. `devices` matches all conducting equipment but not the feeder and substation
containers.

### Topology

| Relation | Meaning |
| --- | --- |
| `DOWNSTREAM OF "X"` | everything electrically below X, X excluded |
| `UPSTREAM OF "X"` | everything between X and the feeder head, X excluded |
| `CONNECTED TO "X"` | immediate neighbours of X |
| `FED BY "X"` | everything X supplies, X included; for a feeder or substation, all of its devices |

Traversal is **topological — it ignores switch state**, so `DOWNSTREAM OF "REC-001"` answers "what
is physically below this recloser", which is the question being asked when planning work. Whether
something is currently *energised* is a separate question, answered by the derived `energized`
attribute:

```
FIND devices DOWNSTREAM OF "REC-001" WHERE NOT energized
```

Operating a switch updates that immediately — `network.get("SW-001").state = "OPEN"` and the next
query sees the new answer. The derived topology is cached, but the cache is keyed to the state it
was built from, so it cannot outlive a change. If you mutate the model in some way the network
cannot observe, call `network.invalidate()`.

Several relations may be stacked, and they intersect.

### Conditions

Operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN (...)` and `CONTAINS`, combined with `AND`, `OR`,
`NOT` and parentheses. `NOT` binds tightest, then `AND`, then `OR`.

A bare attribute is a truth test, so `WHERE energized` and `WHERE NOT energized` read naturally.

**A bare word on the right-hand side is an attribute if the type has one by that name, and a value
otherwise.** That is what makes the switching query work:

```
FIND switches WHERE state != normal_state     -- switches out of normal position
FIND switches WHERE state = OPEN              -- OPEN is not an attribute, so it is a value
FIND switches WHERE state = "OPEN"            -- quoting always forces the value reading
```

String comparisons are case-insensitive.

### SELECT

By default a query returns the columns that suit the type. `SELECT` picks them instead, in the
order given, and column headers keep the spelling you wrote — so `SELECT mRID` produces an `mRID`
header even though attribute lookup is case-insensitive. `SELECT *` is the default set.

```
FIND transformers SELECT name, mRID, kva, primary_voltage, secondary_voltage
```

A selected attribute the object does not have comes back empty rather than being dropped, so
`FIND devices SELECT mrid, kva` lists every device with ratings only where they exist. Selecting
something no such type could have — `SELECT kvaa` — is an error, with a suggestion.

### RETURN

`RETURN table|json|csv|cim` records the output format in the query itself, so a saved query renders
the way its author intended. An explicit `--format` on the command line overrides it.

`cim` is the interesting one: the result is emitted as a CIM RDF/XML document, so a query is how
you carve a slice of the system out as a standards-based exchange file. See
[CIM import and export](#cim-import-and-export).

### Units

Numbers may carry a unit and are converted to the attribute's own unit; a bare number is assumed to
already be in it. These are all the same query:

```
FIND transformers WHERE kva >= 500
FIND transformers WHERE kva >= 500kVA
FIND transformers WHERE kva >= 0.5MVA
```

Dimensions are enforced, so `kva >= 500kW` is an error rather than a wrong answer. Canonical units:
voltages in kV, transformer ratings in kVA, load in kW/kVAr, ampacity in A, length in ft.

## .gridql files

A query worth writing twice is worth keeping. A `.gridql` file holds one or more statements,
separated by `;`, with `--` or `#` comments:

```sql
-- Large service transformers on the Oakdale circuit.
FIND transformers
DOWNSTREAM OF "FDR-104"
WHERE kva >= 500
SELECT
    name,
    mRID,
    kva,
    primary_voltage,
    secondary_voltage
RETURN table
```

```bash
gridql run queries/feeder_analysis.gridql
gridql run queries/feeder_summary.gridql --format json
gridql run queries/feeder_analysis.gridql --explain
```

These are artifacts an engineer can version-control, review, share and run in CI against different
datasets — not disposable database queries. Four examples live in [`queries/`](queries/).

**Output.** Each statement renders in its own format: its `RETURN` clause, else `--format`, else a
table. When a file has several statements, each result is preceded by a `-- n. <query>` comment so
the output stays readable. The exception is `--format json`, which emits a *single* JSON document —
an array with one `{query, type, rows}` entry per statement — so a CI job can parse the whole run
at once.

From Python:

```python
from gridql import build_sample_network, run_file

for result in run_file(build_sample_network(), "queries/feeder_summary.gridql"):
    print(result.type_name, result.mrids)
```

## Building a network

Networks are built through the semantic model's own API — the same API a SQLite or CIM loader will
populate. Each `add_*` connects the new device in series behind the previous one; `after=` branches
off an earlier device.

```python
from gridql import Network

network = Network()
network.add_substation("SUB-001", name="Oakdale", voltage="13.8kV")

feeder = network.add_feeder("FDR-104", voltage="13.8kV", substation="SUB-001")
feeder.add_breaker("BRK-001")
feeder.add_recloser("REC-001")
feeder.add_switch("SW-001")
feeder.add_transformer("XFMR-001", after="REC-001", kva=500, secondary_voltage="0.48kV")
feeder.add_load("LOAD-001", kw=310)
```

See `gridql/data/sample.py` for the full sample feeder, FDR-104.

## Persistence

A network can be stored in SQLite and loaded back identically — same objects, same connectivity,
same feeder heads, so topology is restored rather than re-guessed.

```bash
gridql init grid.sqlite                       # schema + the sample network
gridql --db grid.sqlite 'FIND reclosers'
gridql run queries/open_devices.gridql --db grid.sqlite
```

With no `--db`, everything runs against the bundled sample feeder. `gridql init` refuses to
overwrite an existing file unless you pass `--force`, and `--empty` creates the schema alone.

```python
from gridql import build_sample_network, load_network, save_network

save_network(build_sample_network(), "grid.sqlite")
network = load_network("grid.sqlite")          # the NetworkLoader
```

**Schema.** Class-table inheritance, as sketched in `idea.md`: every piece of equipment has a row
in `devices`, and types with extra attributes have a matching row in an extension table keyed by
the same mRID (`transformers`, `switches`, `lines`, `loads`, `capacitors`), plus `substations`,
`feeders` and `connections`. That mirrors how CIM specialises `ConductingEquipment`. mRIDs are the
primary keys — a surrogate id would earn nothing here. Connectivity is undirected and each edge is
stored once with the lower mRID first, enforced by a `CHECK`, so a circuit cannot accumulate
mirrored duplicates. Attributes outside the schema ride along in a JSON `extras` column and stay
queryable. See [`gridql/storage/schema.sql`](gridql/storage/schema.sql).

**Loading reads everything once** and builds the in-memory graph. That is the trade the design
calls for: SQLite provides durability while traversal stays in memory, so `DOWNSTREAM OF` is a
graph walk rather than a recursive CTE. If scale ever demands otherwise, the loader changes and
the language does not.

A feeder restored from storage knows its head but has no "last added" device, so calling
`feeder.add_switch("SW-NEW")` without `after=` raises rather than quietly leaving the new device
unconnected.

## CIM import and export

GridQL is a translation layer as much as a query language. Pull out the part of the system you
care about with a query, and get a standards-based CIM document back:

```bash
gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-104"'
gridql export-cim - --query 'FIND transformers WHERE kva >= 500'   # to stdout
gridql import-cim vendor-export.xml --db grid.sqlite
```

```
FIND devices FED BY "FDR-104" RETURN cim
```

A slice brings what it needs with it: the feeder and substation that contain it, the
`BaseVoltage` objects its equipment refers to, and the connectivity *among the selected
equipment*. The result is a document that stands on its own.

**What gets written.** Equipment uses its CIM class (`reclosers` → `ProtectedSwitch`,
`transformers` → `PowerTransformer`, `loads` → `EnergyConsumer`), and values are converted to CIM's
SI units — line length in metres, transformer ratings in VA on `PowerTransformerEnd` objects,
load in W and VAr, voltages as `BaseVoltage` in volts.

**Connectivity.** CIM never joins equipment directly: a device has `Terminal`s, and terminals meet
at a `ConnectivityNode`. The model stores plain edges, so export synthesises one node per edge with
two terminals, and import collapses them back. Importing a real bus — a node with more than two
terminals — produces edges between every pair of devices on it, which keeps both `CONNECTED TO`
and feeder traversal correct even though the node object itself is not preserved. The import report
says when this happened.

**Identifiers** are derived from mRIDs rather than freshly minted UUIDs, so exporting the same
network twice gives byte-identical output and a diff means the network really changed. An mRID that
is not a valid XML name is adjusted for `rdf:ID` and preserved exactly in
`cim:IdentifiedObject.mRID`.

**Extensions.** A few things the model needs have nowhere to live in CIM — which device heads a
feeder, a phase string, the `extras` bag. Those are written in a private `gridql:` namespace, so a
round trip is lossless while a standards-only consumer can ignore them. A full export and reimport
of a network reproduces it exactly, which the test suite asserts object by object.

**Reading other people's CIM.** The importer understands the specialisations other tools emit
(`LoadBreakSwitch`, `Disconnector`, `ConformLoad`, …), reports any class it does not model rather
than dropping it silently, and — when no head is recorded — infers the feeder head from the single
breaker on the feeder, saying so. If it cannot, it tells you `DOWNSTREAM OF` will come back empty
for that feeder rather than inventing an answer.

```python
from gridql import read_cim

document = read_cim("vendor-export.xml")
print(document.report.summary())
network = document.network
```

## Architecture

```
            GridQL            gridql/lang     -- lexer, parser, AST, evaluator
               |
      Semantic Model API      gridql/model    -- devices, feeders, substations
               |
         Graph Model          gridql/model/network.py -- connectivity and traversal
               |
        SQLite storage        gridql/storage  -- schema and the NetworkLoader

      CIM RDF/XML  <-->  gridql/cim  -- translation to and from the standard
```

The language only ever calls the semantic model's public API, and the model knows nothing about
where its objects came from. That boundary is the point: swapping in SQLite, CIM or a GIS export
changes the loader, not the language.

Every class records the CIM class it maps to (`reclosers` → `ProtectedSwitch`, `transformers` →
`PowerTransformer`, `loads` → `EnergyConsumer`, …), which is what CIM export is built on.

## Not built yet

GeoJSON output, CSV/JSON input loaders, and the editor — step 6 of the design in `idea.md`.

The `EXPORT CIM FROM feeder "FDR-104" INCLUDING ...` statement from the design is not implemented
as its own syntax; `RETURN cim` and `export-cim --query` express the same thing through clauses
that already exist. Parameterized queries and `project.gridqlconfig` are also still open.

From the `.gridql` section of the design, still to come: parameterized queries
(`gridql run export_feeder.gridql --feeder FDR-104`, planned as a `PARAM feeder = "FDR-104"`
declaration with `$feeder` references), the `EXPORT CIM ... FROM ... INCLUDING ...` statement, and
`project.gridqlconfig`. `gridql run` currently loads the bundled sample network, since there is no
project config yet to point it at a real dataset.
