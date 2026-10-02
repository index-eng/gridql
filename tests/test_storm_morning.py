"""The storm-morning example: the numbers its README tells the story with."""

import shutil
import tempfile
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

    def test_the_only_finding_is_the_solar_that_may_backfeed(self):
        for network, bounds in (
            (self.storm, ("REC-701-01", "TIE-701-702")),
            (self.restored, ("REC-701-01", "SW-701-02")),
        ):
            [finding] = validate(network).findings
            self.assertEqual(finding.code, "possible-backfeed")
            self.assertEqual(finding.objects, ("PV-7022", *bounds))

    def test_the_storm_leaves_nineteen_north_and_eleven_south_customers_out(self):
        off_normal, dark, unknown, solar, by_feeder, out_fuses = self.run_query(
            self.storm, "outage"
        )
        self.assertEqual(sorted(mrids(off_normal)), ["FU-702-03", "REC-701-01"])
        # Willow Way is known dark; below the recloser the solar leaves it unknown.
        self.assertEqual(mrids(dark), ["SP-8022", "SP-8024"])
        self.assertEqual(
            mrids(unknown), ["SP-7022", "SP-7024", "SP-7032", "SP-7042", "SP-7051"]
        )
        self.assertEqual(
            solar.rows(),
            [{"mRID": "PV-7022", "name": "Mill Pond Rd rooftop solar", "kind": "pv",
              "kw": 24, "kva": 25}],
        )
        self.assertEqual(
            {row["feeder"]: (row["SUM(customer_count)"], row["SUM(kw)"]) for row in by_feeder.rows()},
            {"FDR-701": (19, 399), "FDR-702": (11, 37)},
        )
        # The blown fuse is live on its source side; only intact fuses are out.
        self.assertEqual(len(out_fuses.rows()), 4)
        self.assertEqual({row["state"] for row in out_fuses.rows()}, {"CLOSED"})

    def test_the_plan_opens_the_pine_st_switch_and_closes_the_tie(self):
        _, ends, restorable, total, ties = self.run_query(self.storm, "isolate")
        self.assertIn("SW-701-02", mrids(ends))
        self.assertEqual(sorted(mrids(restorable)), ["SP-7032", "SP-7042", "SP-7051"])
        self.assertEqual(total.rows()[0]["SUM(kw)"], 363)
        self.assertEqual(mrids(ties), ["TIE-701-702"])

    def test_after_switching_only_the_faulted_span_is_out(self):
        off_normal, out, *_ = self.run_query(self.restored, "restore")
        self.assertEqual(
            sorted(mrids(off_normal)), ["REC-701-01", "SW-701-02", "TIE-701-702"]
        )
        self.assertEqual(mrids(out), ["SP-7022", "SP-7024"])

    def test_the_span_the_crew_works_on_is_not_known_to_be_dead(self):
        _, _, span, solar, *_ = self.run_query(self.restored, "restore")
        self.assertEqual(span.rows(), [{"mRID": "OH-701-02", "name": "OH-701-02", "energized": None}])
        self.assertEqual(mrids(solar), ["PV-7022"])

    def test_without_the_solar_the_span_is_dead(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            shutil.copytree(EXAMPLE / "data" / "restored", copy, dirs_exist_ok=True)
            devices = copy / "devices.csv"
            devices.write_text(
                "".join(
                    line for line in devices.read_text().splitlines(keepends=True)
                    if not line.startswith("PV-7022,")
                )
            )
            network = read_csv(copy).network
        _, _, span, solar, *_ = self.run_query(network, "restore")
        self.assertIs(span.rows()[0]["energized"], False)
        self.assertEqual(solar.rows(), [])

    def test_after_switching_the_borrowed_load_stays_north(self):
        *_, borrowed, _, _ = self.run_query(self.restored, "restore")
        # Picked up through the tie, but still Millbrook North's.
        self.assertEqual({row["feeder"] for row in borrowed.rows()}, {"FDR-701"})


if __name__ == "__main__":
    unittest.main()
