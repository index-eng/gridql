"""OpenDSS import: the script language, and what a model becomes."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from gridql import execute, load_network, validate
from gridql.cim import export_network
from gridql.cim.importer import loads_cim
from gridql.cli import main
from gridql.dss import DssParseError, loads_dss, read_dss
from gridql.dss.parser import array, loads_script, number, read_script

try:
    from isolation import sample_project
except ImportError:  # run as tests.<module> rather than by discovery
    from .isolation import sample_project


def setUpModule():
    unittest.enterModuleContext(sample_project())


# A feeder that uses most of what OpenDSS models do:
#
#   source - SrcZ - Sub - Brk1 - Main -+- Reg(bank) - Rec -+- Lat - FuseLine - Svc - Tpx - House
#                                      +- Delta            +- Big, Cap3, Tie, Spare, Ctl, Pos, Stub
#                                                          +- (Lat) CapOff
MODEL = r"""
Clear
New Circuit.Demo basekv=115 bus1=src pu=1.0   ! the source
Set voltagebases=[115 12.47 0.208]

/* the substation, reached through
   the source impedance */
New Reactor.SrcZ bus1=src bus2=hv x=(1 2 +)
New Transformer.Sub phases=3 windings=2 sub=yes subname=Station
~ wdg=1 bus=hv conn=delta kv=115 kva=10000
~ wdg=2 bus=lv conn=wye kv=12.47 kva=10000

New Line.Brk1 bus1=lv bus2=b1 switch=y
New Relay.R1 MonitoredObj=Line.Brk1

New LineCode.lc3 nphases=3 units=kft normamps=400
New Line.Main bus1=b1.1.2.3 bus2=b2.1.2.3 linecode=lc3 length=2
New Load.Delta phases=1 bus1=b2.2.3 conn=delta kv=12.47 kW=50 kvar=20

New Transformer.RegA phases=1 bank=Reg buses=[b2.1 b3.1] kvs=[7.2 7.2] kvas=[500 500]
New Transformer.RegB like=RegA buses=[b2.2 b3.2]
New Transformer.RegC like=RegA buses=[b2.3 b3.3]
New RegControl.RegA transformer=RegA winding=2 vreg=122

New Line.Rec bus1=b3 bus2=b4 switch=y
New Recloser.Rc MonitoredObj=Line.Rec
New Load.Big bus1=b4 kW=300 kvar=100
New Capacitor.Cap3 bus1=b4 kvar=[300 300] kv=12.47

New Line.Lat phases=1 bus1=b4.2 bus2=b5.2 length=500 units=ft
New Capacitor.CapOff bus1=b5.2 phases=1 kvar=100 kv=7.2 states=[0]
New Line.FuseLine phases=1 bus1=b5.2 bus2=b6.2 switch=y
New Fuse.F1 MonitoredObj=Line.FuseLine SwitchedObj=Line.FuseLine

New XfmrCode.CT25 phases=1 windings=3 kvs=[7.2 0.12 0.12] kvas=[25 25 25]
New Transformer.Svc XfmrCode=CT25 buses=[b6.2 sec.1.0 sec.0.2]
New Line.Tpx phases=2 bus1=sec.1.2 bus2=house.1.2 length=100 units=ft
New Load.House phases=2 bus1=house.1.2 kv=0.208 kW=5 pf=0.9
New PVSystem.House phases=2 bus1=house.1.2 kv=0.208 kVA=5

New Line.Tie bus1=b4 bus2=other switch=y
Open Line.Tie 1
New Line.Spare bus1=b4 bus2=spare switch=y enabled=no
New Line.Ctl bus1=b4 bus2=ctl switch=y
New SwtControl.S1 SwitchedObj=Line.Ctl Normal=open State=closed
New Load.Gone bus1=b4 kW=1 enabled=false

New Line.Pos b4 pos linecode=lc3 length=1
New Line.Stub bus1=b4 bus2=stub length=10
Edit Line.Pos length=3
Line.Pos.normamps=250

Solve
"""


class ParserTests(unittest.TestCase):
    def test_comments_of_every_kind_are_ignored(self):
        script = loads_script(
            "New Circuit.A ! a comment\n// another\n/* a\nblock */ New Load.L bus1=x kw=1\n"
        )
        self.assertIsNotNone(script.find("load", "l"))

    def test_a_continuation_line_edits_the_object_before_it(self):
        script = loads_script("New Circuit.A\nNew Load.L bus1=x\n~ kw=7\nmore kvar=2")
        self.assertEqual(script.find("load", "L").properties["kw"], "7")
        self.assertEqual(script.find("load", "L").properties["kvar"], "2")

    def test_names_and_classes_ignore_case(self):
        script = loads_script("new circuit.A\nNEW LOAD.Big BUS1=x\nedit load.BIG kW=3")
        self.assertEqual(script.find("Load", "big").properties["kw"], "3")
        self.assertEqual(script.find("load", "big").name, "Big")

    def test_new_circuit_makes_its_source(self):
        script = loads_script("New object=circuit.Grid basekv=69")
        self.assertEqual(script.circuit, "Grid")
        self.assertEqual(script.find("vsource", "source").properties["basekv"], "69")

    def test_values_by_position_follow_the_class_order(self):
        script = loads_script("New Circuit.A\nNew Line.L a b lc 5\nNew Line.M bus1=a r1=1 2 3")
        line = script.find("line", "L").properties
        self.assertEqual((line["bus1"], line["bus2"], line["linecode"], line["length"]),
                         ("a", "b", "lc", "5"))
        self.assertEqual(script.find("line", "M").properties["r0"], "3")  # r1, x1, r0

    def test_arrays_in_every_bracket(self):
        for written in ("[1 2 3]", "(1, 2, 3)", '"1 2 3"', "{1 2 3}", "[1 | 2 3]"):
            with self.subTest(written=written):
                self.assertEqual(array(written), ["1", "2", "3"])

    def test_numbers_may_be_reverse_polish(self):
        self.assertEqual(number("(.5 1000 /)"), 0.0005)
        self.assertAlmostEqual(number("(1.051 0.88 0.001 3 * - - 115 12.47 / sqr *)"), 14.7983, 4)
        self.assertIsNone(number("(1 +)"))

    def test_an_assignment_is_an_edit(self):
        script = loads_script("New Circuit.A\nNew Line.L bus1=a bus2=b\nLine.L.length=9")
        self.assertEqual(script.find("line", "L").properties["length"], "9")

    def test_like_copies_then_overrides(self):
        script = loads_script("New Circuit.A\nNew Load.A bus1=x kw=4 kvar=1\n"
                              "New Load.B like=A kvar=2")
        self.assertEqual(script.find("load", "B").properties["kw"], "4")
        self.assertEqual(script.find("load", "B").properties["kvar"], "2")

    def test_transformer_windings_by_array_and_by_wdg(self):
        script = loads_script("New Circuit.A\nNew Transformer.T buses=[a b] kvs=[12.47 0.48]\n"
                              "~ wdg=2 kva=75")
        windings = script.find("transformer", "T").windings
        self.assertEqual(windings[1], {"bus": "a", "kv": "12.47"})
        self.assertEqual(windings[2], {"bus": "b", "kv": "0.48", "kva": "75"})

    def test_redirects_resolve_beside_the_file_whatever_the_case_or_slash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parts").mkdir()
            (root / "parts" / "Loads.DSS").write_text("New Load.L bus1=x kw=1\n")
            (root / "Master.dss").write_text("New Circuit.A\nRedirect parts\\loads.dss\n")
            script = read_script(root / "Master.dss")
        self.assertIsNotNone(script.find("load", "L"))
        self.assertEqual(len(script.files), 2)

    def test_open_and_close_record_a_position(self):
        script = loads_script("New Circuit.A\nNew Line.S bus1=a bus2=b switch=y\n"
                              "Open Line.S 1\nClose line.s term=1\nOpen Line.S")
        self.assertEqual(script.positions[("line", "s")], "OPEN")

    def test_commands_that_define_nothing_are_counted(self):
        script = loads_script("New Circuit.A\nSolve\nsolve\nShow voltages\ncalcv")
        self.assertEqual(script.skipped, {"solve": 2, "show": 1, "calcv": 1})

    def test_an_error_says_where(self):
        with self.assertRaisesRegex(DssParseError, r"line 2: 'Line.Nope' is not defined"):
            loads_script("New Circuit.A\nEdit Line.Nope length=1")

    def test_a_missing_redirect_is_an_error(self):
        with self.assertRaisesRegex(DssParseError, "no such file"):
            loads_script("Redirect nowhere.dss")


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = loads_dss(MODEL)
        cls.network = cls.document.network

    def get(self, name):
        return self.network.get(name)

    def names(self, query):
        return [row["name"] for row in execute(self.network, query + " SELECT name").rows()]

    def test_the_circuit_is_a_feeder_headed_by_its_source(self):
        feeder = self.get("Demo")
        self.assertEqual(feeder.TYPE, "feeder")
        self.assertEqual(feeder.head, "source")
        self.assertEqual(self.get("source").attribute("cim_class"), "EnergySource")

    def test_a_sub_transformer_names_the_substation(self):
        self.assertEqual(self.get("Station").TYPE, "substation")
        self.assertEqual(self.get("Demo").substation, "Station")
        self.assertEqual(self.get("Main").substation, "Station")

    def test_a_substation_named_for_its_transformer_gives_way_to_it(self):
        document = loads_dss("New Circuit.A\nNew Transformer.T sub=y buses=[sourcebus lv]")
        self.assertEqual(document.network.objects["T"].TYPE, "transformer")
        self.assertEqual(document.network.objects["substation.T"].name, "T")

    def test_the_path_from_a_house_to_the_source(self):
        self.assertEqual(
            self.names('FIND devices UPSTREAM OF "House"'),
            ["Tpx", "Svc", "FuseLine", "Lat", "Rec", "Reg", "Main", "Brk1", "Sub", "SrcZ",
             "source"],
        )

    def test_a_series_reactor_keeps_the_circuit_whole(self):
        self.assertEqual(self.get("SrcZ").attribute("cim_class"), "SeriesCompensator")
        self.assertEqual(self.names("FIND devices WHERE NOT energized"), [])

    def test_protection_decides_what_a_switch_is(self):
        kinds = {name: self.get(name).TYPE for name in ("Brk1", "Rec", "FuseLine", "Tie")}
        self.assertEqual(kinds, {"Brk1": "breaker", "Rec": "recloser", "FuseLine": "fuse",
                                 "Tie": "switch"})

    def test_switch_positions(self):
        positions = {name: (self.get(name).normal_state, self.get(name).state)
                     for name in ("Tie", "Spare", "Ctl", "Rec")}
        self.assertEqual(positions, {
            "Tie": ("OPEN", "OPEN"),  # opened by a command
            "Spare": ("OPEN", "OPEN"),  # disabled
            "Ctl": ("OPEN", "CLOSED"),  # a SwtControl says both
            "Rec": ("CLOSED", "CLOSED"),
        })

    def test_a_bank_is_one_transformer(self):
        regulator = self.get("Reg")
        self.assertEqual((regulator.kva, regulator.phases, regulator.voltage,
                          regulator.primary_voltage), (1500.0, "ABC", 12.47, 7.2))
        self.assertEqual(self.names('FIND transformers WHERE name IN (RegA, RegB, RegC)'), [])

    def test_voltages_come_from_the_nearest_base(self):
        voltages = {name: self.get(name).voltage for name in
                    ("source", "SrcZ", "Sub", "Main", "Rec", "Svc", "Tpx", "House")}
        self.assertEqual(voltages, {"source": 115.0, "SrcZ": 115.0, "Sub": 115.0,
                                    "Main": 12.47, "Rec": 12.47, "Svc": 12.47,
                                    "Tpx": 0.208, "House": 0.208})

    def test_phases_come_from_the_nodes(self):
        phases = {name: self.get(name).phases for name in
                  ("Main", "Lat", "FuseLine", "CapOff", "Delta", "Big", "Svc")}
        self.assertEqual(phases, {"Main": "ABC", "Lat": "B", "FuseLine": "B", "CapOff": "B",
                                  "Delta": "BC", "Big": "ABC", "Svc": "B"})

    def test_a_center_tapped_secondary_is_split_phase(self):
        self.assertEqual(self.get("Tpx").phases, "s1s2")
        self.assertEqual(self.get("House").phases, "s1s2")

    def test_line_lengths_are_in_feet_from_whichever_units_apply(self):
        self.assertEqual(self.get("Main").length, 2000.0)  # the linecode's kft
        self.assertEqual(self.get("Lat").length, 500.0)
        self.assertEqual(self.get("Pos").length, 3000.0)  # edited after it was made
        self.assertIsNone(self.get("Stub").length)  # no units anywhere: not a length

    def test_ampacity_and_conductor(self):
        self.assertEqual((self.get("Main").ampacity, self.get("Main").conductor), (400.0, "lc3"))
        self.assertEqual(self.get("Pos").ampacity, 250.0)

    def test_load_reactive_power_follows_the_power_factor(self):
        self.assertEqual((self.get("Big").kw, self.get("Big").kvar), (300.0, 100.0))
        self.assertEqual((self.get("House").kw, self.get("House").kvar), (5.0, 2.422))

    def test_capacitors(self):
        self.assertEqual((self.get("Cap3").kvar, self.get("Cap3").state), (600.0, "CLOSED"))
        self.assertEqual((self.get("CapOff").kvar, self.get("CapOff").state), (100.0, "OPEN"))

    def test_a_disabled_load_is_left_out_and_said_so(self):
        self.assertNotIn("Gone", self.network)
        self.assertTrue(any("1 disabled element" in n for n in self.document.report.notes))

    def test_two_devices_sharing_a_name_are_both_kept(self):
        self.assertEqual(self.network.objects["House"].TYPE, "load")
        self.assertEqual(self.network.objects["pvsystem.House"].extras["dss_class"], "pvsystem")

    def test_what_was_not_acted_on_is_reported(self):
        self.assertTrue(any("solve x1" in n for n in self.document.report.notes))

    def test_it_validates_clean(self):
        self.assertEqual(list(validate(self.network)), [])

    def test_it_exports_to_cim_and_back(self):
        document = export_network(self.network)
        self.assertIn("cim:EnergySource", document)
        again = loads_cim(document).network
        self.assertEqual({o.mrid: o.to_row() for o in again},
                         {o.mrid: o.to_row() for o in self.network})


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_import_dss_saves_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "Master.dss"
            model.write_text(MODEL)
            db = Path(directory) / "demo.sqlite"
            code, out, err = self.run_cli(["import-dss", str(model), "--db", str(db)])
            self.assertEqual(code, 0, err)
            self.assertIn("imported", out)
            self.assertIn("validation: no problems found", out)
            network = load_network(str(db))
        self.assertEqual(network.get("Reg").kva, 1500.0)
        self.assertEqual(network.get("House").phases, "s1s2")

    def test_import_dss_reports_a_missing_file(self):
        code, _, err = self.run_cli(["import-dss", "nowhere.dss"])
        self.assertEqual(code, 1)
        self.assertIn("error: cannot read nowhere.dss", err)

    def test_read_dss_is_public(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "m.dss"
            model.write_text("New Circuit.A\nNew Load.L kw=1")
            self.assertEqual(read_dss(model).network.get("L").kw, 1.0)


if __name__ == "__main__":
    unittest.main()
