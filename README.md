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

The semantic model and interpreter, SQLite persistence, CSV in and out, CIM import and export,
model validation, and saved queries that take parameters. Pure Python 3.11+, no dependencies.

New here? [**Getting started**](GETTING_STARTED.md) walks through installing it and what it can do.

## Quickstart

```bash
python3 -m gridql.cli                                  # REPL against the sample feeder
python3 -m gridql.cli 'FIND reclosers'                 # one-shot query
python3 -m gridql.cli --format json 'FIND loads'       # table (default), json or csv
python3 -m gridql.cli --explain 'FIND devices DOWNSTREAM OF "REC-001"'
python3 -m gridql.cli run queries/feeder_analysis.gridql   # run a saved query
python3 -m gridql.cli run feeder_report --feeder FDR-104   # ... with a parameter
python3 -m gridql.cli config                           # the project settings in effect
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
[ PARAM <name> [ = <value> ] ]*                       -- in a .gridql file only

FIND <type>
  [ DOWNSTREAM OF <device> | UPSTREAM OF <device> | CONNECTED TO <device> | FED BY <feeder> ]*
  [ WHERE <condition> ]
  [ SELECT <column> | <aggregate>, ... ]
  [ GROUP BY <column>, ... ]
  [ ORDER BY <column> [ASC|DESC], ... ]
  [ LIMIT <n> ]
  [ RETURN table | json | csv | cim ]
```

Clauses come in that order. Statements are separated by `;`. Any declared
parameter can be used as `$name` wherever a value is written.

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

Two rules govern traversal, and they are deliberately different.

**A feeder's tree is its own equipment.** Distribution feeders are tied to their neighbours through
normally open switches, so the physical graph runs across the whole system — a traversal that
followed it would have one feeder swallow the next. A device belongs to exactly one feeder (CIM
puts it in exactly one `EquipmentContainer`), so `DOWNSTREAM OF` and `UPSTREAM OF` stay inside that
feeder and stop at the tie. Adjacency itself is still physical: `CONNECTED TO` reaches across it.

**Traversal ignores switch state**, so `DOWNSTREAM OF "REC-001"` answers "what is physically below
this recloser", which is the question being asked when planning work. Whether something is
currently *energised* is a separate question, answered by the derived `energized` attribute:

```
FIND devices DOWNSTREAM OF "REC-001" WHERE NOT energized
```

Energisation is computed the other way round: it floods from *every* feeder head across the real
graph, blocked by open switches, ignoring feeder boundaries. That is what makes closing a tie
back-feed the neighbouring circuit — the question an engineer is actually asking.

Operating a switch updates that immediately — `network.get("SW-001").state = "OPEN"` and the next
query sees the new answer. The derived topology is cached, but the cache is keyed to the state it
was built from, so it cannot outlive a change. If you mutate the model in some way the network
cannot observe, call `network.invalidate()`.

Several relations may be stacked, and they intersect. The first one also decides two things
below: the order results come back in, and what `hops` measures.

**Results come back in walking order** — nearest the target first, then outward — because that is
the order the question implies. `UPSTREAM OF "XFMR-002"` reports the switch above it, then the
recloser above that, and so on to the feeder head. Add `ORDER BY` to override it; without a
topology relation, results stay sorted by mRID.

That makes the fault-isolation question a query:

```
FIND reclosers UPSTREAM OF "XFMR-002" LIMIT 1     -- which device operates for a fault here
```

### Distance: hops and depth

`hops` is how far a device is from the query's topology target, and `depth` is how far it is from
its feeder head. Both are ordinary attributes, so they can be selected, filtered, sorted and
aggregated:

```
FIND devices DOWNSTREAM OF "REC-001" SELECT mRID, type, hops
FIND devices DOWNSTREAM OF "REC-001" WHERE hops <= 1
FIND devices DOWNSTREAM OF "REC-001" SELECT MAX(hops), COUNT(*)
FIND devices WHERE depth = 0                       -- the feeder heads
```

`depth` needs nothing else. `hops` only means something relative to a target, so a query that
mentions it without a topology relation is refused rather than answered with blanks.

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

### Aggregates, grouping and ordering

Most questions about a grid end in a number, not a list. `COUNT`, `SUM`, `AVG`, `MIN` and `MAX`
go in the `SELECT` list and fold the matched equipment into a single row:

```
FIND loads DOWNSTREAM OF "REC-001" SELECT COUNT(*), SUM(kw), SUM(kvar)
```

```
COUNT(*)  SUM(kw)  SUM(kvar)
--------  -------  ---------
2         358      107
```

That is the load-transfer question: how much is below this point, and will the neighbouring
feeder carry it once the tie is closed?

`GROUP BY` gives one row per group. On its own it counts them, which is usually what you want:

```
FIND devices GROUP BY type
FIND loads SELECT feeder, COUNT(*), SUM(kw) GROUP BY feeder ORDER BY SUM(kw) DESC
```

A column that is neither aggregated nor grouped is refused rather than silently picking one
device's value. `COUNT(*)` counts rows; `COUNT(attr)` counts the rows that have that attribute, so
`SELECT COUNT(*), COUNT(kva)` over a mixed set tells you how many carry a rating. An aggregate over
nothing still answers — `COUNT` is `0`, the rest are empty.

`ORDER BY` and `LIMIT` work on equipment and on grouped rows alike:

```
FIND transformers ORDER BY kva DESC LIMIT 10
```

Equipment that has no value for the sort column sorts **last in both directions**, so "biggest
first" does not open with everything that has no rating at all. Without `ORDER BY`, results stay
sorted by mRID.

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
datasets — not disposable database queries. The examples live in [`queries/`](queries/).

### Parameters

A query is only reusable if the circuit it asks about can change without editing it. `PARAM`
declares what a run may vary, and `$name` stands wherever a value would:

```sql
PARAM feeder  = "FDR-104"
PARAM min_kva = 0

FIND transformers
FED BY $feeder
WHERE kva >= $min_kva
SELECT mRID, name, kva
ORDER BY kva DESC
```

```bash
gridql run queries/feeder_report.gridql                          # the defaults
gridql run queries/feeder_report.gridql --feeder FDR-201         # another circuit
gridql run queries/feeder_report.gridql --min_kva 0.5MVA         # values carry units
gridql run queries/feeder_report.gridql --param feeder=FDR-201   # the long way round
```

**Every declared parameter becomes an option of its own**, which is what makes the command read
like the question. `--param name=value` is the way in for a name that collides with one of
gridql's own options, such as `format`.

Declarations come before the first `FIND` and apply to every statement in the file. **A parameter
with no default is required**: running the file without it is refused, naming what is missing,
rather than answered against whatever the file happened to say last. A `$name` that was never
declared is a syntax error, so a typo cannot quietly return nothing.

```
$ gridql run queries/export_feeder.gridql --no-config
error: missing required parameter of queries/export_feeder.gridql: feeder. Supply it with --feeder <value>
```

That file declares `PARAM feeder` with no default, so it cannot be run without naming a circuit.
`--no-config` is what makes the example fail here: this repository's own project file supplies a
`feeder`, which is the layering working as intended.

**A parameter is always a value, never an attribute.** `WHERE state = $position` with
`--position normal_state` compares against the *text* "normal_state" rather than reading the
attribute, so binding can change what a query is about but never what it means. Values are read
the way the language reads literals — `0.5MVA` is a quantity with a unit, `FDR-104` is a name —
and `--explain` shows the query with its values substituted, which is the query that actually ran.

Parameters work for topology targets, comparison operands, `IN` lists and `LIMIT`, which is enough
to make an export a saved artifact:

```sql
PARAM feeder
FIND devices FED BY $feeder RETURN cim        -- queries/export_feeder.gridql
```

```bash
gridql run queries/export_feeder.gridql --feeder FDR-104 > FDR-104.xml
```

## Project configuration

A utility's GridQL work is a repository: a dataset, a directory of saved queries, and the circuit
those queries are usually about. `project.gridqlconfig` records it once, so commands stop
repeating it.

```toml
name    = "Oakdale District"
db      = "grid.sqlite"      # or: csv = "gis-export"
queries = "queries"

[params]
feeder = "FDR-104"
```

```bash
gridql config                    # what is in effect, and the queries it points at
gridql 'FIND reclosers'          # runs against grid.sqlite, with no --db
gridql run feeder_report         # a query by name, from anywhere in the tree
```

The file is TOML, found by walking up from the working directory the way git finds its own, so it
applies to a whole tree and the same commands work from any subdirectory. Paths resolve against
the file rather than the caller.

**Everything in it is a default.** An explicit `--db`, `--csv` or `--<param>` wins; `--config PATH`
names a different file, and `--no-config` ignores the search entirely — which is what a CI job
wants when it points at a dataset of its own.

`[params]` supplies a project's usual answers. They apply only to files that declare a `PARAM` of
that name, so one project-wide `feeder` does not break every query with no use for it. A setting
the file does not recognise is reported rather than ignored, since a silently dropped key looks
exactly like a setting that does not work.

This repository has [one of its own](project.gridqlconfig), pointing at `queries/` and naming no
dataset, so its queries run against the bundled sample feeder.

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

## Validation

A query engine that quietly picks an answer when the model is ambiguous is worse than one that
refuses, because there is no way to tell the two apart. `validate` surfaces the conditions where
GridQL would otherwise have to choose:

```bash
gridql validate                        # the bundled sample
gridql validate --db grid.sqlite
gridql validate --db grid.sqlite --strict    # also fail on warnings, for CI
```

```
1 error, 1 warning

error   invalid-state          SW-1: state is 'AJAR', expected OPEN or CLOSED
warning loop                   FDR-1: SW-A and SW-B close a loop; the feeder is not radial,
                               so upstream and downstream follow one arbitrary path
```

**Errors** mean the model is broken and answers from it will be wrong — a container that does not
exist, a container of the wrong kind, a feeder head that belongs to a different feeder, a switch
state that is neither OPEN nor CLOSED, equipment connected to itself. The exit code is non-zero.

**Warnings** mean the model is readable but something will behave unexpectedly, almost always by
returning less than you expect — a loop in a circuit meant to be radial, a feeder with no head, an
island the head cannot reach, equipment on no feeder or with no connections, a hard link between
two feeders that is not a tie. These exit zero unless you pass `--strict`.

Findings are grouped by cause, so one missing substation is one finding naming the equipment that
points at it rather than one finding per device. `import-cim` runs the same checks and reports the
counts, and `.validate` works inside the REPL.

```python
from gridql import build_sample_network, validate

report = validate(build_sample_network())
print(report.ok, report.counts())
for finding in report:
    print(finding.severity, finding.code, finding.message)
```

## Loading your own data

CSV is what a utility will actually hand you, so GridQL reads the shape people already keep in
spreadsheets and GIS exports:

```
gis-export/
  devices.csv       mrid, name, type, feeder, ...   (required)
  connections.csv   from_device, to_device          (optional)
  feeders.csv       mrid, name, voltage, head       (optional)
  substations.csv   mrid, name, voltage             (optional)
```

```bash
gridql --csv ./gis-export 'FIND transformers WHERE kva >= 500'   # query it in place
gridql import-csv ./gis-export --db grid.sqlite                  # or load it once
gridql export-csv ./out --db grid.sqlite                         # and back out again
```

`--csv` works anywhere `--db` does, including `run` and `validate`. A single file is taken to be
the equipment list. There is a worked example in [`examples/csv/`](examples/csv/).

**Real exports never use the column names you expect**, so headers are matched case-insensitively
against a table of aliases — `OBJECTID`, `Device Type`, `Circuit`, `Normal Position`, `kV`,
`Rating kVA` and friends all land where they should. Cells may carry units, so `12470 V` and
`0.5MVA` are read as 12.47 kV and 500 kVA.

**Columns GridQL does not recognise are kept, not dropped**, which means a utility's own fields
stay queryable:

```
FIND breakers WHERE install_year < 2000 SELECT mRID, install_year
```

An identifier with a leading zero stays text, so pole number `00412` survives.

**Types** are read as GridQL names (`recloser`, `xfmr`) or as CIM class names, since a GIS export
usually uses those — `LoadBreakSwitch`, `Disconnector`, `PowerTransformer`, `ACLineSegment`. A type
GridQL does not model is loaded as generic equipment with the original kept in `source_type`, and
reported rather than silently reshaped.

**Bad rows are reported, not fatal.** A row with no mRID, a duplicate, an unreadable number, a
connection to a device that is not in the file — each is skipped with its line number, and the
rest of the file loads:

```
loaded 6 devices, 1 feeders, 0 substations, 4 connections
4 row(s) skipped or incomplete:
  devices.csv:7: voltage: could not read 'not-a-voltage' as a quantity
  devices.csv:8: no mRID
  connections.csv:6: unknown device 'GHOST-99'
```

Feeders and substations are created from whatever the devices refer to, and a feeder carrying
exactly one breaker gets it as the head — with anything less clear reported rather than guessed,
the same rule CIM import uses.

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
                                              and binding: $feeder -> a value
               |
      Semantic Model API      gridql/model    -- devices, feeders, substations
               |
         Graph Model          gridql/model/network.py -- connectivity and traversal
               |
        SQLite storage        gridql/storage  -- schema and the NetworkLoader
         CSV in and out       gridql/ingest   -- the format utilities hand you

       gridql/config.py -- project.gridqlconfig: which dataset, which queries

      CIM RDF/XML  <-->  gridql/cim  -- translation to and from the standard

         gridql/validate.py -- what is wrong with a model, and how badly
```

The language only ever calls the semantic model's public API, and the model knows nothing about
where its objects came from. That boundary is the point: swapping in SQLite, CIM or a GIS export
changes the loader, not the language.

Every class records the CIM class it maps to (`reclosers` → `ProtectedSwitch`, `transformers` →
`PowerTransformer`, `loads` → `EnergyConsumer`, …), which is what CIM export is built on.

## Licence

GridQL is free software under the **GNU Affero General Public License, version 3 or later**
([LICENSE](LICENSE)). Copyright &copy; 2026 Index Labs, LLC.

**Using it inside your own organisation carries no obligations.** Download it, script against it,
modify it, run it on your own grid data — the licence asks nothing of you. Copyleft applies when
you pass copies on to others, or when you offer a *modified* GridQL to users over a network, in
which case those users must be offered your modified source. That last clause is the point of the
AGPL: it keeps a modified GridQL from being resold as a closed hosted service.

**Your data is not covered.** The licence governs this software, not what you do with it. Your
network model, the CIM and CSV that GridQL reads and writes, query results, and the `.gridql` files
you write are yours — they are the program's input and output, not derivative works of it, the way
a SQL script is not a derivative of the database engine.

A commercial licence is available for organisations that cannot take copyleft terms; contact Index
Labs, LLC.

## Not built yet

GeoJSON output and device geometry, a JSON model format, and the editor.

The `EXPORT CIM FROM feeder "FDR-104" INCLUDING ...` statement from the design is not implemented
as its own syntax. [`queries/export_feeder.gridql`](queries/export_feeder.gridql) does the same
work with clauses that already exist — a `PARAM`, `FED BY` and `RETURN cim` — which is why the
statement has not earned a grammar of its own.
