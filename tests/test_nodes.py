"""Connectivity recorded as nodes: devices joined where their nodes match."""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from gridql import Network, execute, validate
from gridql.cim import export_network
from gridql.cim.importer import loads_cim
from gridql.cim.vocabulary import CIM_NS, RDF_NS
from gridql.cli import main
from gridql.errors import GridQLNameError
from gridql.ingest import read_csv, write_csv
from gridql.storage import load_network, save_network

#       N0     N1     N2 --L2-- N3 --X1-- N5 --LD1
#   BRK -- L1 ----------|
#                       +--L3-- N4 --SW1-- N6 --LD2
#
# Every device names the nodes at its ends. N2 is a three-way branch point.
TERMINALS = {
    "BRK": ("N0", "N1"),
    "L1": ("N1", "N2"),
    "L2": ("N2", "N3"),
    "L3": ("N2", "N4"),
    "X1": ("N3", "N5"),
    "SW1": ("N4", "N6"),
    "LD1": ("N5",),
    "LD2": ("N6",),
}

DEVICES_CSV = """
mrid,type,feeder,kva,state,normal_state,from_node,to_node
BRK,breaker,F1,,CLOSED,CLOSED,N0,N1
L1,line,F1,,,,N1,N2
L2,line,F1,,,,N2,N3
L3,line,F1,,,,N2,N4
X1,transformer,F1,50,,,N3,N5
SW1,switch,F1,,OPEN,CLOSED,N4,N6
LD1,load,F1,,,,N5,
LD2,load,F1,,,,N6,
"""


def codes(report):
    return {finding.code for finding in report}


def build(terminals=TERMINALS, head="BRK"):
    network = Network()
    feeder = network.add_feeder("F1", voltage="12.47kV")
    kinds = {"BRK": feeder.add_breaker, "SW1": feeder.add_switch, "X1": feeder.add_transformer,
             "LD1": feeder.add_load, "LD2": feeder.add_load}
    for mrid in terminals:
        kinds.get(mrid, feeder.add_line)(mrid, after=None)
    feeder.head = head
    for mrid, nodes in terminals.items():
        for node in nodes:
            network.attach(mrid, node)
    return network


def mrids(objects):
    return [obj.mrid for obj in objects]


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.network = build()

    def test_devices_on_one_node_are_connected(self):
        self.assertEqual(sorted(self.network.neighbors("L1")), ["BRK", "L2", "L3"])
        self.assertEqual(sorted(self.network.neighbors("L2")), ["L1", "L3", "X1"])

    def test_a_branch_point_is_not_a_loop(self):
        self.assertEqual(self.network.topology().loop_edges, [])
        self.assertNotIn("loop", codes(validate(self.network)))

    def test_both_branches_hang_off_the_device_feeding_the_node(self):
        topology = self.network.topology()
        self.assertEqual(topology.parent["L2"], "L1")
        self.assertEqual(topology.parent["L3"], "L1")
        self.assertEqual(topology.depth["L2"], topology.depth["L3"])

    def test_downstream_and_upstream_follow_the_nodes(self):
        self.assertEqual(
            mrids(self.network.downstream_of("L1")), ["L2", "L3", "SW1", "X1", "LD1", "LD2"]
        )
        self.assertEqual(mrids(self.network.downstream_of("L3")), ["SW1", "LD2"])
        self.assertEqual(mrids(self.network.upstream_of("LD1")), ["X1", "L2", "L1", "BRK"])

    def test_siblings_on_a_node_are_connected_to_each_other(self):
        self.assertIn("L3", mrids(self.network.connected_to("L2")))

    def test_an_open_switch_still_blocks_energisation(self):
        self.network.get("SW1").state = "OPEN"
        self.assertTrue(self.network.is_energized("SW1"))
        self.assertFalse(self.network.is_energized("LD2"))
        self.assertTrue(self.network.is_energized("LD1"))

    def test_a_ring_of_three_devices_is_a_loop(self):
        # Three devices each sharing a different node with the next: the same
        # pairs of neighbours as a branch point, but a real loop.
        network = build({
            "BRK": ("N0", "A"), "L1": ("A", "B"), "L2": ("B", "C"), "L3": ("C", "A"),
        })
        self.assertIn("loop", codes(validate(network)))

    def test_parallel_devices_between_the_same_nodes_are_a_loop(self):
        network = build({
            "BRK": ("N0", "N1"), "L1": ("N1", "N2"), "L2": ("N1", "N2"), "L3": ("N2", "N3"),
        })
        self.assertEqual(network.topology().loop_edges, [("L1", "L2")])

    def test_a_plain_connection_alongside_a_shared_node_adds_nothing(self):
        self.network.connect("L2", "L3")
        self.assertEqual(self.network.topology().loop_edges, [])
        self.assertEqual(self.network.links(), [])

    def test_nodes_and_plain_connections_mix(self):
        feeder = self.network.objects["F1"]
        feeder.add_load("LD3", after="LD2")
        self.assertEqual(self.network.links(), [("LD2", "LD3")])
        self.assertEqual(mrids(self.network.downstream_of("SW1")), ["LD2", "LD3"])
        self.assertEqual(self.network.topology().loop_edges, [])

    def test_what_is_recorded_can_be_read_back(self):
        self.assertEqual(self.network.nodes_of("L2"), ["N2", "N3"])
        self.assertEqual(sorted(self.network.at_node("N2")), ["L1", "L2", "L3"])
        self.assertEqual(len(self.network.nodes), 7)

    def test_attaching_twice_changes_nothing(self):
        self.network.attach("L2", "N2")
        self.assertEqual(self.network.nodes_of("L2"), ["N2", "N3"])

    def test_only_equipment_has_terminals(self):
        with self.assertRaises(GridQLNameError):
            self.network.attach("NOPE", "N1")
        with self.assertRaises(ValueError):
            self.network.attach("F1", "N1")

    def test_a_query_reads_the_same_as_with_plain_connections(self):
        result = execute(self.network, 'FIND devices DOWNSTREAM OF "L1" WHERE hops = 1')
        self.assertEqual(result.mrids, ["L2", "L3"])


class CsvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text.lstrip("\n"), encoding="utf-8")
        return path

    def test_devices_are_joined_where_their_nodes_match(self):
        self.write("devices.csv", DEVICES_CSV)
        document = read_csv(self.directory)
        self.assertEqual(document.report.problems, [])
        network = document.network
        self.assertEqual(sorted(network.neighbors("L1")), ["BRK", "L2", "L3"])
        self.assertEqual(mrids(network.downstream_of("L3")), ["SW1", "LD2"])
        self.assertNotIn("loop", codes(validate(network)))

    def test_the_report_counts_the_nodes(self):
        self.write("devices.csv", DEVICES_CSV)
        report = read_csv(self.directory).report
        self.assertIn("8 connections through 7 nodes", report.counts())

    def test_row_order_does_not_matter(self):
        header, *rows = DEVICES_CSV.strip().splitlines()
        self.write("devices.csv", "\n".join([header, *reversed(rows)]) + "\n")
        network = read_csv(self.directory).network
        self.assertEqual(mrids(network.downstream_of("L3")), ["SW1", "LD2"])

    def test_common_spellings_are_recognised(self):
        for first, second in (
            ("FROM_NODE", "TO_NODE"), ("FromNodeId", "ToNodeId"), ("Bus1", "Bus2"),
            ("From Bus", "To Bus"), ("node1", "node2"),
        ):
            with self.subTest(columns=(first, second)):
                text = DEVICES_CSV.replace("from_node,to_node", f"{first},{second}")
                self.write("devices.csv", text)
                network = read_csv(self.directory).network
                self.assertEqual(sorted(network.neighbors("L1")), ["BRK", "L2", "L3"])
                self.assertEqual(network.objects["L1"].extras, {})

    def test_a_single_node_column_is_a_one_terminal_device(self):
        self.write("devices.csv", """
mrid,type,feeder,node
BRK,breaker,F1,N1
LD1,load,F1,N1
""")
        network = read_csv(self.directory).network
        self.assertEqual(sorted(network.neighbors("BRK")), ["LD1"])

    def test_a_device_with_both_ends_on_one_node_is_reported(self):
        self.write("devices.csv", """
mrid,type,feeder,from_node,to_node
BRK,breaker,F1,N0,N1
L1,line,F1,N1,N1
""")
        document = read_csv(self.directory)
        self.assertEqual(len(document.report.problems), 1)
        self.assertIn("both ends are on node 'N1'", document.report.problems[0])
        self.assertEqual(document.network.nodes_of("L1"), ["N1"])

    def test_a_skipped_row_attaches_nothing(self):
        self.write("devices.csv", DEVICES_CSV + "L1,line,F1,,,,N1,N9\n")
        network = read_csv(self.directory).network
        self.assertEqual(network.nodes_of("L1"), ["N1", "N2"])
        self.assertNotIn("N9", network.nodes)

    def test_nodes_and_a_connections_file_together(self):
        self.write("devices.csv", DEVICES_CSV + "LD3,load,F1,,,,,\n")
        self.write("connections.csv", "from_device,to_device\nLD2,LD3\n")
        network = read_csv(self.directory).network
        self.assertEqual(mrids(network.downstream_of("SW1")), ["LD2", "LD3"])

    def test_written_and_read_back_keeps_the_nodes(self):
        self.write("devices.csv", DEVICES_CSV)
        original = read_csv(self.directory).network
        out = self.directory / "out"
        write_csv(original, out)
        back = read_csv(out).network
        self.assertEqual(back.nodes, original.nodes)
        self.assertEqual(
            {m: sorted(back.neighbors(m)) for m in back.objects},
            {m: sorted(original.neighbors(m)) for m in original.objects},
        )
        header = (out / "devices.csv").read_text().splitlines()[0]
        self.assertIn("from_node,to_node", header)

    def test_connections_the_nodes_already_give_are_not_written_again(self):
        self.write("devices.csv", DEVICES_CSV + "LD3,load,F1,,,,,\n")
        self.write("connections.csv", "from_device,to_device\nLD2,LD3\n")
        out = self.directory / "out"
        write_csv(read_csv(self.directory).network, out)
        self.assertEqual(
            (out / "connections.csv").read_text().splitlines(),
            ["from_device,to_device", "LD2,LD3"],
        )

    def test_a_device_on_a_third_node_keeps_those_connections_as_pairs(self):
        network = build()
        network.attach("X1", "N7")      # a tertiary winding
        feeder = network.objects["F1"]
        feeder.add_load("LD3", after=None)
        network.attach("LD3", "N7")
        write_csv(network, self.directory)
        back = read_csv(self.directory).network
        self.assertEqual(back.neighbors("LD3"), {"X1"})
        self.assertEqual(back.nodes_of("X1"), ["N3", "N5"])

    def test_a_network_without_nodes_writes_no_node_columns(self):
        network = Network()
        feeder = network.add_feeder("F1")
        feeder.add_breaker("BRK")
        feeder.add_line("L1")
        write_csv(network, self.directory)
        self.assertNotIn("node", (self.directory / "devices.csv").read_text())

    def test_a_query_straight_from_the_files(self):
        self.write("devices.csv", DEVICES_CSV)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main([
                "--csv", str(self.directory), "--no-config", "--format", "csv",
                'FIND devices DOWNSTREAM OF "L1" WHERE NOT energized SELECT mRID',
            ])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().split(), ["mRID", "LD2"])


class MappingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        (self.directory / "EQUIP.csv").write_text(
            "FACILITY_ID,KIND,CIRCUIT,FNODE,TNODE\n"
            "BRK,BKR,1,100,101\n"
            "L1,OH,1,101,102\n"
            "L2,OH,1,102,103\n"
            "L3,OH,1,102,104\n",
            encoding="utf-8",
        )

    def mapping(self, nodes):
        path = self.directory / "mapping.toml"
        path.write_text(
            '[[devices]]\n'
            'file = "EQUIP.csv"\n'
            'mrid = "FACILITY_ID"\n'
            'feeder = "FDR-{CIRCUIT}"\n'
            'type = { column = "KIND", values = { BKR = "breaker", OH = "line" } }\n'
            + nodes,
            encoding="utf-8",
        )
        return path

    def test_a_mapping_names_the_node_columns(self):
        mapping = self.mapping('from_node = "N-{FNODE}"\nto_node = "N-{TNODE}"\n')
        network = read_csv(self.directory, mapping).network
        self.assertEqual(network.nodes_of("L1"), ["N-101", "N-102"])
        self.assertEqual(sorted(network.neighbors("L1")), ["BRK", "L2", "L3"])
        self.assertNotIn("loop", codes(validate(network)))

    def test_unmapped_node_columns_are_kept_as_attributes_not_connectivity(self):
        network = read_csv(self.directory, self.mapping("")).network
        self.assertEqual(network.nodes, {})
        self.assertEqual(network.objects["L1"].extras, {"fnode": 101, "tnode": 102})


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "grid.sqlite"

    def test_nodes_survive_a_save_and_load(self):
        original = build()
        save_network(original, self.path)
        back = load_network(self.path)
        self.assertEqual(back.nodes, original.nodes)
        self.assertEqual(back.nodes_of("L2"), ["N2", "N3"])
        self.assertEqual(back.topology().loop_edges, [])

    def test_a_database_from_before_nodes_still_loads(self):
        save_network(build(), self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE terminals")
        back = load_network(self.path)
        self.assertEqual(back.nodes, {})
        self.assertEqual(sorted(back.neighbors("L1")), ["BRK", "L2", "L3"])


class CimTests(unittest.TestCase):
    def setUp(self):
        self.network = build()
        self.document = export_network(self.network)
        self.root = ET.fromstring(self.document)

    def elements(self, cim_class):
        return self.root.findall(f"{{{CIM_NS}}}{cim_class}")

    def test_a_node_is_written_as_one_connectivity_node(self):
        self.assertEqual(len(self.elements("ConnectivityNode")), 7)
        branch = [
            terminal for terminal in self.elements("Terminal")
            if terminal.find(f"{{{CIM_NS}}}Terminal.ConnectivityNode").get(f"{{{RDF_NS}}}resource")
            == "#CN_N2"
        ]
        self.assertEqual(len(branch), 3)

    def test_a_round_trip_keeps_the_nodes(self):
        back = loads_cim(self.document).network
        self.assertEqual(back.nodes, self.network.nodes)
        self.assertEqual(back.topology().loop_edges, [])

    def test_exporting_an_imported_network_again_keeps_the_node_ids(self):
        again = export_network(loads_cim(self.document).network)
        ids = sorted(
            element.get(f"{{{RDF_NS}}}ID")
            for element in ET.fromstring(again).findall(f"{{{CIM_NS}}}ConnectivityNode")
        )
        self.assertEqual(ids, sorted(f"CN_{node}" for node in self.network.nodes))

    def test_numeric_node_ids_do_not_collide_with_equipment(self):
        network = Network()
        feeder = network.add_feeder("F1")
        feeder.add_breaker("1001", after=None)
        feeder.add_line("1002", after=None)
        network.attach("1001", "1002")
        network.attach("1002", "1002")
        back = loads_cim(export_network(network)).network
        self.assertEqual(back.nodes, {"1002": ["1001", "1002"]})

    def test_a_slice_keeps_the_nodes_its_devices_are_on(self):
        document = export_network(self.network, [self.network.get("L2")])
        back = loads_cim(document).network
        self.assertEqual(back.nodes_of("L2"), ["N2", "N3"])
        self.assertEqual(back.neighbors("L2"), set())


if __name__ == "__main__":
    unittest.main()
