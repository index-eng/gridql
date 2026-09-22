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
uses it whenever you do not point it at a database of your own.

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
GridQL 0.1.0 -- sample network FDR-104 loaded.
Type a query, '.help' for help, or '.quit' to exit.
gridql>
```

`.help` lists the syntax, `.types` lists every type and the CIM class it maps to, and `.quit`
leaves.

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
| `DOWNSTREAM OF "X"` | what is electrically below X |
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
LOAD-002  Maple Ln Residential  load
XFMR-002  Maple Ln Bank         transformer
```

Those two are dark because SW-002 is open. They are still downstream of the recloser, and GridQL
keeps the two facts apart rather than quietly conflating them.

### Filters that read like the question

```
FIND switches WHERE state != normal_state        -- anything out of normal position
FIND devices  WHERE type IN (load, transformer)
FIND devices  WHERE phases CONTAINS A
FIND loads    WHERE NOT energized
```

Operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN (...)` and `CONTAINS`, combined with `AND`,
`OR`, `NOT` and parentheses. String comparison is case-insensitive.

### Units that behave

Write the unit or leave it off; these are the same query:

```
FIND transformers WHERE kva >= 500
FIND transformers WHERE kva >= 500kVA
FIND transformers WHERE kva >= 0.5MVA
```

Dimensions are enforced, so `kva >= 500kW` is an error rather than a wrong answer.

### Output you can use

`--format table` (the default), `json`, `csv`, or `cim`. `SELECT` picks the columns:

```bash
gridql --format csv 'FIND transformers SELECT mRID, name, kva'
```

### Saved queries: .gridql files

A query worth writing twice belongs in a file that can be reviewed, version-controlled and run in
CI:

```bash
gridql run queries/feeder_analysis.gridql
```

Files hold one or more statements separated by `;`, take `--` and `#` comments, and can name their
own output format with `RETURN`. Four worked examples live in [`queries/`](queries/).

### Persistence: SQLite

```bash
gridql init grid.sqlite                      # create a database holding the sample network
gridql --db grid.sqlite 'FIND reclosers'     # query it
```

A network saved and reloaded comes back identical — same objects, same connectivity, same feeder
heads. Loading reads everything once and keeps the graph in memory, so topology queries stay fast
walks rather than recursive SQL.

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
| `gridql/cim/` | CIM import and export |
| `gridql/data/sample.py` | the sample feeder, built through the public API |
| `queries/` | example `.gridql` files |
| `tests/` | the test suite |
| `idea.md` | the original design notes this was built from |

## Not built yet

An editor, GeoJSON output, CSV/JSON input loaders, parameterized queries
(`gridql run foo.gridql --feeder FDR-104`) and a project config file. The `EXPORT CIM` statement
from the design notes is not its own syntax — `RETURN cim` and `export-cim --query` do the same job
with clauses that already exist.

## Next steps

- [README](README.md) — the full language reference
- [`queries/`](queries/) — worked examples to copy
- `gridql --help`, and `.help` inside the prompt
