High level overview:

GridQL is a domain-specific query language for electric utility data and power-system models. Rather than requiring engineers to understand database schemas, GridQL lets them query the electric grid using concepts they already understand: feeders, substations, transformers, switches, reclosers, phases, voltage levels, connectivity, and electrical topology. Queries can express relationships such as upstream of, downstream of, connected to, and fed by, while supporting filters for equipment attributes and operating state. GridQL abstracts the underlying data source, allowing utility data to be queried consistently whether it originated from GIS, CIM, ADMS, SCADA, CSV files, or a relational database.

GridQL is also designed as a translation layer between utility data and industry standards. Query results can be returned as tables, JSON, CSV, GeoJSON, or CIM, allowing engineers to extract a specific portion of a utility network and generate standards-based CIM representations without manually manipulating complex CIM structures. Ultimately, GridQL provides a common, utility-native interface for exploring, analyzing, transforming, and exchanging electric grid data—essentially giving the power system its own query language.


1. How I'd start

Don't start by building the editor. Don't even start by building CIM import/export.

Start by building the utility semantic model + GridQL interpreter as a standalone Python project.

Something like:

gridcode/
├── gridql/
│   ├── lexer.py
│   ├── parser.py
│   ├── ast.py
│   └── evaluator.py
│
├── model/
│   ├── feeder.py
│   ├── device.py
│   ├── transformer.py
│   ├── switch.py
│   └── network.py
│
├── storage/
│   └── sqlite.py
│
├── cim/
│   ├── importer.py
│   └── exporter.py
│
└── cli.py

Then build this progression:

Step 1 — hard-code a tiny grid
network = Network()

feeder = network.add_feeder(
    "FDR-104",
    voltage="13.8kV"
)

feeder.add_recloser("REC-104-01")
feeder.add_switch("SW-104-17", normal_state="OPEN")
feeder.add_transformer("XFMR-104-22", kva=500)
Step 2 — make GridQL work
FIND reclosers

Then:

FIND switches
WHERE state = OPEN

Then:

FIND transformers
WHERE kva >= 500
Step 3 — add topology

This is where it gets interesting:

FIND transformers
DOWNSTREAM OF "REC-104-01"
Step 4 — add persistence

Put the model into SQLite.

Step 5 — CIM import/export

Now you have something worth mapping to CIM.

Step 6 — build the editor

Only after the language/model works would I put Monaco or another editor around it.

2. Where does the data actually live?

Yes: use a database.

But I would not make GridQL directly query raw database tables.

I'd use three layers:

             GridQL
                ↓
        Semantic Model API
                ↓
           Graph Model
                ↓
          SQLite/Postgres

The database is the storage mechanism.

The semantic model is the thing GridQL actually understands.

That's an important distinction.

What would the database look like?

You could start with SQLite.

For example:

devices
────────────────────────
id
mrid
name
device_type
voltage
phase
normal_state
state
feeders
────────────────────────
id
mrid
name
voltage
substation_id
connections
────────────────────────
from_device
to_device
transformers
────────────────────────
device_id
kva
primary_voltage
secondary_voltage

And so forth.

But here's the important part:

GridQL shouldn't know those tables exist.

When someone writes:

FIND transformers
DOWNSTREAM OF "REC-104"
WHERE kva > 500

the interpreter turns that into operations against your Network abstraction.

Something conceptually like:

network.devices \
    .of_type("transformer") \
    .downstream_of("REC-104") \
    .where(kva__gt=500)

That abstraction can then retrieve the necessary data from SQLite.

Why not just use SQL?

Because SQL doesn't naturally understand this:

DOWNSTREAM OF "REC-104"

You'd have to write increasingly ugly recursive queries against your connectivity tables.

Your language can make topology a first-class concept.

For example:

FIND devices
DOWNSTREAM OF "REC-104"

could internally perform a graph traversal:

REC-104
   │
   ├── SW-104-01
   │      │
   │      ├── XFMR-104-01
   │      └── XFMR-104-02
   │
   └── SW-104-02
          │
          └── XFMR-104-03

The user doesn't care how that's implemented.

And this leads to an important architectural decision

I would not start with Neo4j or another graph database.

It's tempting because the electrical network is obviously a graph.

But your initial dataset is small enough that SQLite + an in-memory graph representation is probably ideal.

Something like:

SQLite
  ↓
NetworkLoader
  ↓
Network
  ├── Devices
  ├── Feeders
  ├── Substations
  ├── Connectivity Graph
  └── CIM metadata

You can maintain the topology graph in memory while SQLite provides persistence.

Later, if the scale or query requirements justify it, you can change the storage implementation without changing GridQL.

The really cool part: multiple data sources

Eventually the database doesn't necessarily have to be the original source.

Imagine:

                 GridQL
                    │
              Semantic Model
                    │
        ┌───────────┼───────────┐
        ↓           ↓           ↓
      CIM          GIS        CSV/SQL
        │           │           │
        └───────────┼───────────┘
                    ↓
             Unified Model

A utility might give you:

CIM XML
GIS export
CSV equipment list
SQL database
OpenDSS model
ADMS export

GridCode could normalize all of them into one internal model.

Then the user doesn't care where the data originated.

I'd make CIM the canonical semantic vocabulary

This is where your utility experience gives you a huge advantage.

You don't want to invent:

Device
Feeder
Transformer
Switch
Terminal
ConnectivityNode

without considering existing standards.

You can make your internal model CIM-inspired / CIM-mapped, so that:

GridQL object
      ↓
Internal semantic object
      ↓
CIM class

For example:

GridQL:

FIND reclosers

maps roughly to:

CIM:
ProtectedSwitch

And:

FIND transformers

maps to:

PowerTransformer

The user gets a much friendlier abstraction than raw CIM RDF/XML.

--

Your first actual milestone

I'd make the first version ridiculously small.

Create a 10-device feeder:

Substation
    │
Breaker
    │
Recloser
    │
────┴────────────
│               │
Switch          Transformer
│               │
Tie             Load

Store it in SQLite.

Then make these five queries work:

FIND feeders
FIND reclosers
FIND devices DOWNSTREAM OF "REC-001"
FIND transformers WHERE kva >= 500
FIND switches WHERE state != normal_state

If you can make those work cleanly, you've proven the fundamental idea.

Then I'd add:

EXPORT CIM

One thing I'd strongly consider

Given your NodeFabric experience, you could eventually make the NodeFabric data model one of GridCode's native backends.

That would give you a very useful development loop:

GridCode → query utility model → visualize topology → export CIM → feed into NodeFabric

And NodeFabric itself becomes a real-world testbed for the language rather than you inventing a hypothetical utility dataset.

--- 

.gridql file type support

You'd be treating GridQL queries as reusable utility engineering artifacts, rather than disposable database queries.

For example:

feeder_analysis.gridql

could contain:

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

Then another:

export_feeder.gridql
EXPORT CIM
FROM feeder "FDR-104"
INCLUDING
    breakers,
    reclosers,
    switches,
    transformers,
    lines
And this becomes really interesting with Git

A utility could have a repository like:

utility-project/
├── model/
├── queries/
│   ├── large_transformers.gridql
│   ├── open_devices.gridql
│   ├── feeder_summary.gridql
│   └── export_cim.gridql
├── scripts/
│   └── analysis.py
└── project.gridqlconfig

Now .gridql files become something engineers can:

version-control
review
share
parameterize
schedule
run from CI/CD
execute against different utility datasets

You could even have:

$ gridql run feeder_analysis.gridql

and:

$ gridql run export_feeder.gridql --feeder FDR-104