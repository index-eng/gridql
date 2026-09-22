import unittest

from gridql import Network, build_sample_network
from gridql.errors import GridQLNameError


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
