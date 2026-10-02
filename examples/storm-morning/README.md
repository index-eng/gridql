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
             Church St                     Mill Pond Rd + PV     High       Orchard Ln       │
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

Several houses on Mill Pond Rd have rooftop solar, recorded as one generator, `PV-7022`: 24 kW of
panels behind 25 kVA of inverters. It sits on the lateral that hangs off the span the storm is about
to bring a tree down on.

## 06:40: what the storm left

``` bash
gridql run outage
```

Before any answer, GridQL warns that validation found something in the model. `gridql validate`
gives the full report:

``` text
0 errors, 1 warning

warning possible-backfeed      PV-7022: generation on a section behind open REC-701-01, TIE-701-702, so its 24 devices may be backfed and their energized is unknown
```

Keep that in mind. The first question is what is not where it should be:

``` text
mRID        name                feeder   state  normal_state
----------  ------------------  -------  -----  ------------
FU-702-03   Willow Way tap      FDR-702  OPEN   CLOSED
REC-701-01  Mill Pond Recloser  FDR-701  OPEN   CLOSED
```

Two things have operated. A fuse blew on Willow Way, and the recloser on Millbrook North tried to
clear a fault, failed, and locked out.

Asked who is without power, GridQL names only Willow Way:

``` text
FIND loads WHERE NOT energized

mRID     name              feeder   customer_count  kw
-------  ----------------  -------  --------------  --
SP-8022  Willow Way 2-18   FDR-702  6               21
SP-8024  Willow Way 20-30  FDR-702  5               16
```

Everything below the recloser is cut off from the substation, but the Mill Pond Rd solar is cut off
with it. Whether those inverters are backfeeding the section depends on their anti-islanding, which
the model does not hold. So GridQL does not call those customers dark. Their `energized` is
unknown, and the next query asks for exactly that:

``` text
FIND loads WHERE energized IS MISSING

mRID     name                   feeder   customer_count  kw
-------  ---------------------  -------  --------------  ---
SP-7022  Mill Pond Rd 2-20      FDR-701  6               22
SP-7024  Mill Pond Rd 22-30     FDR-701  4               14
SP-7032  Millbrook High School  FDR-701  1               240
SP-7042  Orchard Ln 1-13        FDR-701  7               28
SP-7051  Maplewood Care Home    FDR-701  1               95
```

In practice the inverters almost certainly tripped within seconds, and meter data will confirm it.
But "almost certainly" is not something the model can establish, and calling the section dead would
be the dangerous mistake. Together the two lists account for 30 customers: 11 known dark on the
south feeder, and 19 on the north that are out or possibly backfed.

The last query in the file is the one worth noticing:

``` text
FIND fuses WHERE NOT energized OR energized IS MISSING SELECT mRID, name, state

mRID       name                  state
---------  --------------------  ------
FU-701-02  Mill Pond Rd tap      CLOSED
FU-701-03  Millbrook High riser  CLOSED
FU-701-04  Orchard Ln tap        CLOSED
FU-701-05  Maplewood Care riser  CLOSED
```

These fuses are intact, and out only because the recloser above them is open. A crew sent to
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

Everything beyond the Pine St switch is healthy, only cut off:

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
FIND loads WHERE NOT energized OR energized IS MISSING

mRID     name                feeder   customer_count  kw
-------  ------------------  -------  --------------  --
SP-7022  Mill Pond Rd 2-20   FDR-701  6               22
SP-7024  Mill Pond Rd 22-30  FDR-701  4               14
```

Twenty of the thirty customers are back. The remaining ten are on the faulted span.

### Before the tree crew climbs

The span with the tree on it is now open at both ends: the recloser above it, the Pine St switch
below. Is it dead?

``` text
FIND lines WHERE mRID = $fault SELECT mRID, name, energized

mRID       name       energized
---------  ---------  ---------
OH-701-02  OH-701-02  -

FIND generators WHERE energized IS MISSING SELECT mRID, name, kind, kw, kva

mRID     name                        kind  kw  kva
-------  --------------------------  ----  --  ---
PV-7022  Mill Pond Rd rooftop solar  pv    24  25
```

GridQL will not say. The Mill Pond Rd solar is on the same isolated section, through the intact
lateral fuse, so the span may be backfed. That is the answer a crew needs before work starts: treat
the span as live until it has been tested and grounded. `gridql validate` gives the same warning,
naming the solar and the two open switches that bound the section.

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
- Take the solar away: delete the `PV-7022` row from a copy of `data/storm`, and run `outage`
  against it. All 30 customers are reported dark, and `validate` has nothing to say.
- Ask the model a question of your own: `gridql 'FIND devices PROTECTED BY "REC-702-01"'`.
- Save the morning as a database with `gridql import-csv data/storm --db storm.sqlite`, then
  query it with `--db storm.sqlite`.
