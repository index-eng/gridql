"""Traversal order, and the hops/depth attributes it makes available."""

import unittest

from gridql import Network, build_sample_network, execute
from gridql.errors import GridQLError


def sample():
    return build_sample_network()


class ModelOrderTests(unittest.TestCase):
    """The model walks the circuit; the order it walks in is information."""

    def setUp(self):
        self.network = sample()

    def test_downstream_is_breadth_first_nearest_off_the_target_first(self):
        self.assertEqual(
            [o.mrid for o in self.network.downstream_of("REC-001")],
            ["SW-001", "SW-002", "XFMR-001", "LN-002", "LOAD-001", "XFMR-002",
             "LOAD-002", "TIE-001"],
        )

    def test_upstream_walks_back_toward_the_head(self):
        self.assertEqual(
            [o.mrid for o in self.network.upstream_of("XFMR-002")],
            ["SW-002", "REC-001", "LN-001", "BRK-001"],
        )

    def test_fed_by_leads_with_the_target_itself(self):
        self.assertEqual(
            [o.mrid for o in self.network.fed_by("SW-002")],
            ["SW-002", "XFMR-002", "LOAD-002"],
        )

    def test_the_walk_is_reproducible(self):
        once = [o.mrid for o in self.network.downstream_of("REC-001")]
        twice = [o.mrid for o in sample().downstream_of("REC-001")]
        self.assertEqual(once, twice)


class LanguageOrderTests(unittest.TestCase):
    """A topology query reports what the walk found, in the order it found it."""

    def setUp(self):
        self.network = sample()

    def mrids(self, source):
        return execute(self.network, source).mrids

    def test_upstream_keeps_its_order(self):
        # The defect this fixes: results used to come back sorted by mRID,
        # throwing away the one ordering the question implies.
        self.assertEqual(
            self.mrids('FIND devices UPSTREAM OF "XFMR-002"'),
            ["SW-002", "REC-001", "LN-001", "BRK-001"],
        )

    def test_downstream_keeps_its_order(self):
        found = self.mrids('FIND devices DOWNSTREAM OF "REC-001"')
        self.assertEqual(found[:3], ["SW-001", "SW-002", "XFMR-001"])
        self.assertNotEqual(found, sorted(found))

    def test_the_nearest_upstream_protective_device(self):
        # Which device operates for a fault here -- LIMIT 1 now means nearest,
        # not alphabetically first.
        self.assertEqual(self.mrids('FIND switches UPSTREAM OF "XFMR-002" LIMIT 1'), ["SW-002"])
        self.assertEqual(self.mrids('FIND reclosers UPSTREAM OF "XFMR-002" LIMIT 1'), ["REC-001"])

    def test_a_filter_does_not_disturb_the_order(self):
        self.assertEqual(
            self.mrids('FIND devices DOWNSTREAM OF "REC-001" WHERE NOT energized'),
            ["XFMR-002", "LOAD-002"],
        )

    def test_an_explicit_order_by_wins(self):
        self.assertEqual(
            self.mrids('FIND devices UPSTREAM OF "XFMR-002" ORDER BY mrid'),
            ["BRK-001", "LN-001", "REC-001", "SW-002"],
        )

    def test_without_a_relation_results_stay_sorted_by_mrid(self):
        found = self.mrids("FIND devices")
        self.assertEqual(found, sorted(found))

    def test_limit_takes_the_nearest(self):
        self.assertEqual(
            self.mrids('FIND devices DOWNSTREAM OF "REC-001" LIMIT 3'),
            ["SW-001", "SW-002", "XFMR-001"],
        )


class HopsTests(unittest.TestCase):
    def setUp(self):
        self.network = sample()

    def hops(self, source):
        result = execute(self.network, source)
        return {row["mRID"]: row["hops"] for row in result.rows()}

    def test_hops_counts_devices_from_the_target(self):
        self.assertEqual(
            self.hops('FIND devices DOWNSTREAM OF "REC-001" SELECT mRID, hops'),
            {"SW-001": 1, "SW-002": 1, "XFMR-001": 1, "LN-002": 2, "LOAD-001": 2,
             "XFMR-002": 2, "LOAD-002": 3, "TIE-001": 3},
        )

    def test_hops_upstream(self):
        self.assertEqual(
            self.hops('FIND devices UPSTREAM OF "XFMR-002" SELECT mRID, hops'),
            {"SW-002": 1, "REC-001": 2, "LN-001": 3, "BRK-001": 4},
        )

    def test_fed_by_puts_the_target_at_zero(self):
        self.assertEqual(
            self.hops('FIND devices FED BY "SW-002" SELECT mRID, hops'),
            {"SW-002": 0, "XFMR-002": 1, "LOAD-002": 2},
        )

    def test_connected_to_is_always_one(self):
        self.assertEqual(
            set(self.hops('FIND devices CONNECTED TO "REC-001" SELECT mRID, hops').values()),
            {1},
        )

    def test_a_container_target_measures_from_its_head(self):
        found = self.hops('FIND devices FED BY "FDR-104" SELECT mRID, hops')
        self.assertEqual(found["BRK-001"], 0)
        self.assertEqual(found["REC-001"], 2)

    def test_hops_can_be_filtered_on(self):
        self.assertEqual(
            execute(self.network, 'FIND devices DOWNSTREAM OF "REC-001" WHERE hops <= 1').mrids,
            ["SW-001", "SW-002", "XFMR-001"],
        )

    def test_hops_can_be_ordered_by(self):
        self.assertEqual(
            execute(
                self.network, 'FIND devices DOWNSTREAM OF "REC-001" ORDER BY hops DESC LIMIT 2'
            ).mrids,
            ["LOAD-002", "TIE-001"],
        )

    def test_hops_can_be_aggregated(self):
        self.assertEqual(
            execute(
                self.network,
                'FIND devices DOWNSTREAM OF "REC-001" SELECT MAX(hops), COUNT(*)',
            ).rows(),
            [{"MAX(hops)": 3, "COUNT(*)": 8}],
        )

    def test_the_first_relation_defines_hops(self):
        found = self.hops(
            'FIND devices DOWNSTREAM OF "REC-001" FED BY "FDR-104" SELECT mRID, hops'
        )
        self.assertEqual(found["SW-001"], 1)  # measured from REC-001, not the feeder head

    def test_hops_without_a_relation_is_refused(self):
        for source in (
            "FIND devices SELECT mrid, hops",
            "FIND devices ORDER BY hops",
            "FIND devices WHERE hops > 1",
            "FIND devices SELECT MAX(hops)",
            "FIND devices GROUP BY hops",
        ):
            with self.subTest(source=source), self.assertRaises(GridQLError) as raised:
                execute(self.network, source)
            self.assertIn("topology target", str(raised.exception))


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.network = sample()

    def test_depth_counts_from_the_feeder_head(self):
        rows = execute(self.network, "FIND devices SELECT mRID, depth").rows()
        depth = {row["mRID"]: row["depth"] for row in rows}
        self.assertEqual(depth["BRK-001"], 0)
        self.assertEqual(depth["LN-001"], 1)
        self.assertEqual(depth["REC-001"], 2)
        self.assertEqual(depth["LOAD-002"], 5)

    def test_depth_needs_no_relation(self):
        self.assertEqual(
            execute(self.network, "FIND devices WHERE depth = 0").mrids, ["BRK-001"]
        )

    def test_depth_is_empty_for_equipment_outside_any_tree(self):
        network = Network()
        feeder = network.add_feeder("FDR-1", voltage="13.8kV")
        feeder.add_breaker("B")
        from gridql.model import Device

        network.add(Device(mrid="LOOSE"))
        rows = execute(network, "FIND devices SELECT mRID, depth").rows()
        self.assertEqual({r["mRID"]: r["depth"] for r in rows}, {"B": 0, "LOOSE": None})

    def test_depth_updates_when_the_head_moves(self):
        self.assertEqual(self.network.depth_of("REC-001"), 2)
        self.network.feeders[0].head = "REC-001"
        self.assertEqual(self.network.depth_of("REC-001"), 0)


if __name__ == "__main__":
    unittest.main()
