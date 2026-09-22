"""Model validation, and the feeder-scoped traversal it depends on."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute, save_network, validate
from gridql.cli import main
from gridql.model import Device


def codes(report):
    return sorted({finding.code for finding in report})


def tied_feeders():
    """Two feeders joined by a normally open tie -- the standard arrangement."""
    network = Network()
    network.add_substation("SUB", voltage="13.8kV")

    a = network.add_feeder("FDR-A", voltage="13.8kV", substation="SUB")
    a.add_breaker("A-BRK")
    a.add_switch("A-SW")
    a.add_transformer("A-XFMR", kva=100)
    a.add_switch("TIE", normal_state="OPEN", is_tie=True, after="A-SW")

    b = network.add_feeder("FDR-B", voltage="13.8kV", substation="SUB")
    b.add_breaker("B-BRK")
    b.add_switch("B-SW")
    b.add_transformer("B-XFMR", kva=100)

    network.connect("TIE", "B-SW")
    return network


class FeederScopedTraversalTests(unittest.TestCase):
    """A feeder's tree is its own equipment; it must not run through a tie."""

    def setUp(self):
        self.network = tied_feeders()

    def test_a_feeder_does_not_swallow_its_neighbour(self):
        self.assertEqual(
            sorted(o.mrid for o in self.network.downstream_of("A-BRK")),
            ["A-SW", "A-XFMR", "TIE"],
        )

    def test_the_second_feeder_keeps_its_own_tree(self):
        self.assertEqual(
            sorted(o.mrid for o in self.network.downstream_of("B-BRK")),
            ["B-SW", "B-XFMR"],
        )

    def test_upstream_stops_at_the_feeder_source(self):
        self.assertEqual(
            [o.mrid for o in self.network.upstream_of("B-XFMR")], ["B-SW", "B-BRK"]
        )

    def test_adjacency_still_crosses_the_tie(self):
        # Physically they touch, and CONNECTED TO is a question about that.
        self.assertEqual(
            sorted(o.mrid for o in self.network.connected_to("TIE")), ["A-SW", "B-SW"]
        )

    def test_a_tied_model_is_not_flagged_as_a_problem(self):
        self.assertEqual(validate(self.network).findings, [])

    def test_energisation_ignores_feeder_boundaries(self):
        dark = lambda: execute(self.network, "FIND devices WHERE NOT energized").mrids
        self.assertEqual(dark(), [])

        self.network.get("B-BRK").state = "OPEN"
        self.assertEqual(dark(), ["B-SW", "B-XFMR"])

        # Closing the tie back-feeds B from A, which is the point of the tie.
        self.network.get("TIE").state = "CLOSED"
        self.assertEqual(dark(), [])

    def test_a_hard_link_between_feeders_is_flagged(self):
        self.network.connect("A-XFMR", "B-XFMR")  # not through a tie
        self.assertIn("cross-feeder-link", codes(validate(self.network)))


class CleanModelTests(unittest.TestCase):
    def test_the_sample_network_validates(self):
        report = validate(build_sample_network())
        self.assertEqual(report.findings, [])
        self.assertTrue(report.ok)
        self.assertFalse(report)
        self.assertEqual(report.summary(), "no problems found")

    def test_an_empty_network_validates(self):
        self.assertTrue(validate(Network()).ok)


class ErrorTests(unittest.TestCase):
    def report_for(self, build):
        network = Network()
        network.add_substation("SUB", voltage="13.8kV")
        build(network)
        return validate(network)

    def test_a_missing_container(self):
        def build(network):
            network.add_feeder("FDR-1", voltage="13.8kV", substation="NOPE").add_breaker("B")

        report = self.report_for(build)
        self.assertIn("dangling-reference", codes(report))
        self.assertFalse(report.ok)

    def test_missing_containers_are_grouped_into_one_finding(self):
        def build(network):
            feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="NOPE")
            for index in range(10):
                feeder.add_switch(f"SW-{index}")

        dangling = [f for f in self.report_for(build) if f.code == "dangling-reference"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("more", dangling[0].message)

    def test_a_container_of_the_wrong_kind(self):
        def build(network):
            network.add(Device(mrid="D", feeder="SUB"))

        self.assertIn("wrong-container", codes(self.report_for(build)))

    def test_a_head_belonging_to_another_feeder(self):
        def build(network):
            a = network.add_feeder("FDR-A", voltage="13.8kV", substation="SUB")
            a.add_breaker("A-BRK")
            b = network.add_feeder("FDR-B", voltage="13.8kV", substation="SUB")
            b.add_transformer("X", kva=10)
            b.head = "A-BRK"

        report = self.report_for(build)
        self.assertIn("bad-feeder-head", codes(report))
        self.assertIn("belongs to FDR-A", report.errors[0].message)

    def test_a_head_that_does_not_exist(self):
        def build(network):
            feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="SUB")
            feeder.add_breaker("B")
            feeder.head = "GHOST"

        self.assertIn("bad-feeder-head", codes(self.report_for(build)))

    def test_an_invalid_switch_state(self):
        def build(network):
            feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="SUB")
            feeder.add_breaker("B")
            feeder.add_switch("SW")
            network.get("SW").state = "AJAR"

        self.assertIn("invalid-state", codes(self.report_for(build)))

    def test_a_self_connection(self):
        def build(network):
            feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="SUB")
            feeder.add_breaker("B")
            network.connect("B", "B")

        report = self.report_for(build)
        self.assertIn("self-connection", codes(report))
        # and it is not also reported as a loop
        self.assertNotIn("loop", codes(report))


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.network = Network()
        self.network.add_substation("SUB", voltage="13.8kV")

    def feeder(self, mrid="FDR-1"):
        return self.network.add_feeder(mrid, voltage="13.8kV", substation="SUB")

    def test_a_loop_makes_the_feeder_non_radial(self):
        feeder = self.feeder()
        feeder.add_breaker("B")
        feeder.add_switch("SW-A")
        feeder.add_switch("SW-B")
        self.network.connect("SW-B", "B")

        report = validate(self.network)
        self.assertIn("loop", codes(report))
        self.assertTrue(report.ok, "a loop is a warning, not an error")

    def test_a_headless_feeder(self):
        feeder = self.feeder()
        feeder.add_breaker("B")
        feeder.head = None
        self.assertIn("headless-feeder", codes(validate(self.network)))

    def test_an_empty_feeder(self):
        self.feeder()
        self.assertIn("empty-feeder", codes(validate(self.network)))

    def test_equipment_the_head_cannot_reach(self):
        feeder = self.feeder()
        feeder.add_breaker("B")
        feeder.add_switch("SW-A")
        island_a = self.network.add(Device(mrid="IS-1", feeder="FDR-1"))
        island_b = self.network.add(Device(mrid="IS-2", feeder="FDR-1"))
        self.network.connect(island_a.mrid, island_b.mrid)
        self.assertIn("unreachable", codes(validate(self.network)))

    def test_equipment_on_no_feeder(self):
        self.network.add(Device(mrid="ORPHAN"))
        self.assertIn("unassigned-device", codes(validate(self.network)))

    def test_equipment_with_no_connections(self):
        feeder = self.feeder()
        feeder.add_breaker("B")
        self.network.add(Device(mrid="LOOSE", feeder="FDR-1"))
        report = validate(self.network)
        self.assertIn("isolated-device", codes(report))
        # not also reported as unreachable; the isolated finding is more precise
        self.assertNotIn("unreachable", codes(report))


class ReportTests(unittest.TestCase):
    def test_errors_are_listed_before_warnings(self):
        network = Network()
        feeder = network.add_feeder("FDR-1", voltage="13.8kV", substation="MISSING")
        feeder.add_breaker("B")
        network.add(Device(mrid="ORPHAN"))

        report = validate(network)
        lines = [line for line in report.summary().splitlines() if line.strip()]
        self.assertIn("error", lines[1])
        self.assertTrue(lines[-1].startswith("warning"))

    def test_counts_read_naturally(self):
        network = Network()
        network.add_feeder("FDR-1", voltage="13.8kV")
        self.assertEqual(validate(network).counts(), "0 errors, 1 warning")

    def test_findings_name_the_objects_involved(self):
        network = Network()
        network.add(Device(mrid="ORPHAN"))
        finding = validate(network).warnings[0]
        self.assertIn("ORPHAN", finding.objects)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def save(self, network, name):
        path = self.directory / name
        save_network(network, path)
        return str(path)

    def test_a_clean_model_exits_zero(self):
        code, out, _ = self.run_cli(["validate"])
        self.assertEqual(code, 0)
        self.assertIn("no problems found", out)

    def test_errors_exit_non_zero(self):
        network = build_sample_network()
        network.get("SW-001").state = "AJAR"
        code, out, _ = self.run_cli(["validate", "--db", self.save(network, "bad.sqlite")])
        self.assertEqual(code, 1)
        self.assertIn("invalid-state", out)

    def test_warnings_alone_exit_zero(self):
        network = build_sample_network()
        network.connect("TIE-001", "REC-001")  # closes a loop
        path = self.save(network, "warn.sqlite")
        code, out, _ = self.run_cli(["validate", "--db", path])
        self.assertEqual(code, 0)
        self.assertIn("loop", out)

    def test_strict_fails_on_warnings(self):
        network = build_sample_network()
        network.connect("TIE-001", "REC-001")
        path = self.save(network, "warn.sqlite")
        self.assertEqual(self.run_cli(["validate", "--db", path, "--strict"])[0], 1)

    def test_a_missing_database_exits_non_zero(self):
        code, _, err = self.run_cli(["validate", "--db", str(self.directory / "nope.sqlite")])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_importing_cim_reports_validation(self):
        xml = self.directory / "grid.xml"
        self.run_cli(["export-cim", str(xml)])
        code, out, _ = self.run_cli(["import-cim", str(xml)])
        self.assertEqual(code, 0)
        self.assertIn("validation: no problems found", out)


if __name__ == "__main__":
    unittest.main()
