import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute, load_network, save_network
from gridql.errors import GridQLNameError
from gridql.model import Switch


def mrids(objects):
    return sorted(obj.mrid for obj in objects)


class SampleTopologyTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_downstream_excludes_the_target(self):
        found = mrids(self.network.downstream_of("REC-001"))
        self.assertNotIn("REC-001", found)
        self.assertEqual(
            found,
            ["LN-002", "LOAD-001", "LOAD-002", "SW-001", "SW-002", "TIE-001",
             "XFMR-001", "XFMR-002"],
        )

    def test_downstream_of_a_leaf_is_empty(self):
        self.assertEqual(self.network.downstream_of("LOAD-001"), [])

    def test_upstream_walks_back_to_the_head(self):
        self.assertEqual(
            [obj.mrid for obj in self.network.upstream_of("XFMR-002")],
            ["SW-002", "REC-001", "LN-001", "BRK-001"],
        )

    def test_upstream_of_the_head_is_empty(self):
        self.assertEqual(self.network.upstream_of("BRK-001"), [])

    def test_connected_to_is_immediate_neighbours_only(self):
        self.assertEqual(
            mrids(self.network.connected_to("REC-001")),
            ["LN-001", "SW-001", "SW-002", "XFMR-001"],
        )

    def test_fed_by_a_device_includes_the_device(self):
        found = mrids(self.network.fed_by("SW-002"))
        self.assertEqual(found, ["LOAD-002", "SW-002", "XFMR-002"])

    def test_fed_by_a_feeder_is_every_device_on_it(self):
        self.assertEqual(len(self.network.fed_by("FDR-104")), len(self.network.devices))

    def test_fed_by_a_substation(self):
        self.assertEqual(len(self.network.fed_by("SUB-001")), len(self.network.devices))

    def test_traversal_ignores_switch_state(self):
        # SW-002 is open, but XFMR-002 is still physically downstream of it.
        self.assertIn("XFMR-002", mrids(self.network.downstream_of("SW-002")))

    def test_energisation_stops_at_an_open_switch(self):
        energized = {
            mrid: self.network.is_energized(mrid)
            for mrid in ("BRK-001", "REC-001", "SW-001", "TIE-001", "SW-002",
                         "XFMR-001", "XFMR-002", "LOAD-002")
        }
        self.assertEqual(
            energized,
            {"BRK-001": True, "REC-001": True, "SW-001": True, "TIE-001": True,
             "SW-002": True, "XFMR-001": True, "XFMR-002": False, "LOAD-002": False},
        )

    def test_lookup_by_name_and_case(self):
        self.assertEqual(self.network.get("rec-001").mrid, "REC-001")
        self.assertEqual(self.network.get("Mainline Recloser").mrid, "REC-001")

    def test_unknown_identifier_suggests_alternatives(self):
        with self.assertRaises(GridQLNameError) as raised:
            self.network.get("REC-002")
        self.assertIn("REC-001", str(raised.exception))


class CacheInvalidationTests(unittest.TestCase):
    """The derived topology must never outlive the state it was derived from.

    Adding equipment and making connections go through Network, which sees
    them. Operating a switch and moving a feeder head are plain attribute
    assignments, which it cannot see unless the object says so.
    """

    def setUp(self):
        self.network = build_sample_network()

    def dark(self):
        return execute(self.network, "FIND devices WHERE NOT energized").mrids

    def test_opening_a_switch_de_energises_what_is_below_it(self):
        self.assertEqual(self.dark(), ["LOAD-002", "XFMR-002"])
        self.network.get("SW-001").state = "OPEN"
        self.assertEqual(self.dark(), ["LN-002", "LOAD-002", "TIE-001", "XFMR-002"])

    def test_closing_a_switch_restores_it(self):
        self.dark()  # warm the cache first
        self.network.get("SW-002").state = "CLOSED"
        self.assertEqual(self.dark(), [])

    def test_a_switch_can_be_operated_repeatedly(self):
        switch = self.network.get("SW-001")
        for _ in range(3):
            switch.state = "OPEN"
            self.assertIn("TIE-001", self.dark())
            switch.state = "CLOSED"
            self.assertNotIn("TIE-001", self.dark())

    def test_moving_the_feeder_head_moves_the_root(self):
        feeder = self.network.feeders[0]
        self.assertNotIn("BRK-001", mrids(self.network.downstream_of("REC-001")))
        feeder.head = "REC-001"
        self.assertIn("BRK-001", mrids(self.network.downstream_of("REC-001")))

    def test_the_cache_is_still_a_cache(self):
        self.assertIs(self.network.topology(), self.network.topology())

    def test_a_change_replaces_the_cached_topology(self):
        before = self.network.topology()
        self.network.get("TIE-001").state = "CLOSED"
        self.assertIsNot(self.network.topology(), before)

    def test_normal_state_does_not_disturb_the_cache(self):
        # Only the present state feeds energisation; normal_state is reference data.
        before = self.network.topology()
        self.network.get("SW-001").normal_state = "OPEN"
        self.assertIs(self.network.topology(), before)

    def test_invalidate_is_available_for_changes_nothing_can_observe(self):
        before = self.network.topology()
        self.network.invalidate()
        self.assertIsNot(self.network.topology(), before)

    def test_a_switch_outside_any_network_can_still_be_set(self):
        loose = Switch(mrid="SW-LOOSE")
        loose.state = "OPEN"  # must not raise: it belongs to no network
        self.assertEqual(loose.state, "OPEN")

    def test_a_network_read_back_from_storage_invalidates_too(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite"
            save_network(self.network, path)
            loaded = load_network(path)
            self.assertEqual(
                execute(loaded, "FIND devices WHERE NOT energized").mrids,
                ["LOAD-002", "XFMR-002"],
            )
            loaded.get("SW-001").state = "OPEN"
            self.assertEqual(
                execute(loaded, "FIND devices WHERE NOT energized").mrids,
                ["LN-002", "LOAD-002", "TIE-001", "XFMR-002"],
            )


class BuilderTests(unittest.TestCase):
    def test_devices_chain_in_series_by_default(self):
        network = Network()
        feeder = network.add_feeder("FDR-1", voltage="12.47kV")
        feeder.add_breaker("BRK")
        feeder.add_switch("SW")
        feeder.add_transformer("XFMR", kva=100)
        self.assertEqual(mrids(network.downstream_of("BRK")), ["SW", "XFMR"])
        self.assertEqual(mrids(network.connected_to("SW")), ["BRK", "XFMR"])

    def test_after_creates_a_branch(self):
        network = Network()
        feeder = network.add_feeder("FDR-1", voltage="12.47kV")
        feeder.add_breaker("BRK")
        feeder.add_switch("SW-A")
        feeder.add_switch("SW-B", after="BRK")
        self.assertEqual(mrids(network.connected_to("BRK")), ["SW-A", "SW-B"])
        self.assertEqual(network.downstream_of("SW-A"), [])

    def test_devices_inherit_feeder_context(self):
        network = Network()
        network.add_substation("SUB", voltage="13.8kV")
        feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="SUB")
        device = feeder.add_switch("SW")
        self.assertEqual((device.feeder, device.substation, device.voltage),
                         ("FDR-1", "SUB", 13.8))

    def test_units_are_normalised_on_the_way_in(self):
        network = Network()
        feeder = network.add_feeder("FDR-1", voltage="13800V")
        transformer = feeder.add_transformer("XFMR", kva="0.5MVA")
        self.assertEqual(feeder.voltage, 13.8)
        self.assertEqual(transformer.kva, 500.0)

    def test_duplicate_mrid_is_rejected(self):
        network = Network()
        feeder = network.add_feeder("FDR-1")
        feeder.add_switch("SW")
        with self.assertRaises(ValueError):
            feeder.add_switch("SW")

    def test_topology_is_rebuilt_after_a_change(self):
        network = Network()
        feeder = network.add_feeder("FDR-1")
        feeder.add_breaker("BRK")
        self.assertEqual(network.downstream_of("BRK"), [])
        feeder.add_switch("SW")
        self.assertEqual(mrids(network.downstream_of("BRK")), ["SW"])


if __name__ == "__main__":
    unittest.main()
