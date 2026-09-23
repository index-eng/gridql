# Getting started with GridQL

A tour of what GridQL does and how to get it running, for someone seeing it for the first time.
For the full language reference, see the [README](README.md).

## What GridQL is

GridQL is a query language for electric utility networks. It lets an engineer ask questions of the
grid using the vocabulary they already have — feeders, substations, transformers, switches,
reclosers, phases, voltage levels, upstream and downstream — instead of learning whatever schema
happens to sit underneath.

```
FIND transformers DOWNSTREAM OF "REC-1201-01" WHERE kva >= 500
```

The same question in SQL means knowing the table layout and writing a recursive query over a
connectivity table. Here, "downstream of" is part of the language.

GridQL is also a translation layer. A query can carve out a slice of the system and hand it back
as a standards-based CIM document, so getting a feeder into an exchange format does not mean
hand-assembling CIM structures.

## Installing

You need **Python 3.11 or newer**. GridQL has **no dependencies** — everything it does uses the
standard library, so there is nothing to resolve and nothing to pin. The one exception is optional:
reading straight from a Postgres database needs the psycopg driver, which
`python3 -m pip install '.[postgres]'` brings with it.

Check your Python first:

```bash
python3 --version
```

### Option 1 — install it

From the project directory:

```bash
python3 -m pip install .
```

That puts a `gridql` command on your path. Confirm it worked:

```bash
gridql --version
gridql 'FIND reclosers'
```

If you plan to change the code, install it editable instead so your edits take effect immediately:

```bash
python3 -m pip install -e .
```

### Option 2 — run it without installing

Nothing has to be installed at all. From the project directory:

```bash
python3 -m gridql.cli 'FIND reclosers'
```

Everywhere this guide says `gridql`, `python3 -m gridql.cli` works the same way.

### Running the tests

```bash
python3 -m unittest discover -s tests
```

## Your first queries

Every command below works immediately from this directory. The repository's
[project file](project.gridqlconfig) points at the example data in [`examples/csv/`](examples/csv/):
two 12.47 kV feeders out of the Cedar Hill substation, 77 devices in all. Here is the larger one,
with the line sections between devices left out:

```
Substation SUB-12 "Cedar Hill"  ·  Feeder FDR-1201 @ 12.47 kV
──────────────────────────────────────────────────────────────────────
BKR-1201  feeder breaker (the feeder head)
  │
  ├─ FU-1201-01 ── Maple Ave lateral, A phase
  ├─ CAP-1201-01   300 kVAR capacitor bank
  │
REC-1201-01  mid-line recloser
  │
  ├─ FU-1201-02 ── Cedar Hill Plaza, underground: 750 and 300 kVA pad-mounts
  │
SW-1201-01  sectionalizing switch
  │
  ├─ SEC-1201-01 ── Ridge Rd branch
  │                   ├─ FU-1201-03 ── Birchwood Ct, B phase
  │                   ├─ FU-1201-04 ── Quarry Ln, C phase      <- blown
  │                   └─ TX-40340   ── Ridge Rd Farm Supply
  ├─ FU-1201-05 ── Oak St lateral, C phase
  ├─ FU-1201-06 ── Cedar Hill Elementary, 500 kVA pad-mount
  │
TIE-1201-1202  normally open tie to the end of FDR-1202
```

FDR-1202 is a shorter neighbour with its own recloser. Outside a project, with no `--db` or
`--csv`, GridQL uses a smaller bundled sample feeder, `FDR-104`, instead.

Start with the simplest thing:

```bash
gridql 'FIND reclosers'
```

```
mrid         name              type      feeder    phases  voltage  state   normal_state
-----------  ----------------  --------  --------  ------  -------  ------  ------------
REC-1201-01  Midline Recloser  recloser  FDR-1201  ABC     12.47    CLOSED  CLOSED
REC-1202-01  Midline Recloser  recloser  FDR-1202  ABC     12.47    CLOSED  CLOSED

2 rows
```

Then filter:

```bash
gridql 'FIND transformers WHERE kva >= 500'
```

Then ask a question about topology — the reason the language exists:

```bash
gridql 'FIND devices DOWNSTREAM OF "SEC-1201-01"'
```

Or start the interactive prompt and poke around:

```bash
gridql
```

```
GridQL 0.1.0  Copyright (C) 2026 Index Labs, LLC
Free software under AGPL-3.0-or-later, with NO WARRANTY; type '.license' for details.
Loaded: Cedar Hill example: examples/csv (CSV).
Type a query, '.help' for help, or '.quit' to exit.
gridql>
```

The `Loaded:` line names whatever is in effect — here, this repository's own project file and the
example data it points at.

`.help` lists the syntax, `.types` lists every type and the CIM class it maps to, `.config` shows
the project settings in effect, `.run <file> [name=value ...]` runs a saved query, `.validate`
checks the model, `.reload` reads the data again — from a live database, what it holds now — and
`.quit` leaves.

## The features, briefly

### Query by utility concept

`FIND <type>` where the type is `devices`, `switches`, `reclosers`, `breakers`, `fuses`,
`sectionalizers`, `transformers`, `lines`, `loads`, `capacitors`, `feeders` or `substations`.
Singular and common shorthand (`xfmrs`, `caps`, `subs`, `conductors`) work too.

`switches` deliberately covers every switching device — reclosers, breakers, fuses and ties —
because that is what an engineer means by it, and what CIM says.

### Topology as a first-class idea

| Relation | Asks |
| --- | --- |
| `DOWNSTREAM OF "X"` | what is electrically below X, within its own feeder |
| `UPSTREAM OF "X"` | what lies between X and the feeder head |
| `CONNECTED TO "X"` | what touches X directly |
| `FED BY "X"` | everything X supplies |

### Physical versus energized — the distinction that matters

Traversal **ignores switch state**. `DOWNSTREAM OF "SEC-1201-01"` tells you what is *physically*
below that sectionalizer, which is the question you are asking when planning work. Whether it is
*currently energized* is a separate question, and a separate attribute:

```bash
gridql 'FIND devices DOWNSTREAM OF "SEC-1201-01" WHERE NOT energized SELECT mRID, name, type'
```

```
mRID        name             type
----------  ---------------  -----------
OH-1201-24  OH-1201-24       line
OH-1201-25  OH-1201-25       line
TX-40331    Pole 40331       transformer
SP-40331    Quarry Ln 3-9    load
TX-40335    Pole 40335       transformer
SP-40335    Quarry Ln 11-19  load

6 rows
```

Those six are dark because fuse FU-1201-04 has blown. They are still downstream of the
sectionalizer, and GridQL keeps the two facts apart rather than quietly conflating them.

The two questions are answered differently on purpose. A feeder's tree covers that feeder's own
equipment and stops at a tie, so one circuit never swallows its neighbour. Energisation ignores
feeder boundaries and follows the real graph from every source, so closing a tie back-feeds the
next circuit — which is exactly what you want to ask before you close it.

### How far away is it?

Topology results come back in walking order — nearest the target first — so the everyday
fault-isolation question is just a query:

```bash
gridql 'FIND switches UPSTREAM OF "TX-40331" LIMIT 1'    # the nearest device: FU-1201-04
gridql 'FIND reclosers UPSTREAM OF "TX-40331" LIMIT 1'   # the recloser behind it: REC-1201-01
```

`hops` (distance from the query's target) and `depth` (distance from the feeder head) are ordinary
attributes you can select, filter, sort and total:

```
FIND devices DOWNSTREAM OF "REC-1201-01" SELECT mRID, type, hops
FIND devices DOWNSTREAM OF "REC-1201-01" WHERE hops <= 1
```

### Filters that read like the question

```
FIND switches WHERE state != normal_state        -- anything out of normal position
FIND devices  WHERE type IN (load, transformer)
FIND devices  WHERE phases CONTAINS A
FIND loads    WHERE NOT energized
```

Operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN (...)` and `CONTAINS`, combined with `AND`,
`OR`, `NOT` and parentheses. String comparison is case-insensitive.

Clause order is `FIND`, topology, `WHERE`, `SELECT`, `GROUP BY`, `ORDER BY`, `LIMIT`, `RETURN`; a
clause out of place says which one and where.

### Units that behave

Write the unit or leave it off; these are the same query:

```
FIND transformers WHERE kva >= 500
FIND transformers WHERE kva >= 500kVA
FIND transformers WHERE kva >= 0.5MVA
```

Dimensions are enforced, so `kva >= 500kW` is an error rather than a wrong answer.

### Answers that are numbers, not lists

```bash
gridql 'FIND loads DOWNSTREAM OF "REC-1201-01" SELECT COUNT(*), SUM(kw), SUM(kvar)'
```

```
COUNT(*)  SUM(kw)  SUM(kvar)
--------  -------  ---------
10        905      292

1 row
```

`COUNT`, `SUM`, `AVG`, `MIN` and `MAX` fold the match into one row. `GROUP BY` gives one row per
group, `ORDER BY` and `LIMIT` do what you would expect:

```
FIND devices GROUP BY type
FIND loads SELECT feeder, COUNT(*), SUM(kw) GROUP BY feeder ORDER BY SUM(kw) DESC
FIND transformers ORDER BY kva DESC LIMIT 10
```

Together with back-feed, that answers the everyday planning question: close this tie, and can the
neighbouring circuit carry what it picks up?

### Output you can use

`--format table` (the default), `json`, `csv`, or `cim`. Without `SELECT` you get the type's usual
columns plus anything the query filtered or sorted on, so `WHERE install_year < 2000` shows
`install_year`. `SELECT` picks the columns exactly, and `SELECT *` shows everything, your own
columns included:

```bash
gridql --format csv 'FIND transformers SELECT mRID, name, kva'
gridql 'FIND transformers SELECT *'
```

In a terminal the table is coloured: a switch out of its normal position and equipment that is not
energised stand out in yellow. Piped or redirected output stays plain, and `--color never` or
`NO_COLOR=1` turns it off.

### Checking a model before you trust it

Real models arrive with problems. `validate` names them instead of letting a query quietly return
a plausible wrong answer:

```bash
gridql validate                     # the project's data
gridql validate --db grid.sqlite    # or any other
```

**Errors** mean answers will be wrong — a feeder pointing at a substation that does not exist, a
feeder head belonging to a different feeder, a switch whose state is neither OPEN nor CLOSED.
**Warnings** mean you will get less than you expect — a loop in a circuit meant to be radial, a
feeder with no source, an island nothing can reach. Errors exit non-zero; add `--strict` in CI to
fail on warnings too.

### Saved queries: .gridql files

A query worth writing twice belongs in a file that can be reviewed, version-controlled and run in
CI:

```bash
gridql run queries/feeder_analysis.gridql
```

Files hold one or more statements separated by `;`, take `--` and `#` comments, and can name their
own output format with `RETURN`. The worked examples live in [`queries/`](queries/).

### Parameters: one file, any circuit

A saved query should not have to be edited to ask about a different feeder. `PARAM` declares what
a run can vary, and `$name` stands wherever a value would:

```sql
PARAM feeder  = "FDR-1201"
PARAM min_kva = 0

FIND transformers FED BY $feeder WHERE kva >= $min_kva ORDER BY kva DESC
```

```bash
gridql run queries/feeder_report.gridql                     # the defaults
gridql run queries/feeder_report.gridql --feeder FDR-1202   # another circuit
gridql run queries/feeder_report.gridql --min_kva 0.5MVA    # values carry units
```

Every declared parameter becomes an option of its own. A parameter with no default is *required*,
so a run that leaves it out is refused rather than answered against a stale value, and a `$name`
that was never declared is a syntax error rather than an empty result.

### The project file

`project.gridqlconfig` says where a project's data and queries live, so commands stop repeating
it. It is TOML, and it is found by walking up from the working directory the way git finds its
own:

```toml
name    = "Oakdale District"
db      = "grid.sqlite"
queries = "queries"

[params]
feeder = "FDR-104"
```

A project that reads CSV sets `csv = "gis-export"` instead of `db`, and can add
`mapping = "gis-export.toml"` to say what the export's columns mean — see
[Mapping your utility's columns](#mapping-your-utilitys-columns). A project whose network lives in
Postgres adds `postgres = "service=gis"` beside its `mapping` and `db` — see
[Reading from Postgres](#reading-from-postgres).

```bash
gridql config             # what is in effect, and the queries it points at
gridql 'FIND reclosers'   # against grid.sqlite, with no --db
gridql run feeder_report  # a query by name, from anywhere in the tree
```

Everything in it is a default: `--db`, `--csv`, `--postgres`, `--mapping` and `--<param>` win, and `--no-config`
ignores it altogether. This repository has [one of its own](project.gridqlconfig), naming
`examples/csv` as its data and `FDR-1201` as its usual feeder.

### Data formats

Every format below is read into the same in-memory `Network`. Once it is loaded the language
cannot tell where it came from, so nothing in a `.gridql` file depends on the source.

| Format | Read | Write |
| --- | --- | --- |
| CSV — a directory, or a single devices file | `--csv PATH` (with `--mapping FILE` for your own schema), `import-csv`, `load_csv()` | `write_csv()` |
| SQLite — GridQL's own schema | `--db PATH`, `load_network()` | `init`, `save_network()` |
| Postgres — a utility's own schema, through a mapping | `--postgres DATABASE`, `import-postgres`, `read_postgres()` | — |
| CIM RDF/XML | `import-cim`, `read_cim()` | `export-cim`, `RETURN cim` |
| OpenDSS — a master `.dss` file and what it redirects to | `import-dss`, `read_dss()` | — |
| Python objects | the `Network` builder API | — |

Query *results* render as `table`, `json`, `csv` or `cim`, chosen with `--format` or a `RETURN`
clause. That is a separate question from the input format: you can read CSV and return CIM.

A SQLite file must be one GridQL wrote — the loader checks a schema version and refuses anything
else rather than guessing. A Postgres database can be read as it is, through a mapping; reading
from Oracle, SQL Server or a GIS server means exporting to CSV first, which is what the CSV reader
is for — and a mapping file means the export can keep the source system's own table and column
names.

### Loading your own data

```bash
gridql --csv ./gis-export 'FIND transformers WHERE kva >= 500'
gridql import-csv ./gis-export --db grid.sqlite
```

A `devices.csv` is all that is strictly required; `connections.csv`, `feeders.csv` and
`substations.csv` are optional. Connectivity can be a `connections.csv` of device pairs, or
`from_node` and `to_node` columns in `devices.csv` naming the nodes at each device's ends. Column
names are matched loosely (`OBJECTID`, `Device Type`,
`Circuit`, `kV` all work), cells may carry units (`12470 V`, `0.5MVA`), and any column GridQL does
not recognise is kept as a queryable attribute rather than dropped. Bad rows are reported with
their line number instead of stopping the load. A query still runs when that happens, but warns
first — so an empty answer caused by rows that never loaded does not look like a real one.

[`examples/csv/`](examples/csv/) shows the shape: two feeders out of a small substation, 77
devices, with a blown lateral fuse to find.

```bash
gridql --csv examples/csv 'FIND loads WHERE NOT energized SELECT name, customer_count'
```

The columns GridQL looks for, under whatever spelling your export uses:

| File | Columns |
| --- | --- |
| `devices.csv` | `mrid`, `name`, `type`, `feeder`, `substation`, `phases`, `voltage`, plus the attributes of the type — `kva`, `kw`, `kvar`, `length`, `conductor`, `ampacity`, `state`, `normal_state`, `is_tie` |
| `connections.csv` | `from_device`, `to_device` |
| `feeders.csv` | `mrid`, `name`, `voltage`, `substation`, `head` |
| `substations.csv` | `mrid`, `name`, `voltage` |

A row needs only its `mrid` — or, in `connections.csv`, both ends. The accepted spellings live
in `ALIASES` in [`gridql/ingest/csv_files.py`](gridql/ingest/csv_files.py). If your export uses
names it does not know, or its files are not laid out this way at all, write a mapping instead
(next section) rather than renaming columns. A `type` value is matched against GridQL's own vocabulary
and then against CIM class names; anything it cannot place becomes generic equipment, with the
original string kept as `source_type` and a note in the report.

Two things worth exporting even though the loader does not demand them. Without
`connections.csv` there is no connectivity, so `DOWNSTREAM OF`, `UPSTREAM OF`, `energized` and
`depth` have nothing to walk and every device reads as an island. Without `state` and
`normal_state` on switches, every switch takes its default and the whole feeder reports as
energized. Run `gridql validate` after a load to see whether either applies.

### Mapping your utility's columns

The loose matching above is a guess, and a guess is fine for a first look. For data you will rely
on, a **mapping file** says exactly what your export's files and columns mean. The files can be
called anything and laid out however your GIS produces them, and the queries do not change.

[`examples/mapped/`](examples/mapped/) is the example data exported the way a GIS might do it —
a file per equipment type, numeric circuit and station numbers, voltages in volts, switch positions
as `O` and `C`, connectivity as the node at each end of every device — with the
[mapping](examples/mapped/mapping.toml) that reads it:

```bash
gridql import-csv examples/mapped --mapping examples/mapped/mapping.toml
```

```
loaded 77 devices, 2 feeders, 1 substations, 94 connections through 61 nodes
STATION.csv: unmapped columns kept as attributes: source_kv
FUSE.csv: unmapped columns kept as attributes: link_rating
CAPACITOR.csv: unmapped columns kept as attributes: control
TRANSFORMER.csv: unmapped columns kept as attributes: mounting, install_year
CONDUCTOR.csv: unmapped columns kept as attributes: construction
SERVICE_POINT.csv: unmapped columns kept as attributes: customer_count
FDR-1201: no head recorded, inferred BKR-1201 as the only breaker on the feeder
FDR-1202: no head recorded, inferred BKR-1202 as the only breaker on the feeder
validation: no problems found
not saved: pass --db PATH to keep it, or query the files in place with 'gridql --csv examples/mapped --mapping examples/mapped/mapping.toml <query>'
```

Without `--db` nothing is saved: a plain `gridql` answers from whatever the project file names —
in this repository `examples/csv`, which happens to hold the same network. Query the files in place
as that last line says, or load them once and query the database:

```bash
gridql import-csv examples/mapped --mapping examples/mapped/mapping.toml --db cedar-hill.sqlite
gridql --db cedar-hill.sqlite 'FIND loads WHERE NOT energized SELECT name, customer_count'
```

A mapping is TOML. Each section names a `file` and says which of its columns fill which GridQL
field:

```toml
[feeders]
file    = "CIRCUIT.csv"
mrid    = "FDR-{CIRCUIT_NO}"
name    = "CIRCUIT_DESC"
voltage = { column = "NOM_VOLTS", unit = "V" }

[[devices]]
file   = "SWITCH.csv"
mrid   = "FACILITY_ID"
feeder = "FDR-{CIRCUIT_NO}"
type   = { column = "SW_TYPE", values = { BKR = "breaker", RCL = "recloser", LBS = "switch" } }
state  = { column = "POSITION", values = { O = "OPEN", C = "CLOSED" } }

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

The sections are `substations`, `feeders`, `devices` and `connections`. Write `[[devices]]` once
per equipment file; the others may be repeated the same way. File names are relative to the
directory you pass to `--csv`. Only `devices` is required: feeders and substations the equipment
refers to are created for you, as they are without a mapping.

A field can be written three ways:

| Written as | Means | Example |
| --- | --- | --- |
| `"COLUMN"` | the value of that column | `name = "CIRCUIT_DESC"` |
| `"text {COLUMN} text"` | a template: text built around columns | `mrid = "FDR-{CIRCUIT_NO}"` |
| `{ value = "..." }` | the same value for every row | `type = { value = "transformer" }` |

The table form also takes `column = "..."` or `template = "..."`, and three options:

- `unit` — what unit bare numbers are in: `{ column = "NOM_VOLTS", unit = "V" }` reads `13800`
  as 13.8 kV. A cell that carries its own unit, like `0.5MVA`, is taken at its word.
- `values` — a translation of your codes into GridQL's: switch types, `O`/`C` positions, `Y`/`N`
  flags. Matching ignores case.
- `default` — what an empty cell means: `phases = { column = "PHASING", default = "ABC" }`.

Templates matter more than they look. Every object needs an `mrid` that is unique across the whole
network, and GIS tables often number their rows independently — switch `1001` and transformer
`1001` — so `mrid = "SW-{OBJECTID}"` keeps them apart. Wherever one object points at another
(`feeder`, `substation`, `head`, the two ends of a connection), use the same template the target
used for its own `mrid`, so the references line up. If a column a template needs is empty, the
whole field is empty rather than a half-built ID like `SW-`.

**With a mapping, nothing is guessed.** Only the columns you map are read as fields. Every other
column is kept as a queryable attribute under a lowercase name (`INSTALL_YEAR` becomes
`install_year`), so a column your utility calls `STATUS` stays `status` rather than being taken for
a switch position. Add `extras = false` to a section to keep none, or `extras = ["INSTALL_YEAR"]`
to keep only those. A column whose name GridQL already uses, such as an unmapped `LENGTH`, is left
out and reported, since it would be hidden behind GridQL's own attribute — map it to use it.

#### Building a mapping for your own export

Start small and let the import report guide you:

1. Map one equipment file with just `file` and `mrid`, and run `gridql import-csv <dir> --mapping
   <file>`. The report lists every column it kept as an attribute — those are the ones left to map.
2. Map the fields you need. Add a `values` table for any column of codes.
3. Run it again. Any code a `values` table does not cover is counted, file by file, and loaded as
   written:

   ```
   SWITCH.csv: SW_TYPE value 'RCL' has no translation for type (1 row); used as written
   ```

4. Add the other equipment files and the connections, then check the result with
   `gridql validate --csv <dir> --mapping <file>`.

Mistakes in the mapping itself stop the load with a message naming the mapping file and a
suggestion, rather than producing a network with holes in it:

```
error: gis-export.toml: [devices] SWITCH.csv: no column 'FACILTY_ID' for mrid (did you mean FACILITY_ID?). The file has: FACILITY_ID, DESCRIPTION, SW_TYPE, ...
```

Once it works, name it in `project.gridqlconfig` with `mapping = "gis-export.toml"`. It describes
your utility's export format rather than one folder of it, so every CSV read in the project uses it
— next month's export included — unless `--mapping` names a different one.

Connectivity can be listed device to device in a `connections` section, or — as most GIS exports
record it — as the nodes at each device's ends, with devices joined where their node IDs match.
Map those in the device section: `from_node = "FROM_NODE"` and `to_node = "TO_NODE"`.

### Reading from Postgres

If the network lives in a Postgres database — a GIS, an asset register — GridQL can read it where
it is instead of from an export. You need the driver first:

```bash
python3 -m pip install '.[postgres]'
```

There is no standard utility schema to guess from, so a Postgres import always takes a mapping. It
is the same format as above, with `table = "gis.switch"` where a CSV mapping has
`file = "SWITCH.csv"`. Table and column names are matched whatever their case, and a section can
give a `query` instead, for a join or a filter:

```toml
[[devices]]
query = "SELECT * FROM gis.transformer WHERE status = 'IN SERVICE'"
type  = { value = "transformer" }
mrid  = "facility_id"
kva   = "kva_rating"
```

To try it on the example data, load [`examples/postgres/cedar_hill.sql`](examples/postgres/cedar_hill.sql)
into a scratch database — the same two feeders as the CSV example, in typed tables — and import
it with [its mapping](examples/postgres/mapping.toml):

```bash
createdb cedar_hill
psql -d cedar_hill -f examples/postgres/cedar_hill.sql
gridql import-postgres postgresql:///cedar_hill --mapping examples/postgres/mapping.toml
```

Without `--db` that is a trial run: it reports what it read, just as `import-csv` does, and saves
nothing. Once the report looks right, there are two ways to query it.

**Live**, with `--postgres`, answers from the database as it is at that moment:

```bash
gridql --postgres postgresql:///cedar_hill --mapping examples/postgres/mapping.toml \
    'FIND fuses WHERE state = OPEN'
```

Every query reads all the mapped tables, since tracing connectivity needs the whole network. That
is the right trade for "what is open right now?" and the wrong one for a morning's worth of reports
on a big system. In the REPL the network is read once when it starts; `.reload` reads it again.

**From a snapshot**: add `--db cedar-hill.sqlite` to the import to keep what it read, then query
that with `--db` as usual. It answers quickly and puts no load on the database, and it is only as
current as the last import.

The import only ever reads. It runs in one read-only transaction, which also means every table is
read as it stood at the same moment, even while people are editing the GIS.

The database is named by a URL (`postgresql://gis@gis-db/utility`) or a libpq string
(`host=gis-db dbname=utility user=gis`, or `service=gis`). Keep the password out of it: libpq finds
one in `~/.pgpass` or `PGPASSWORD`.

A project records the rest. Name only `postgres` and `mapping`, and every query reads the database
live. Add a `db` and queries answer from that snapshot instead, a nightly refresh is one command,
and `--live` still reaches the database when a question cannot wait:

```toml
postgres = "service=gis"
mapping  = "gis.toml"
db       = "grid.sqlite"
```

```bash
gridql import-postgres --db grid.sqlite --force      # the nightly refresh
gridql 'FIND fuses WHERE state = OPEN'               # from grid.sqlite
gridql --live 'FIND fuses WHERE state = OPEN'        # from the database, now
```

A refresh is refused, leaving the old database in place, when the new data would drop a feeder,
lose more than a tenth of the equipment, or fail validation — pass `--skip-checks` when the change
is intended.

### Persistence: SQLite

```bash
gridql init grid.sqlite                      # create a database holding the sample network
gridql --db grid.sqlite 'FIND reclosers'     # query it
```

A network saved and reloaded comes back identical — same objects, same connectivity, same feeder
heads. Loading reads everything once and keeps the graph in memory, so topology queries stay fast
walks rather than recursive SQL.

To refresh a database from a new export, import over it with `--force`. It is replaced whole, not
merged, so it is worth looking at the export before it replaces anything:

```bash
gridql import-csv ./gis-export                            # read and validate; saves nothing
gridql import-csv ./gis-export --db grid.sqlite --force   # replace the database with it
gridql init grid.sqlite --empty --force                   # or clear it out entirely
```

GridQL also checks for you. It refuses to replace a database — and leaves it untouched — when the
new data has validation errors, has more than 10% fewer devices, or is missing a feeder the
database has, since those are what a truncated or wrongly filtered export looks like:

```
error: not saved: grid.sqlite was left as it was, because
  - it would replace 11 devices with 2, 82% fewer
Check the new data, or pass --skip-checks to replace it anyway.
```

If the change is real — a feeder retired, say — add `--skip-checks`. The same applies to
`import-cim` and `import-dss`.

### CIM import and export

```bash
gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-1201"'
gridql import-cim vendor-export.xml --db grid.sqlite
```

A query result *is* the export selection, and it brings what it needs with it — the containing
feeder and substation, the base voltages its equipment refers to, and the connectivity among the
selected equipment — so the document stands on its own.

The importer reads CIM from other tools, understands the specialisations they emit
(`LoadBreakSwitch`, `Disconnector`, `ConformLoad`, …), and reports anything it does not model
instead of dropping it silently. It has been tested against the IEEE 13, 123 and 8500-node test feeders as
GridAPPS-D publishes them: it finds each feeder's source, keeps equipment it has no class for so the
circuit stays whole, and reads phasing, tank-built transformers and capacitor ratings the way
distribution tools write them.

### OpenDSS models

```bash
gridql import-dss Master.dss --db grid.sqlite
gridql --db grid.sqlite 'FIND transformers WHERE kva >= 1000'
```

An OpenDSS model becomes one feeder, named for its circuit and headed by its source. Switches take
their kind from the fuse, recloser or relay on them, a regulator bank becomes one transformer, and
each bus gets its nominal voltage from the nearest of the script's voltage bases. The importer reads
the script without solving it, and says which commands it read but did not act on.

### Using it from Python

The bundled sample feeder is always there to experiment with:

```python
from gridql import build_sample_network, execute, render

network = build_sample_network()
result = execute(network, 'FIND switches WHERE state != normal_state')

print(result.mrids)              # ['SW-002']
print(render(result, 'json'))
```

Building your own network:

```python
from gridql import Network, save_network

network = Network()
network.add_substation("SUB-001", name="Oakdale", voltage="13.8kV")

feeder = network.add_feeder("FDR-104", voltage="13.8kV", substation="SUB-001")
feeder.add_breaker("BRK-001")                       # the first device is the feeder head
feeder.add_recloser("REC-001")                      # each add connects behind the last
feeder.add_transformer("XFMR-001", kva=500)
feeder.add_load("LOAD-001", kw=310)
feeder.add_switch("SW-001", after="REC-001")        # ...unless you branch explicitly

save_network(network, "grid.sqlite")
```

## Where things live

| Path | What it holds |
| --- | --- |
| `gridql/lang/` | the language: lexer, parser, AST, evaluator |
| `gridql/model/` | the semantic model: equipment, containers, the connectivity graph |
| `gridql/storage/` | the SQLite schema and loader |
| `gridql/ingest/` | reading and writing CSV, reading Postgres, and mapping files |
| `gridql/cim/` | CIM import and export |
| `gridql/dss/` | OpenDSS import: the script language, and what a model becomes |
| `gridql/validate.py` | model validation |
| `gridql/config.py` | `project.gridqlconfig`: which dataset, which queries |
| `gridql/data/sample.py` | the bundled sample feeder, used outside a project, built through the public API |
| `queries/` | example `.gridql` files, written for the example data |
| `examples/` | two realistic feeders: `csv/` in GridQL's own layout, `mapped/` as a GIS might export it, `postgres/` as a GIS database holds it |
| `project.gridqlconfig` | this repository's own project file |
| `tests/` | the test suite |
| `tests/reference/` | fetches the IEEE test feeders the reference-model tests read |
| `idea.md` | the original design notes this was built from |

## Not built yet

An editor, GeoJSON output and device geometry, a JSON model format, and reading databases other
than Postgres — Oracle and SQL Server exports go through CSV.

The `EXPORT CIM` statement from the design notes is not its own syntax — a `PARAM`, `FED BY` and
`RETURN cim` do the same job with clauses that already exist, as
[`queries/export_feeder.gridql`](queries/export_feeder.gridql) shows.

## Licence

AGPL-3.0-or-later, copyright &copy; 2026 Index Labs, LLC — see [LICENSE](LICENSE). Using GridQL
within your own organisation carries no obligations; your grid data and the `.gridql` files you
write are yours. See the [README](README.md#licence) for the details, including commercial terms.

## Next steps

- [README](README.md) — the full language reference
- [`queries/`](queries/) — worked examples to copy
- `gridql --help`, and `.help` inside the prompt
