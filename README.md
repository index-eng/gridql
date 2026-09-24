# GridQL

A domain-specific query language for electric utility data and power-system models.

## What is GridQL?

The grid that brings electricity to homes and businesses is a huge web of equipment: substations,
power lines, transformers on poles, and switches and fuses that cut the power off when something
goes wrong. Utilities keep a record of every piece of it, and of how each piece is wired to the
next, in databases that can hold hundreds of thousands of entries.

GridQL is a way of asking questions of that record in the grid's own words. You describe what you
want the way someone at a utility would say it out loud — *the customers beyond this switch*, *the
large transformers on this circuit* — and GridQL works out where the answer lives and how to find
it.

The aim is to let the people who know the grid — engineers, planners, operators, analysts — get
answers from the data themselves, without having to learn how the database is organised or wait for
a specialist to write the query for them.

### Two examples

A *recloser* is an automatic circuit breaker partway along a power line. If it trips, everything
beyond it goes dark. Which customers would that affect?

```
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

A *load* is anything drawing power — here a grocery store, a school, a farm supply and rows of
houses — and `kw` is how much each one uses.

And which customers have no power right now?

```
$ gridql 'FIND loads WHERE NOT energized SELECT name, kw'
name             kw
---------------  --
Quarry Ln 3-9    12
Quarry Ln 11-19  17

2 rows
```

Nobody told GridQL which houses were out. It knows a fuse on Quarry Lane is open, and it followed
the wires to find who sits behind it.

### Why not just use SQL?

SQL is the standard language for asking questions of a database, and it is very good at it. But
SQL only knows about tables, rows and columns. It has no idea what a feeder is, which way power
flows, or that an open switch means the lights are off on the other side of it.

So in SQL, "which customers are beyond this recloser?" becomes a puzzle. You have to know which
tables hold the equipment and which hold the connections between them, then write a query that
walks those connections one step at a time, in the right direction, without wandering onto the
neighbouring circuit. For a typical database it looks something like this:

```sql
WITH RECURSIVE beyond(mrid) AS (
    SELECT c.to_device
      FROM connection c
     WHERE c.from_device = 'REC-1201-01'
    UNION
    SELECT c.to_device
      FROM connection c
      JOIN beyond b    ON c.from_device = b.mrid
      JOIN equipment e ON e.mrid = c.to_device
     WHERE e.feeder = 'FDR-1201'
)
SELECT e.name, l.kw
  FROM beyond b
  JOIN equipment e ON e.mrid = b.mrid
  JOIN load l      ON l.mrid = e.mrid;
```

— and that is the easy version, which assumes the data already records which way power flows. Real
utility data usually does not. In GridQL the same question is one line, and "downstream of" is part
of the language.

You might choose GridQL because:

- **It speaks the grid's language.** Feeders, transformers, reclosers, phases and voltages are
  built in, so a query reads like the question it answers.
- **It understands how the grid is wired.** Upstream, downstream, what a fuse protects, and what is
  energised right now are all things you can ask for directly.
- **You do not need to know the database.** GridQL reads spreadsheet exports, SQLite and Postgres
  databases, and industry-standard CIM and OpenDSS files, and the same query works on all of them.
- **Queries are short enough to share.** A colleague who has never written code can read one, check
  it asks the right thing, and run it again next week.

GridQL is not a replacement for SQL everywhere. Billing, work orders and anything else that is not
about how the grid is connected are still SQL's job. GridQL is for the questions where the wiring
is the point.

## For engineers

GridQL lets engineers query the grid with the concepts they already use — feeders, substations,
transformers, switches, reclosers, phases, voltage levels and electrical topology — instead of
learning a database schema.

```
$ gridql 'FIND transformers DOWNSTREAM OF "REC-1201-01" WHERE kva >= 500'
mrid      name       type         feeder    phases  kva  primary_voltage  secondary_voltage
--------  ---------  -----------  --------  ------  ---  ---------------  -----------------
TX-40210  Pad 40210  transformer  FDR-1201  ABC     750  12.47            0.48
TX-40512  Pad 40512  transformer  FDR-1201  ABC     500  12.47            0.48

2 rows
```

The semantic model and interpreter, SQLite persistence, CSV in and out, Postgres import, CIM import
and export, model validation, and saved queries that take parameters. Pure Python 3.11+, no
dependencies — reading straight from Postgres adds one, the psycopg driver.

New here? [**Getting started**](GETTING_STARTED.md) walks through installing it and what it can do.

## Quickstart

```bash
python3 -m gridql.cli                                  # REPL against the example data
python3 -m gridql.cli 'FIND reclosers'                 # one-shot query
python3 -m gridql.cli --format json 'FIND loads'       # table (default), json or csv
python3 -m gridql.cli --color never 'FIND switches'    # colour: auto (default), always, never
python3 -m gridql.cli --explain 'FIND devices DOWNSTREAM OF "REC-1201-01"'
python3 -m gridql.cli run queries/feeder_analysis.gridql   # run a saved query
python3 -m gridql.cli run feeder_report --feeder FDR-1202  # ... with a parameter
python3 -m gridql.cli config                           # the project settings in effect
python3 -m gridql.cli init grid.sqlite                 # create a database
python3 -m gridql.cli --db grid.sqlite 'FIND feeders'  # query it
python3 -m gridql.cli export-cim feeder.xml --query 'FIND devices FED BY "FDR-1201"'
python3 -m gridql.cli import-cim feeder.xml
python3 -m gridql.cli import-dss Master.dss --db grid.sqlite   # an OpenDSS model
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
| `PROTECTED BY "X"` | X's protection zone: everything below it down to, and including, the next protective devices |

Two rules govern traversal, and they are deliberately different.

**A feeder's tree is its own equipment.** Distribution feeders are tied to their neighbours through
normally open switches, so the physical graph runs across the whole system — a traversal that
followed it would have one feeder swallow the next. A device belongs to exactly one feeder (CIM
puts it in exactly one `EquipmentContainer`), so `DOWNSTREAM OF` and `UPSTREAM OF` stay inside that
feeder and stop at the tie. Adjacency itself is still physical: `CONNECTED TO` reaches across it.

**Traversal ignores where switches are right now**, so `DOWNSTREAM OF "REC-1201-01"` answers "what is physically below
this recloser", which is the question being asked when planning work. Whether something is
currently *energised* is a separate question, answered by the derived `energized` attribute:

```
FIND devices DOWNSTREAM OF "REC-1201-01" WHERE NOT energized
```

**A loop inside one feeder breaks at its normally open switch.** Some circuits are meshed within a
single feeder and run radially by leaving a switch normally open; the IEEE 123-bus feeder has two
loops closed that way. Nothing is fed *through* a normally open switch while another path reaches
the equipment beyond it, so upstream and downstream follow the circuit's normal configuration, and
validation does not report the designed open point as a loop. Equipment reachable only through such a
switch is still below it. This uses each switch's *normal* position, so operating one moves nothing.

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
the order the question implies. `UPSTREAM OF "TX-40331"` reports the line feeding it, then the
fuse on its lateral, then the branch above that, and so on to the feeder head. Add `ORDER BY` to override it; without a
topology relation, results stay sorted by mRID.

That makes the fault-isolation question a query:

```
FIND switches UPSTREAM OF "TX-40331" LIMIT 1      -- the nearest device to isolate a fault here
FIND reclosers UPSTREAM OF "TX-40331" LIMIT 1     -- and the recloser behind it
```

### Protection zones

Breakers, reclosers, fuses and sectionalizers are **protective devices**: each one opens on its own
to isolate a fault below it, so a fault takes out only the zone of the nearest one above it.
`PROTECTED BY "X"` is that zone — everything below X down to the next protective devices, which
are included because a fault on one of them is still X's to clear. Everything past them is in
their own zones. A plain switch bounds nothing, because it waits for someone to operate it.

```
FIND devices PROTECTED BY "SEC-1201-01" SELECT mRID, type, hops
```

```
mRID        type         hops
----------  -----------  ----
OH-1201-21  line         1
FU-1201-03  fuse         2
FU-1201-04  fuse         2
OH-1201-26  line         2
TX-40340    transformer  3
SP-40340    load         4

6 rows
```

A sectionalizer clears nothing itself. It counts the trips of the recloser behind it and opens
while the line is dead, so a permanent fault below it takes out only what is below it — which is
the boundary a zone describes, so it counts as protective. A feeder's zone is its head breaker's.
Naming a device that bounds no zone is an error that says so, rather than an empty answer.

Every device also has a `protected_by` attribute: the nearest protective device above it, and
exactly the X whose zone it is in. That turns "how many customers does each fuse take out?" into a
`GROUP BY`:

```
FIND loads SELECT protected_by, COUNT(*), SUM(kw) GROUP BY protected_by ORDER BY SUM(kw) DESC
```

```
protected_by  COUNT(*)  SUM(kw)
------------  --------  -------
FU-1201-02    2         545
FU-1201-06    1         210
FU-1202-02    1         88
...
```

Zones follow the circuit's normal configuration, like `UPSTREAM OF` and `DOWNSTREAM OF`: a blown
fuse is still the boundary of its zone, and equipment above every protective device, such as the
feeder head, has no `protected_by`.

### Distance: hops and depth

`hops` is how far a device is from the query's topology target, and `depth` is how far it is from
its feeder head. Both are ordinary attributes, so they can be selected, filtered, sorted and
aggregated:

```
FIND devices DOWNSTREAM OF "REC-1201-01" SELECT mRID, type, hops
FIND devices DOWNSTREAM OF "REC-1201-01" WHERE hops <= 1
FIND devices DOWNSTREAM OF "REC-1201-01" SELECT MAX(hops), COUNT(*)
FIND devices WHERE depth = 0                       -- the feeder heads
```

`depth` needs nothing else. `hops` only means something relative to a target, so a query that
mentions it without a topology relation is refused rather than answered with blanks.

### Conditions

Operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN (...)`, `CONTAINS` and `LIKE`, combined with
`AND`, `OR`, `NOT` and parentheses. `NOT` binds tightest, then `AND`, then `OR`.

`CONTAINS` is a substring test. `LIKE` is SQL's pattern match: `%` stands for any run of characters
and `_` for exactly one, and the pattern must match the whole value, so `LIKE "Pad"` is plain
equality while `LIKE "Pad%"` finds every name that starts with it.

```
FIND devices      WHERE name CONTAINS "Maple"     -- Maple anywhere in the name
FIND transformers WHERE name LIKE "Pad 40%"       -- names that start with Pad 40
FIND fuses        WHERE mrid LIKE "FU-120_-01"    -- the first fuse on each 120x feeder
```

A bare attribute is a truth test, so `WHERE energized` and `WHERE NOT energized` read naturally.

**A bare word on the right-hand side is an attribute if the type has one by that name, and a value
otherwise** — and a utility's own columns count, so `WHERE kva > customer_count` compares the two.
That is what makes the switching query work:

```
FIND switches WHERE state != normal_state     -- switches out of normal position
FIND switches WHERE state = OPEN              -- OPEN is not an attribute, so it is a value
FIND switches WHERE state = "OPEN"            -- quoting always forces the value reading
```

String comparisons are case-insensitive. An attribute the type cannot have is an error, not an
empty answer, so `WHERE kvaa >= 500` asks whether you meant `kva`. Equipment that simply lacks a
value does not match: `FIND devices WHERE kva >= 500` passes over the loads, which have none.

### SELECT

By default a query returns the columns that suit the type, **plus whatever it filtered or sorted
on**, so an answer shows its own evidence: `FIND transformers WHERE install_year < 2000` ends with
an `install_year` column, and `WHERE NOT energized` with `energized`. A filter on a column already
shown adds nothing.

`SELECT` picks the columns instead, in the order given, and column headers keep the spelling you
wrote — so `SELECT mRID` produces an `mRID` header even though attribute lookup is
case-insensitive. `SELECT *` shows everything the results carry: the usual columns, the type's
other fields, then the utility's own columns.

```
FIND transformers SELECT name, mRID, kva, primary_voltage, secondary_voltage
FIND transformers SELECT *
```

A selected attribute the object does not have comes back empty rather than being dropped, so
`FIND devices SELECT mrid, kva` lists every device with ratings only where they exist. Selecting
something no such type could have — `SELECT kvaa` — is an error, with a suggestion.

### Aggregates, grouping and ordering

Most questions about a grid end in a number, not a list. `COUNT`, `SUM`, `AVG`, `MIN` and `MAX`
go in the `SELECT` list and fold the matched equipment into a single row:

```
FIND loads DOWNSTREAM OF "REC-1201-01" SELECT COUNT(*), SUM(kw), SUM(kvar)
```

```
COUNT(*)  SUM(kw)  SUM(kvar)
--------  -------  ---------
10        905      292
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

### Colour

In a terminal, a table marks what deserves a second look: a switch or capacitor **out of its normal
position**, and equipment that is **not energised**, both in bold yellow. Headers are bold, and
empty cells and row counts are dimmed. Errors are red and warnings yellow, in query output and in
`validate` alike.

Colour marks what is abnormal, never a switch's position as such. Utilities do not agree on what red
and green mean for a switch — a North American one-line draws a closed breaker red and an open one
green, while anyone outside the industry reads red as the alarm — so colouring `OPEN` and `CLOSED`
would mislead one reader or the other. A switch out of its normal position means the same thing
everywhere.

`--color auto` (the default) colours only a terminal, never a pipe or a file, and honours
[`NO_COLOR`](https://no-color.org) and `FORCE_COLOR`. `--color always` and `--color never` override
all of that. JSON, CSV and CIM are data for another program and are never coloured.

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
PARAM feeder  = "FDR-1201"
PARAM min_kva = 0

FIND transformers
FED BY $feeder
WHERE kva >= $min_kva
SELECT mRID, name, kva
ORDER BY kva DESC
```

```bash
gridql run queries/feeder_report.gridql                          # the defaults
gridql run queries/feeder_report.gridql --feeder FDR-1202        # another circuit
gridql run queries/feeder_report.gridql --min_kva 0.5MVA         # values carry units
gridql run queries/feeder_report.gridql --param feeder=FDR-1202  # the long way round
```

**Every declared parameter becomes an option of its own**, which is what makes the command read
like the question. `--param name=value` is the way in for a name that collides with one of
gridql's own options, such as `format`.

Declarations come before the first `FIND` and apply to every statement in the file. **A parameter
with no default is required**: running the file without it is refused, naming what is missing,
rather than answered against whatever the file happened to say last. A `$name` that was never
declared is a syntax error, so a typo cannot quietly return nothing.

A value is only a number where the attribute it meets is one: `--min_kva 0.5MVA` converts against
`kva`, while `--feeder 0412` names circuit `0412`, not `412`, and `--device 12A` is a name, not
twelve amps.

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
gridql run queries/export_feeder.gridql --feeder FDR-1202 > FDR-1202.xml
```

## Project configuration

A utility's GridQL work is a repository: a dataset, a directory of saved queries, and the circuit
those queries are usually about. `project.gridqlconfig` records it once, so commands stop
repeating it.

```toml
name    = "Oakdale District"
db      = "grid.sqlite"      # or: csv = "gis-export"
queries = "queries"
# mapping = "gis-export.toml"   what the CSV columns mean; see "Mapping a utility's own schema"
# postgres = "service=gis"       a Postgres database, queried live or imported; see "Reading from Postgres"

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

**Everything in it is a default.** An explicit `--db`, `--csv`, `--postgres`, `--mapping` or `--<param>` wins; `--config PATH`
names a different file, and `--no-config` ignores the search entirely — which is what a CI job
wants when it points at a dataset of its own.

`[params]` supplies a project's usual answers. They apply only to files that declare a `PARAM` of
that name, so one project-wide `feeder` does not break every query with no use for it. A setting
the file does not recognise is reported rather than ignored, since a silently dropped key looks
exactly like a setting that does not work.

This repository has [one of its own](project.gridqlconfig): it points at `queries/` and at the
example data in [`examples/csv/`](examples/csv/), with `FDR-1201` as its usual feeder. Outside a
project, and with no `--db` or `--csv`, GridQL uses its bundled sample feeder, `FDR-104`.

**Output.** Each statement renders in its own format: its `RETURN` clause, else `--format`, else a
table. When a file has several statements, each result is preceded by a `-- n. <query>` comment so
the output stays readable. The exception is `--format json`, which emits a *single* JSON document —
an array with one `{query, type, rows}` entry per statement — so a CI job can parse the whole run
at once.

From Python:

```python
from gridql import load_csv, run_file

for result in run_file(load_csv("examples/csv"), "queries/feeder_summary.gridql"):
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
gridql validate                        # the project's data
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
points at it rather than one finding per device. `import-cim` runs the same checks and lists what
it finds (or, with `--db`, reports the counts), and `.validate` works inside the REPL.

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
the equipment list.

**The example data** in [`examples/csv/`](examples/csv/) is two 12.47 kV feeders out of the Cedar
Hill substation, 77 devices in all. Feeder 1201 has a mid-line recloser, a switched capacitor bank,
a sectionalizing switch, a sectionalized branch, fused single-phase laterals, underground
commercial services, and a normally open tie to 1202. One lateral fuse has blown:

```bash
gridql --csv examples/csv 'FIND devices WHERE state != normal_state'           # FU-1201-04
gridql --csv examples/csv 'FIND loads WHERE NOT energized SELECT SUM(customer_count)'   # 9
gridql --csv examples/csv 'FIND devices UPSTREAM OF "SP-40335"'                # back to BKR-1201
```

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

That is what `import-csv` prints. A query run with `--csv` shows only its answer, so when the load
skipped rows or found no equipment at all, it says so on stderr first and points at the report:

```
warning: no equipment loaded from ./export/TRANSFORMER.csv: 2 rows skipped or incomplete, starting with TRANSFORMER.csv:2: no mRID
  run 'gridql import-csv ./export/TRANSFORMER.csv' for the full report
```

Feeders and substations are created from whatever the devices refer to, and a feeder carrying
exactly one breaker gets it as the head — with anything less clear reported rather than guessed,
the same rule CIM import uses.

### Connectivity nodes

Most GIS and ADMS exports do not list which device connects to which. Instead each device names
the nodes at its two ends, and devices are connected wherever those nodes match:

```
mrid,type,feeder,from_node,to_node
BRK,breaker,F1,N0,N1
L1,line,F1,N1,N2
L2,line,F1,N2,N3        <- L1, L2 and L3 meet at N2: a branch point
L3,line,F1,N2,N4
LD1,load,F1,N3,         <- a load has one end
```

`from_node` and `to_node` go in `devices.csv`, and the usual spellings are recognised —
`FROM_NODE`, `FromNodeId`, `Bus1`/`Bus2`, `node1`/`node2`, or a single `node` column for
one-ended equipment. Rows can come in any order. `connections.csv` is still read if it is there,
so the two styles can be mixed.

**The nodes are kept, not just the connections they imply.** Every device on a node is connected
to every other, so `CONNECTED TO "L2"` includes `L3`. But the connections alone cannot tell a
branch point from three devices wired in a ring — the neighbours are the same. The feeder walk
uses the nodes: whichever device reaches a node first feeds everything else on it, so `L2` and
`L3` both hang off `L1`, and only reaching a device or a node a second way counts as a loop.
Parallel cables between the same two nodes are still reported as the loop they are.

`import-csv` reports how many nodes it read (`loaded 5 devices, ... 5 connections through 5
nodes`), and SQLite, `export-csv` and CIM export all keep the nodes. A mapping names the columns like any other field:
`from_node = "N-{FROM_NODE}"`.

### Mapping a utility's own schema

Guessing from headers only goes so far. One utility calls a circuit `FEEDER_ID`, the next
`CIRCUIT_NO`; one keeps every device in a single table, another has a file per equipment type;
switch positions arrive as `O` and `C`, voltages in volts. A **mapping file** says exactly what
the utility's files and columns mean, and the queries stay the same whoever's data is underneath:

```toml
[feeders]
file       = "CIRCUIT.csv"
mrid       = "FDR-{CIRCUIT_NO}"                     # a template: an ID built from columns
name       = "CIRCUIT_DESC"                         # a column
substation = "SUB-{STATION_NO}"
voltage    = { column = "NOM_VOLTS", unit = "V" }   # bare cells are in volts

[[devices]]
file   = "SWITCH.csv"
mrid   = "FACILITY_ID"
feeder = "FDR-{CIRCUIT_NO}"
type   = { column = "SW_TYPE", values = { BKR = "breaker", RCL = "recloser", LBS = "switch" } }
state  = { column = "POSITION", values = { O = "OPEN", C = "CLOSED" } }

[[devices]]
file = "TRANSFORMER.csv"
type = { value = "transformer" }                    # a constant: the whole file is one type
mrid = "FACILITY_ID"
kva  = "KVA_RATING"

[connections]
file        = "CONNECTIVITY.csv"
from_device = "FROM_FACILITY"
to_device   = "TO_FACILITY"
```

```bash
gridql import-csv ./gis-export --mapping gis-export.toml        # try it: the report says what it made
gridql --csv ./gis-export --mapping gis-export.toml 'FIND reclosers'
```

A field is a column, a `{COLUMN}` template, or a table naming a `column`, `template` or `value`,
with an optional `unit` the cells are written in, a `values` table translating the utility's
codes and a `default` for empty cells. Sections are `substations`, `feeders`, `devices` (as many
as there are files) and `connections`; file names are relative to the `--csv` directory. A device
section can map `from_node` and `to_node` instead of, or as well as, a `connections` section.

**With a mapping nothing is guessed.** A column the mapping does not name is kept as an attribute
— `INSTALL_YEAR` becomes `install_year` — but never read as a field, so a utility's `STATUS`
column cannot be mistaken for a switch position. `extras = false` keeps none, and a list keeps
just those. A column that would be hidden behind one of GridQL's own attributes is reported instead
of kept.

**Mistakes are reported, not guessed around.** A field GridQL does not have, a column the file does
not have, or a unit that does not fit is refused with a suggestion, naming the mapping file. The
import report counts every code a `values` table did not translate, so an unexpected position or
type turns up the first time the mapping is tried:

```
SWITCH.csv: SW_TYPE value 'SECT' has no translation for type (4 rows); used as written
SWITCH.csv: unmapped columns kept as attributes: install_year, mfr
```

A project names its mapping with `mapping = "gis-export.toml"` in `project.gridqlconfig`. It
describes the utility's export format rather than one directory of it, so it applies to any CSV
read in the project. [`examples/mapped/`](examples/mapped/) is the example data as a GIS might
export it — a file per equipment type, connected through `FROM_NODE` and `TO_NODE` — with the
mapping that reads it back to exactly the same network.

### Reading from Postgres

A GIS or asset database need not be exported first. GridQL reads it through a mapping — the same
format, with `table` where a CSV mapping has `file`, or a `query` for a join or a filter — and
either queries it live or saves it to SQLite:

```toml
[[devices]]
table  = "gis.switch"                  # schema-qualified; matched whatever its case
mrid   = "facility_id"
type   = { column = "sw_type", values = { BKR = "breaker", RCL = "recloser", LBS = "switch" } }
state  = { column = "position", values = { O = "OPEN", C = "CLOSED" } }
is_tie = "tie_flag"                    # a boolean column needs no translating

[[devices]]                            # the billing system knows who a service point serves
query = """
    SELECT sp.*, count(p.premise_no) AS customer_count
    FROM gis.service_point sp
    LEFT JOIN cis.premise p ON p.service_point = sp.facility_id
    GROUP BY sp.facility_id
"""
type  = { value = "load" }
mrid  = "facility_id"
```

```bash
pip install 'gridql[postgres]'                                                   # the psycopg driver
gridql --postgres postgresql://gis@gis-db/utility --mapping gis.toml 'FIND fuses WHERE state = OPEN'
gridql import-postgres postgresql://gis@gis-db/utility --mapping gis.toml       # what it made of it
gridql import-postgres postgresql://gis@gis-db/utility --mapping gis.toml --db grid.sqlite
```

**It only reads.** Everything is read in one read-only, repeatable-read transaction, so the import
cannot change the database, and every table is read as it stood at the same moment — an edit made
while it runs cannot leave a device on a feeder the feeder table has not caught up with.

**Typed columns read as their text would.** Booleans, numbers and dates arrive as a CSV cell holding
them would, so everything above about mappings applies unchanged, and a NULL is an empty cell that
a `default` fills. Columns holding no plain value — geometry, `bytea`, `json`, arrays — are not
kept as attributes unless `extras` names them; the report lists what it left out. A table's rows
are read in primary-key order, so the same row wins a duplicate every time, and a bad row is
reported by its key:

```
gis.switch (objectid 4411): duplicate mRID 'SW-1188'
```

**The connection** is a URL or a libpq string (`service=gis`); whatever it leaves out comes from the
`PG*` environment variables, `~/.pgpass` and `pg_service.conf`, so a password need not be written
anywhere GridQL reads. A project names it with `postgres = "..."` next to its `mapping` and `db`,
and `gridql import-postgres --db grid.sqlite --force` becomes the whole refresh, guarded by the same
checks as any other: an import that would drop feeders or a tenth of the equipment is refused.

**Live or snapshot.** `--postgres` answers from the database as it is now: `gridql`, `run`,
`validate`, `export-csv` and `export-cim` all take it. Tracing connectivity needs the whole network,
so a live query reads every mapped table each time it runs — right for "which fuses are open right
now?", wasteful for a batch of reports. For those, `import-postgres --db` saves a snapshot and
queries read that. In the REPL the network is read once, and `.reload` reads it again.

A project chooses with what it names. With `postgres` alone, every query reads the database live;
with `db` beside it, queries answer from the snapshot, and `--live` reads the database for a
question that cannot wait for the next refresh:

```bash
gridql 'FIND fuses WHERE state = OPEN'           # the snapshot in grid.sqlite
gridql --live 'FIND fuses WHERE state = OPEN'    # the database, now
```

[`examples/postgres/`](examples/postgres/) holds the example feeders as typed tables in a `gis`
schema, with customers in a separate `cis` one, and the mapping that reads them to exactly the
network the CSV example gives.

## Persistence

A network can be stored in SQLite and loaded back identically — same objects, same connectivity,
same feeder heads, so topology is restored rather than re-guessed.

```bash
gridql init grid.sqlite                       # schema + the sample network
gridql --db grid.sqlite 'FIND reclosers'
gridql run queries/open_devices.gridql --db grid.sqlite
```

With no `--db` or `--csv`, everything runs against the project's dataset, or the bundled sample
feeder outside a project. `gridql init` refuses to
overwrite an existing file unless you pass `--force`, and `--empty` creates the schema alone.

```python
from gridql import build_sample_network, load_network, save_network

save_network(build_sample_network(), "grid.sqlite")
network = load_network("grid.sqlite")          # the NetworkLoader
```

**Which database a plain `gridql` reads.** Saving a database does not make it the default. A
plain `gridql` reads the dataset `project.gridqlconfig` names, or the bundled sample network when
none does, so after a save the import says how to query what it saved and, when a plain `gridql`
will not, what to set:

```
saved to grid.sqlite
query it with 'gridql --db grid.sqlite'
note: a plain 'gridql' here still reads the bundled sample network FDR-104, because project.gridqlconfig names no dataset; set db = "grid.sqlite" in it to change that
```

**Refreshing.** A database is replaced whole, never merged: import the new export over it with
`--force`, and equipment that is not in the new data is gone. The replacement is one transaction,
so a save that fails leaves the old data in place.

```bash
gridql import-csv ./gis-export                               # look first: nothing is saved
gridql import-csv ./gis-export --db grid.sqlite --force      # then replace
gridql init grid.sqlite --empty --force                      # or clear it to an empty schema
```

Because a bad export — truncated, filtered to one circuit, broken — would otherwise swap good data
for less of it, `import-csv` and `import-cim` check before they replace a database, and leave it as
it was if

- validation finds errors in the new data,
- the new data has more than 10% fewer devices than the database, or
- a feeder in the database is missing from the new data.

```
error: not saved: grid.sqlite was left as it was, because
  - it would replace 11 devices with 2, 82% fewer
Check the new data, or pass --skip-checks to replace it anyway.
```

The import report still prints, so the reason is in front of you. When the change is intended — a
feeder retired, a region split off — `--skip-checks` replaces it anyway. A refresh that passes says
what it replaced: `saved to grid.sqlite (was 11 devices, now 10)`. Writing a new database is never
checked, since there is nothing to lose, and neither is `save_network()` from Python.

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
gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-1201"'
gridql export-cim - --query 'FIND transformers WHERE kva >= 500'   # to stdout
gridql import-cim vendor-export.xml --db grid.sqlite
```

```
FIND devices FED BY "FDR-1201" RETURN cim
```

A slice brings what it needs with it: the feeder and substation that contain it, the
`BaseVoltage` objects its equipment refers to, and the connectivity *among the selected
equipment*. The result is a document that stands on its own.

**What gets written.** Equipment uses its CIM class (`reclosers` → `ProtectedSwitch`,
`transformers` → `PowerTransformer`, `loads` → `EnergyConsumer`), and values are converted to CIM's
SI units — line length in metres, transformer ratings in VA on `PowerTransformerEnd` objects,
load in W and VAr, voltages as `BaseVoltage` in volts.

**Connectivity.** CIM never joins equipment directly: a device has `Terminal`s, and terminals meet
at a `ConnectivityNode`. Import keeps each node, so a bus with five devices on it stays one junction
(see [Connectivity nodes](#connectivity-nodes)). Export writes each node the model holds as one
`ConnectivityNode` with a terminal per device, and gives each plain device-to-device connection a
node of its own.

**Identifiers** are derived from mRIDs rather than freshly minted UUIDs, so exporting the same
network twice gives byte-identical output and a diff means the network really changed. An mRID that
is not a valid XML name is adjusted for `rdf:ID` and preserved exactly in
`cim:IdentifiedObject.mRID`.

**Extensions.** A few things the model needs have nowhere to live in CIM — which device heads a
feeder, a phase string, the `extras` bag. Those are written in a private `gridql:` namespace, so a
round trip is lossless while a standards-only consumer can ignore them. A full export and reimport
of a network reproduces it exactly, which the test suite asserts object by object.

**Reading other people's CIM.** The importer reads any CIM release's namespace (the cim16 URI or
CIM17's `CIM100`), understands the specialisations other tools emit (`LoadBreakSwitch`,
`Disconnector`, `ConformLoad`, …), merges a resource described more than once (`rdf:ID`, then
`rdf:about`), and reports any class or namespace it does not read rather than dropping it silently.
It reads the way distribution tools write a feeder:

- **Where power enters.** A feeder with an `EnergySource` is headed by it. Failing that, the importer
  infers the head from the single breaker on the feeder, and says so. If it cannot, it tells you
  `DOWNSTREAM OF` will come back empty for that feeder rather than inventing an answer.
- **Equipment GridQL has no class for** — an `EnergySource`, a `SeriesCompensator`, an inverter — still
  has terminals, and dropping it would cut the circuit wherever it stands in series. It is kept as a
  plain device whose `cim_class` is the one the document used (`FIND devices WHERE cim_class =
  EnergySource`), and export writes it back out under that class.
- **Phasing** comes from the per-phase objects CIM hangs off equipment on fewer than three phases
  (`ACLineSegmentPhase`, `EnergyConsumerPhase`, `SwitchPhase`, `ShuntCompensatorPhase`), and a
  transformer takes the phasing of its primary tank ends. The two legs of a 120/240 V service are
  written `s1s2`, as CIM names them.
- **Transformers built from tanks** — a bank of single-phase units, or a pole-top service
  transformer — are rated from their `TransformerTankInfo` datasheets; a bank's kVA is the sum of its
  tanks'.
- **Capacitors** are rated from their susceptance, `bPerSection × nomU²` over every section, and a bank
  with no sections switched in is open.

These are tested against the IEEE 13, 123 and 8500-node feeders as GridAPPS-D publishes them — CIM
written by another tool, checked against what those feeders are published to contain. The files are
not in the repository; `python3 tests/reference/fetch.py` downloads them, pinned to a commit and
checked by digest, and those tests skip until it has run.

```python
from gridql import read_cim

document = read_cim("vendor-export.xml")
print(document.report.summary())
network = document.network
```

## OpenDSS import

Most distribution planning models are OpenDSS scripts. `import-dss` reads one — the master file and
everything it redirects to — and reports what it found, as `import-cim` does:

```bash
gridql import-dss Master.dss                    # what is in it, and whether it validates
gridql import-dss Master.dss --db grid.sqlite   # keep it, and query it
```

```python
from gridql import read_dss

document = read_dss("IEEE8500/Master.dss")
print(document.report.summary())
```

OpenDSS is a scripting language, so the importer runs the commands that define things — `New`,
`Edit`, `like=`, `Redirect`, `Open`, `Close`, `Enable`, `Disable`, `Class.name.property=value` —
and counts the rest (`Solve`, `Show`, …) as read but not acted on. It never runs a power flow.
It understands the language as models are actually written: `~` continuations, `!`, `//` and
`/* */` comments, values given by position, arrays in any bracket, values in reverse Polish
(`%r=(.5 1000 /)`), `XfmrCode` and `LineCode` libraries, and Windows paths in `Redirect`.

Several things GridQL treats as facts are left for an OpenDSS reader to work out, and the importer
works them out the way a planner would:

- **The feeder.** A script is one circuit: it becomes a feeder named for the circuit, headed by its
  source, which is kept as a device whose `cim_class` is `EnergySource`. A transformer marked
  `sub=yes` names the substation.
- **Switches.** A switch is a `Line` with `switch=yes`. A `Relay`, `Fuse` or `Recloser` on it makes it
  a breaker, fuse or recloser. It is normally open if an `Open` command opened it, if a `SwtControl`
  says so, or if it is disabled — which is how some models draw their open points.
- **Banks.** Single-phase transformers that share a `bank` — a three-phase regulator, say — are one
  transformer, rated as their sum, as they are in CIM.
- **Voltage.** A bus has no voltage until a power flow gives it one. Each bus's nominal voltage is
  carried out from the source and every transformer winding, and settled on the nearest of the
  script's `voltagebases`, as OpenDSS's `CalcVoltageBases` would.
- **Phases** come from the bus nodes (`650.1.3` is phases A and C). A 120/240 V service is written on
  nodes 1 and 2 like phases A and B, so buses fed from a center-tapped transformer's secondary are
  found by walking out from it, and equipment on them is `s1s2`, as CIM names it.
- **Lengths** are converted to feet from the line's `units`, or its linecode's. A line with no units
  anywhere has no length, rather than a number of unknown meaning.

Equipment GridQL has no class for — PV, storage, generators, reactors — is kept as plain devices
with their OpenDSS class in `dss_class`, so the circuit stays whole. A disabled element is left out
and counted, except a switch or capacitor, which is kept, open.

The importer is tested against the IEEE 13, 123 and 8500-node feeders as OpenDSS models, and each is
compared device by device with the same feeder read from CIM: the same equipment, joined the same
way, with the same ratings. (`python3 tests/reference/fetch.py` downloads them.)

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
         CSV in and out       gridql/ingest   -- the format utilities hand you,
          Postgres in                            and the database behind it

       gridql/config.py -- project.gridqlconfig: which dataset, which queries

      CIM RDF/XML  <-->  gridql/cim  -- translation to and from the standard
   OpenDSS scripts  -->  gridql/dss  -- the models planners already have

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

GeoJSON output and device geometry, a JSON model format, reading databases other than Postgres
(Oracle and SQL Server exports go through CSV), and the editor.

The `EXPORT CIM FROM feeder "FDR-104" INCLUDING ...` statement from the design is not implemented
as its own syntax. [`queries/export_feeder.gridql`](queries/export_feeder.gridql) does the same
work with clauses that already exist — a `PARAM`, `FED BY` and `RETURN cim` — which is why the
statement has not earned a grammar of its own.
