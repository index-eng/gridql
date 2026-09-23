# Getting started with GridQL

A tour of what GridQL does and how to get it running, for someone seeing it for the first time.
For the full language reference, see the [README](README.md).

## What GridQL is

GridQL is a query language for electric utility networks. It lets an engineer ask questions of the
grid using the vocabulary they already have — feeders, substations, transformers, switches,
reclosers, phases, voltage levels, upstream and downstream — instead of learning whatever schema
happens to sit underneath.

```
FIND transformers DOWNSTREAM OF "REC-001" WHERE kva >= 500
```

The same question in SQL means knowing the table layout and writing a recursive query over a
connectivity table. Here, "downstream of" is part of the language.

GridQL is also a translation layer. A query can carve out a slice of the system and hand it back
as a standards-based CIM document, so getting a feeder into an exchange format does not mean
hand-assembling CIM structures.

## Installing

You need **Python 3.11 or newer**. GridQL has **no dependencies** — everything it does uses the
standard library, so there is nothing to resolve and nothing to pin.

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

Every command below works immediately: GridQL ships with a small sample feeder, **FDR-104**, and
uses it whenever nothing points it at data of your own — no `--db`, no `--csv`, and no project
file naming a dataset.

```
Substation SUB-001 "Oakdale"  ·  Feeder FDR-104 @ 13.8 kV
─────────────────────────────────────────────────────────
   BRK-001  (feeder head)
      │
   LN-001
      │
   REC-001 ──────────────┬──────────────────┐
      │                  │                  │
   SW-001            XFMR-001            SW-002  (open — crew working)
      │                  │                  │
   LN-002            LOAD-001            XFMR-002
      │                                     │
   TIE-001 (normally open tie)           LOAD-002
```

Start with the simplest thing:

```bash
gridql 'FIND reclosers'
```

```
mrid     name               type      feeder   phases  voltage  state   normal_state
-------  -----------------  --------  -------  ------  -------  ------  ------------
REC-001  Mainline Recloser  recloser  FDR-104  ABC     13.8     CLOSED  CLOSED

1 row
```

Then filter:

```bash
gridql 'FIND transformers WHERE kva >= 500'
```

Then ask a question about topology — the reason the language exists:

```bash
gridql 'FIND devices DOWNSTREAM OF "REC-001"'
```

Or start the interactive prompt and poke around:

```bash
gridql
```

```
GridQL 0.1.0  Copyright (C) 2026 Index Labs, LLC
Free software under AGPL-3.0-or-later, with NO WARRANTY; type '.license' for details.
Loaded: GridQL sample project: the bundled sample network FDR-104.
Type a query, '.help' for help, or '.quit' to exit.
gridql>
```

The `Loaded:` line names whatever is in effect — here, this repository's own project file and the
sample feeder it falls back to.

`.help` lists the syntax, `.types` lists every type and the CIM class it maps to, `.config` shows
the project settings in effect, `.run <file> [name=value ...]` runs a saved query, `.validate`
checks the model, and `.quit` leaves.

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

Traversal **ignores switch state**. `DOWNSTREAM OF "REC-001"` tells you what is *physically* below
that recloser, which is the question you are asking when planning work. Whether it is *currently
energized* is a separate question, and a separate attribute:

```bash
gridql 'FIND devices DOWNSTREAM OF "REC-001" WHERE NOT energized SELECT mRID, name, type'
```

```
mRID      name                  type
--------  --------------------  -----------
XFMR-002  Maple Ln Bank         transformer
LOAD-002  Maple Ln Residential  load

2 rows
```

Those two are dark because SW-002 is open. They are still downstream of the recloser, and GridQL
keeps the two facts apart rather than quietly conflating them.

The two questions are answered differently on purpose. A feeder's tree covers that feeder's own
equipment and stops at a tie, so one circuit never swallows its neighbour. Energisation ignores
feeder boundaries and follows the real graph from every source, so closing a tie back-feeds the
next circuit — which is exactly what you want to ask before you close it.

### How far away is it?

Topology results come back in walking order — nearest the target first — so the everyday
fault-isolation question is just a query:

```bash
gridql 'FIND reclosers UPSTREAM OF "XFMR-002" LIMIT 1'   # which device operates for a fault here
```

`hops` (distance from the query's target) and `depth` (distance from the feeder head) are ordinary
attributes you can select, filter, sort and total:

```
FIND devices DOWNSTREAM OF "REC-001" SELECT mRID, type, hops
FIND devices DOWNSTREAM OF "REC-001" WHERE hops <= 1
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
gridql 'FIND loads DOWNSTREAM OF "REC-001" SELECT COUNT(*), SUM(kw), SUM(kvar)'
```

```
COUNT(*)  SUM(kw)  SUM(kvar)
--------  -------  ---------
2         358      107

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

### Checking a model before you trust it

Real models arrive with problems. `validate` names them instead of letting a query quietly return
a plausible wrong answer:

```bash
gridql validate --db grid.sqlite
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
PARAM feeder  = "FDR-104"
PARAM min_kva = 0

FIND transformers FED BY $feeder WHERE kva >= $min_kva ORDER BY kva DESC
```

```bash
gridql run queries/feeder_report.gridql                     # the defaults
gridql run queries/feeder_report.gridql --feeder FDR-201    # another circuit
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
[Mapping your utility's columns](#mapping-your-utilitys-columns).

```bash
gridql config             # what is in effect, and the queries it points at
gridql 'FIND reclosers'   # against grid.sqlite, with no --db
gridql run feeder_report  # a query by name, from anywhere in the tree
```

Everything in it is a default: `--db`, `--csv`, `--mapping` and `--<param>` win, and `--no-config`
ignores it altogether. This repository has [one of its own](project.gridqlconfig).

### Data formats

Every format below is read into the same in-memory `Network`. Once it is loaded the language
cannot tell where it came from, so nothing in a `.gridql` file depends on the source.

| Format | Read | Write |
| --- | --- | --- |
| CSV — a directory, or a single devices file | `--csv PATH` (with `--mapping FILE` for your own schema), `import-csv`, `load_csv()` | `write_csv()` |
| SQLite — GridQL's own schema | `--db PATH`, `load_network()` | `init`, `save_network()` |
| CIM RDF/XML | `import-cim`, `read_cim()` | `export-cim`, `RETURN cim` |
| Python objects | the `Network` builder API | — |

Query *results* render as `table`, `json`, `csv` or `cim`, chosen with `--format` or a `RETURN`
clause. That is a separate question from the input format: you can read CSV and return CIM.

A SQLite file must be one GridQL wrote — the loader checks a schema version and refuses anything
else rather than guessing. There is no connection to an external database: reading from Oracle,
Postgres or a GIS server means exporting to CSV first, which is what the CSV reader is for — and a
mapping file means the export can keep the source system's own table and column names.

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
first — so an empty answer caused by rows that never loaded does not look like a real one. See
[`examples/csv/`](examples/csv/) for the shape.

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

[`examples/mapped/`](examples/mapped/) is the sample feeder exported the way a GIS might do it —
a file per equipment type, numeric circuit and station numbers, voltages in volts, switch positions
as `O` and `C` — with the [mapping](examples/mapped/mapping.toml) that reads it:

```bash
gridql import-csv examples/mapped --mapping examples/mapped/mapping.toml
```

```
loaded 11 devices, 1 feeders, 1 substations, 10 connections
TRANSFORMER.csv: unmapped columns kept as attributes: install_year
FDR-104: no head recorded, inferred BRK-001 as the only breaker on the feeder
validation: no problems found
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
`import-cim`.

### CIM import and export

```bash
gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-104"'
gridql import-cim vendor-export.xml --db grid.sqlite
```

A query result *is* the export selection, and it brings what it needs with it — the containing
feeder and substation, the base voltages its equipment refers to, and the connectivity among the
selected equipment — so the document stands on its own.

The importer reads CIM from other tools, understands the specialisations they emit
(`LoadBreakSwitch`, `Disconnector`, `ConformLoad`, …), and reports anything it does not model
instead of dropping it silently.

### Using it from Python

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
| `gridql/ingest/` | reading and writing CSV, and mapping files |
| `gridql/cim/` | CIM import and export |
| `gridql/validate.py` | model validation |
| `gridql/config.py` | `project.gridqlconfig`: which dataset, which queries |
| `gridql/data/sample.py` | the sample feeder, built through the public API |
| `queries/` | example `.gridql` files |
| `examples/` | the sample feeder as CSV: `csv/` in GridQL's own layout, `mapped/` as a GIS might export it |
| `project.gridqlconfig` | this repository's own project file |
| `tests/` | the test suite |
| `idea.md` | the original design notes this was built from |

## Not built yet

An editor, GeoJSON output and device geometry, a JSON model format, and reading straight from a
database such as Postgres.

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
