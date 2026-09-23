"""The design doc's first milestone, asserted exactly.

    FIND feeders
    FIND reclosers
    FIND devices DOWNSTREAM OF "REC-001"
    FIND transformers WHERE kva >= 500
    FIND switches WHERE state != normal_state

"If you can make those work cleanly, you've proven the fundamental idea."
"""

import unittest

from gridql import build_sample_network, execute
from gridql.cli import main

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project


def setUpModule():
    unittest.enterModuleContext(sample_project())


class MilestoneQueryTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def query(self, source):
        return execute(self.network, source).mrids

    def test_1_find_feeders(self):
        self.assertEqual(self.query("FIND feeders"), ["FDR-104"])

    def test_2_find_reclosers(self):
        self.assertEqual(self.query("FIND reclosers"), ["REC-001"])

    def test_3_find_devices_downstream_of_the_recloser(self):
        found = self.query('FIND devices DOWNSTREAM OF "REC-001"')

        # Everything below the recloser, and nothing else.
        self.assertEqual(
            sorted(found),
            ["LN-002", "LOAD-001", "LOAD-002", "SW-001", "SW-002", "TIE-001",
             "XFMR-001", "XFMR-002"],
        )
        # Reported in walking order: nearest to the recloser first.
        self.assertEqual(
            found,
            ["SW-001", "SW-002", "XFMR-001", "LN-002", "LOAD-001", "XFMR-002",
             "LOAD-002", "TIE-001"],
        )

    def test_4_find_transformers_by_rating(self):
        self.assertEqual(self.query("FIND transformers WHERE kva >= 500"), ["XFMR-001"])

    def test_5_find_switches_out_of_normal_position(self):
        self.assertEqual(self.query("FIND switches WHERE state != normal_state"), ["SW-002"])


class SampleNetworkTests(unittest.TestCase):
    def test_it_is_the_small_feeder_from_the_doc(self):
        network = build_sample_network()
        self.assertEqual(len(network.devices), 11)
        self.assertEqual(len(network.feeders), 1)
        self.assertEqual(len(network.substations), 1)
        self.assertEqual(network.feeders[0].head, "BRK-001")

    def test_every_device_carries_its_cim_class(self):
        network = build_sample_network()
        by_mrid = {device.mrid: device.CIM_CLASS for device in network.devices}
        self.assertEqual(by_mrid["REC-001"], "ProtectedSwitch")
        self.assertEqual(by_mrid["XFMR-001"], "PowerTransformer")
        self.assertEqual(by_mrid["LOAD-001"], "EnergyConsumer")
        self.assertEqual(by_mrid["LN-001"], "ACLineSegment")


class CommandLineTests(unittest.TestCase):
    def test_each_milestone_query_runs_from_the_cli(self):
        import contextlib
        import io

        for source in ("FIND feeders", "FIND reclosers", 'FIND devices DOWNSTREAM OF "REC-001"',
                       "FIND transformers WHERE kva >= 500",
                       "FIND switches WHERE state != normal_state"):
            for output_format in ("table", "json", "csv"):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = main([source, "--format", output_format])
                self.assertEqual(code, 0, source)
                self.assertTrue(buffer.getvalue().strip(), source)

    def test_a_syntax_error_exits_non_zero(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["FIND transformers WHERE kva >="])
        self.assertEqual(code, 1)
        self.assertIn("^", buffer.getvalue())

    def test_an_unknown_device_exits_non_zero(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(['FIND devices DOWNSTREAM OF "NOPE-1"'])
        self.assertEqual(code, 1)
        self.assertIn("error:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
