# Storm morning in Millbrook

A small GridQL project that follows one outage from first light to restoration. It is the example
to read after the [60-second one](../../README.md#a-60-second-example). It uses the same language,
but here the queries tell a story in order, the way an operator would ask them.

``` text
storm-morning/
  project.gridqlconfig     the data, the queries, and the outage they are about
  data/storm/              the network at 06:40
  data/restored/           the same network at 09:30, three switch operations later
  queries/
    outage.gridql          what is out, and where to look
    isolate.gridql         the switching plan
    restore.gridql         what the switching achieved
```

Run everything from this directory. The project file is found there, so no `--csv` is needed except
to look at the later snapshot.

## The town

Millbrook substation (`SUB-7`) feeds two 12.47 kV circuits:

``` text
Millbrook North (FDR-701)

BKR-701 ── OH ──┬── REC-701-01 ── OH-701-02 ──┬── SW-701-02 ── OH ──┬─── OH ───┬─── OH ─── TIE-701-702
                │   recloser      the span    │   Pine St           │          │          normally open
             FU-701-01                     FU-701-02             FU-701-03  FU-701-04        │
             Church St                     Mill Pond Rd          High       Orchard Ln       │
                                                                 School     FU-701-05        │
                                                                            Care Home        │
Millbrook South (FDR-702)                                                                    │
                                                                                             │
BKR-702 ── OH ──┬── OH ── REC-702-01 ── OH ──┬───────────────── OH-702-04 ───────────────────┘
                │                            │
             FU-702-01                    FU-702-02  Grocery
             Station Rd                   FU-702-03  Willow Way
```

The two feeders meet at `TIE-701-702`, a switch left open so that each feeder runs as its own
radial circuit.

## 06:40: what the storm left

``` bash
gridql run outage
```

The first question is what is not where it should be:

``` text
mRID        name                feeder   state  normal_state
----------  ------------------  -------  -----  ------------
FU-702-03   Willow Way tap      FDR-702  OPEN   CLOSED
REC-701-01  Mill Pond Recloser  FDR-701  OPEN   CLOSED
```

Two things have operated. A fuse blew on Willow Way, and the recloser on Millbrook North tried to
clear a fault, failed, and locked out. Together they leave 30 customers dark, 19 on the north
feeder and 11 on the south.

The last query in the file is the one worth noticing:

``` text
FIND fuses WHERE NOT energized SELECT mRID, name, state

mRID       name                  state
---------  --------------------  ------
FU-701-02  Mill Pond Rd tap      CLOSED
FU-701-03  Millbrook High riser  CLOSED
FU-701-04  Orchard Ln tap        CLOSED
FU-701-05  Maplewood Care riser  CLOSED
```

These fuses are intact, and dark only because the recloser above them is open. A crew sent to
re-fuse them would have nothing to do. The fuse that *did* blow is missing from the list because its
source side is still live. `state` is what a device is doing, and `energized` is whether power
reaches it. GridQL keeps the two apart.

## The switching plan

The line patrol finds a tree across `OH-701-02`, the span just below the recloser.

``` bash
gridql run isolate
```

The project file names the recloser, the span and the switch to open as parameters, so the same
file serves the next storm with `--tripped`, `--fault` and `--isolate`.

The span is bounded by the switches connected to it:

``` text
FIND switches CONNECTED TO "OH-701-02"

mRID        name                type      state
----------  ------------------  --------  ------
FU-701-02   Mill Pond Rd tap    fuse      CLOSED
REC-701-01  Mill Pond Recloser  recloser  OPEN
SW-701-02   Pine St Switch      switch    CLOSED
```

The recloser is already open upstream. Opening the Pine St switch cuts the fault off from
everything beyond it. The Mill Pond Rd lateral hangs off the faulted span itself, so its 10
customers wait for the tree crew.

Everything beyond the Pine St switch is healthy, only dark:

``` text
FIND loads DOWNSTREAM OF "SW-701-02" SELECT COUNT(*), SUM(customer_count), SUM(kw)

COUNT(*)  SUM(customer_count)  SUM(kw)
--------  -------------------  -------
3         9                    363
```

The school, the care home and Orchard Ln can be picked up through the tie at the far end. The plan
is to open `SW-701-02` and close `TIE-701-702`.

`DOWNSTREAM OF` answered all of this while the recloser was still open. It follows the circuit as
it is *built*, not as it is switched this morning, so a locked-out recloser does not make the
equipment below it disappear from the model.

## 09:30: after the switching

The Pine St switch is open, the tie is closed, and a crew has replaced the Willow Way fuse.
`data/restored` is that snapshot. Its `devices.csv` differs from the 06:40 one in exactly those
three rows.

``` bash
gridql run restore --csv data/restored
```

``` text
FIND loads WHERE NOT energized

mRID     name                feeder   customer_count  kw
-------  ------------------  -------  --------------  --
SP-7022  Mill Pond Rd 2-20   FDR-701  6               22
SP-7024  Mill Pond Rd 22-30  FDR-701  4               14
```

Twenty of the thirty customers are back. The remaining ten are on the faulted span.

The customers beyond the Pine St switch are energized again. Their power now reaches them from
Millbrook South, across the closed tie, because energisation follows the switches as they stand.
Their `feeder` is still `FDR-701`. They belong to Millbrook North and are only borrowing the other
feeder's supply, so `FED BY "FDR-702"` still reports the south feeder's own 253 kW. The operator
has to add the 363 kW riding across the tie to that figure before deciding whether the south
feeder can hold it through the afternoon peak.

The first query in `restore.gridql` is the list of everything that has to go back to normal
before the day is done: the recloser, the Pine St switch and the tie.

## Try next

- Finish the day. Once the tree is cleared, the three switches go back to normal: in a copy of
  `data/restored`, set `REC-701-01` and `SW-701-02` to `CLOSED` and `TIE-701-702` to `OPEN`, then
  run `restore` against it. Nothing is off normal and nobody is out.
- Ask the model a question of your own: `gridql 'FIND devices PROTECTED BY "REC-702-01"'`.
- Save the morning as a database with `gridql import-csv data/storm --db storm.sqlite`, then
  query it with `--db storm.sqlite`.
