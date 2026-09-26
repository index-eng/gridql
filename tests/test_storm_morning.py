"""The storm-morning example: the numbers its README tells the story with."""

import unittest
from pathlib import Path

from gridql import read_csv
from gridql.script import run_file
from gridql.validate import validate

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "storm-morning"
QUERIES = EXAMPLE / "queries"


def load(snapshot):
    document = read_csv(EXAMPLE / "data" / snapshot)
    assert not document.report.problems, document.report.problems
    return document.network


def mrids(result):
    return [row["mRID"] for row in result.rows()]


class StormMorningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storm = load("storm")
        cls.restored = load("restored")

    def run_query(self, network, name):
        return run_file(network, QUERIES / f"{name}.gridql")

    def test_both_snapshots_validate(self):
        for network in (self.storm, self.restored):
            self.assertFalse(validate(network).findings)

    def test_the_storm_leaves_nineteen_north_and_eleven_south_customers_out(self):
        off_normal, dark, by_feeder, dead_fuses = self.run_query(self.storm, "outage")
        self.assertEqual(sorted(mrids(off_normal)), ["FU-702-03", "REC-701-01"])
        self.assertEqual(len(dark.rows()), 7)
        self.assertEqual(
            {row["feeder"]: (row["SUM(customer_count)"], row["SUM(kw)"]) for row in by_feeder.rows()},
            {"FDR-701": (19, 399), "FDR-702": (11, 37)},
        )
        # The blown fuse is live on its source side; only intact fuses are dead.
        self.assertEqual({row["state"] for row in dead_fuses.rows()}, {"CLOSED"})

    def test_the_plan_opens_the_pine_st_switch_and_closes_the_tie(self):
        _, ends, restorable, total, ties = self.run_query(self.storm, "isolate")
        self.assertIn("SW-701-02", mrids(ends))
        self.assertEqual(sorted(mrids(restorable)), ["SP-7032", "SP-7042", "SP-7051"])
        self.assertEqual(total.rows()[0]["SUM(kw)"], 363)
        self.assertEqual(mrids(ties), ["TIE-701-702"])

    def test_after_switching_only_the_faulted_span_is_out(self):
        off_normal, dark, borrowed, _, _ = self.run_query(self.restored, "restore")
        self.assertEqual(
            sorted(mrids(off_normal)), ["REC-701-01", "SW-701-02", "TIE-701-702"]
        )
        self.assertEqual(mrids(dark), ["SP-7022", "SP-7024"])
        # Picked up through the tie, but still Millbrook North's.
        self.assertEqual({row["feeder"] for row in borrowed.rows()}, {"FDR-701"})


if __name__ == "__main__":
    unittest.main()
