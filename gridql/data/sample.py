# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A small sample feeder, built entirely through the builder API.

This is the circuit from the design doc's first milestone::

    Substation
        |
     Breaker
        |
     Recloser
        |
    ----+--------------------
    |                       |
   Switch              Transformer
    |                       |
   Tie                     Load

It is the loader seam: a SQLite ``NetworkLoader`` would return the same kind of
:class:`~gridql.model.network.Network` from the same public API, and nothing in
the language would change.
"""

from __future__ import annotations

from ..model import Network


def build_sample_network() -> Network:
    """Build the sample distribution feeder FDR-104."""
    network = Network(source="the bundled sample network FDR-104")
    network.add_substation("SUB-001", name="Oakdale", voltage="13.8kV")

    feeder = network.add_feeder(
        "FDR-104",
        name="Oakdale 104",
        voltage="13.8kV",
        substation="SUB-001",
    )

    # Getaway: substation breaker feeds the mainline recloser.
    feeder.add_breaker("BRK-001", name="Oakdale 104 Breaker")
    feeder.add_line("LN-001", length="2400ft", conductor="336 ACSR", ampacity=530)
    feeder.add_recloser("REC-001", name="Mainline Recloser")

    # Branch A: mainline out to the normally open tie with the next circuit.
    feeder.add_switch("SW-001", name="Mainline Sectionalizing Switch")
    feeder.add_line("LN-002", length="5100ft", conductor="336 ACSR", ampacity=530)
    feeder.add_switch("TIE-001", name="Tie to FDR-107", normal_state="OPEN", is_tie=True)

    # Branch B: service transformer hung off the recloser.
    feeder.add_transformer(
        "XFMR-001",
        name="Elm St Bank",
        after="REC-001",
        kva=500,
        primary_voltage="13.8kV",
        secondary_voltage="0.48kV",
    )
    feeder.add_load("LOAD-001", name="Elm St Commercial", kw=310, kvar=95, voltage="0.48kV")

    # Branch C: a lateral that is currently switched out for construction.
    feeder.add_switch(
        "SW-002",
        name="Maple Ln Lateral Switch",
        after="REC-001",
        normal_state="CLOSED",
        state="OPEN",
    )
    feeder.add_transformer(
        "XFMR-002",
        name="Maple Ln Bank",
        kva=75,
        phases="A",
        primary_voltage="13.8kV",
        secondary_voltage="0.24kV",
    )
    feeder.add_load(
        "LOAD-002", name="Maple Ln Residential", kw=48, kvar=12, phases="A", voltage="0.24kV"
    )

    return network
