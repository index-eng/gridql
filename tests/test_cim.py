"""CIM import and export: document structure, fidelity, and foreign files."""

import contextlib
import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from gridql import Network, build_sample_network, execute, render
from gridql.cim import export_network, export_summary, read_cim
from gridql.cim.importer import CimImportError, loads_cim
from gridql.cim.vocabulary import CIM_NS, GRIDQL_NS, RDF_NS
from gridql.cli import main
from gridql.model import Switch

RDF = f"{{{RDF_NS}}}"
CIM = f"{{{CIM_NS}}}"
GRIDQL = f"{{{GRIDQL_NS}}}"


def snapshot(network):
    return (
        {o.mrid: (type(o).__name__, dataclasses.asdict(o)) for o in network.objects.values()},
        {mrid: sorted(network.neighbors(mrid)) for mrid in network.objects},
        {feeder.mrid: feeder.head for feeder in network.feeders},
    )


def parse(document):
    return ET.fromstring(document)


def elements(document, cim_class):
    return parse(document).findall(f"{CIM}{cim_class}")


def find(document, cim_class, identifier):
    for element in elements(document, cim_class):
        if element.get(f"{RDF}ID") == identifier:
            return element
    raise AssertionError(f"no {cim_class} with rdf:ID {identifier}")


def text(element, tag):
    found = element.find(tag)
    return None if found is None else found.text


FOREIGN = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cim="http://iec.ch/TC57/2013/CIM-schema-cim16#">
  <cim:Substation rdf:ID="ST_A">
    <cim:IdentifiedObject.name>Alpha</cim:IdentifiedObject.name>
  </cim:Substation>
  <cim:Feeder rdf:ID="FDR_A">
    <cim:IdentifiedObject.name>Alpha 1</cim:IdentifiedObject.name>
    <cim:Feeder.NormalEnergizingSubstation rdf:resource="#ST_A"/>
  </cim:Feeder>
  <cim:BaseVoltage rdf:ID="BV1">
    <cim:BaseVoltage.nominalVoltage>12470</cim:BaseVoltage.nominalVoltage>
  </cim:BaseVoltage>
  <cim:Breaker rdf:ID="BKR1">
    <cim:IdentifiedObject.name>Alpha Breaker</cim:IdentifiedObject.name>
    <cim:Equipment.EquipmentContainer rdf:resource="#FDR_A"/>
    <cim:ConductingEquipment.BaseVoltage rdf:resource="#BV1"/>
    <cim:Switch.normalOpen>false</cim:Switch.normalOpen>
  </cim:Breaker>
  <cim:LoadBreakSwitch rdf:ID="LBS1">
    <cim:Equipment.EquipmentContainer rdf:resource="#FDR_A"/>
    <cim:Switch.normalOpen>false</cim:Switch.normalOpen>
    <cim:Switch.open>true</cim:Switch.open>
  </cim:LoadBreakSwitch>
  <cim:Disconnector rdf:ID="DISC1">
    <cim:Equipment.EquipmentContainer rdf:resource="#FDR_A"/>
    <cim:Switch.normalOpen>true</cim:Switch.normalOpen>
  </cim:Disconnector>
  <cim:ConformLoad rdf:ID="LD1">
    <cim:Equipment.EquipmentContainer rdf:resource="#FDR_A"/>
    <cim:EnergyConsumer.p>25000</cim:EnergyConsumer.p>
    <cim:EnergyConsumer.q>8000</cim:EnergyConsumer.q>
  </cim:ConformLoad>
  <cim:SynchronousMachine rdf:ID="GEN1">
    <cim:IdentifiedObject.name>not modelled</cim:IdentifiedObject.name>
  </cim:SynchronousMachine>
  <cim:ConnectivityNode rdf:ID="CN1"/>
  <cim:ConnectivityNode rdf:ID="CN2"/>
  <cim:Terminal rdf:ID="T1">
    <cim:Terminal.ConductingEquipment rdf:resource="#BKR1"/>
    <cim:Terminal.ConnectivityNode rdf:resource="#CN1"/>
  </cim:Terminal>
  <cim:Terminal rdf:ID="T2">
    <cim:Terminal.ConductingEquipment rdf:resource="#LBS1"/>
    <cim:Terminal.ConnectivityNode rdf:resource="#CN1"/>
  </cim:Terminal>
  <cim:Terminal rdf:ID="T3">
    <cim:Terminal.ConductingEquipment rdf:resource="#DISC1"/>
    <cim:Terminal.ConnectivityNode rdf:resource="#CN1"/>
  </cim:Terminal>
  <cim:Terminal rdf:ID="T4">
    <cim:Terminal.ConductingEquipment rdf:resource="#LBS1"/>
    <cim:Terminal.ConnectivityNode rdf:resource="#CN2"/>
  </cim:Terminal>
  <cim:Terminal rdf:ID="T5">
    <cim:Terminal.ConductingEquipment rdf:resource="#LD1"/>
    <cim:Terminal.ConnectivityNode rdf:resource="#CN2"/>
  </cim:Terminal>
</rdf:RDF>
"""


class ExportStructureTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()
        self.document = export_network(self.network)

    def test_the_document_is_rdf(self):
        self.assertEqual(parse(self.document).tag, f"{RDF}RDF")

    def test_equipment_uses_its_cim_class(self):
        for cim_class, identifier in (
            ("ProtectedSwitch", "REC-001"),
            ("Breaker", "BRK-001"),
            ("Switch", "SW-001"),
            ("PowerTransformer", "XFMR-001"),
            ("ACLineSegment", "LN-001"),
            ("EnergyConsumer", "LOAD-001"),
            ("Substation", "SUB-001"),
            ("Feeder", "FDR-104"),
        ):
            with self.subTest(cim_class=cim_class):
                find(self.document, cim_class, identifier)

    def test_every_object_carries_its_mrid(self):
        recloser = find(self.document, "ProtectedSwitch", "REC-001")
        self.assertEqual(text(recloser, f"{CIM}IdentifiedObject.mRID"), "REC-001")
        self.assertEqual(text(recloser, f"{CIM}IdentifiedObject.name"), "Mainline Recloser")

    def test_equipment_points_at_its_container(self):
        recloser = find(self.document, "ProtectedSwitch", "REC-001")
        reference = recloser.find(f"{CIM}Equipment.EquipmentContainer")
        self.assertEqual(reference.get(f"{RDF}resource"), "#FDR-104")

    def test_switch_state_uses_the_cim_booleans(self):
        lateral = find(self.document, "Switch", "SW-002")
        self.assertEqual(text(lateral, f"{CIM}Switch.normalOpen"), "false")
        self.assertEqual(text(lateral, f"{CIM}Switch.open"), "true")

        tie = find(self.document, "Switch", "TIE-001")
        self.assertEqual(text(tie, f"{CIM}Switch.normalOpen"), "true")
        self.assertEqual(text(tie, f"{GRIDQL}isTie"), "true")

    def test_voltages_become_base_voltage_objects_in_volts(self):
        nominal = {
            text(element, f"{CIM}BaseVoltage.nominalVoltage")
            for element in elements(self.document, "BaseVoltage")
        }
        self.assertEqual(nominal, {"13800", "480", "240"})

    def test_transformer_ratings_live_on_its_ends_in_si(self):
        primary = find(self.document, "PowerTransformerEnd", "XFMR-001_END_1")
        self.assertEqual(text(primary, f"{CIM}TransformerEnd.endNumber"), "1")
        self.assertEqual(text(primary, f"{CIM}PowerTransformerEnd.ratedS"), "500000")
        self.assertEqual(text(primary, f"{CIM}PowerTransformerEnd.ratedU"), "13800")

        secondary = find(self.document, "PowerTransformerEnd", "XFMR-001_END_2")
        self.assertEqual(text(secondary, f"{CIM}PowerTransformerEnd.ratedU"), "480")

    def test_line_length_is_written_in_metres(self):
        line = find(self.document, "ACLineSegment", "LN-001")
        self.assertEqual(text(line, f"{CIM}Conductor.length"), "731.52")  # 2400 ft

    def test_load_is_written_in_watts_and_vars(self):
        load = find(self.document, "EnergyConsumer", "LOAD-001")
        self.assertEqual(text(load, f"{CIM}EnergyConsumer.p"), "310000")
        self.assertEqual(text(load, f"{CIM}EnergyConsumer.q"), "95000")

    def test_connectivity_becomes_nodes_and_terminals(self):
        edges = sum(len(self.network.neighbors(m)) for m in self.network.objects) // 2
        self.assertEqual(len(elements(self.document, "ConnectivityNode")), edges)
        self.assertEqual(len(elements(self.document, "Terminal")), edges * 2)

    def test_each_terminal_joins_one_device_to_one_node(self):
        for terminal in elements(self.document, "Terminal"):
            self.assertIsNotNone(terminal.find(f"{CIM}Terminal.ConductingEquipment"))
            self.assertIsNotNone(terminal.find(f"{CIM}Terminal.ConnectivityNode"))

    def test_the_feeder_head_is_recorded_as_an_extension(self):
        feeder = find(self.document, "Feeder", "FDR-104")
        reference = feeder.find(f"{GRIDQL}headTerminalEquipment")
        self.assertEqual(reference.get(f"{RDF}resource"), "#BRK-001")

    def test_export_is_reproducible(self):
        self.assertEqual(export_network(build_sample_network()), self.document)

    def test_summary_matches_the_document(self):
        counts = export_summary(self.network)
        self.assertEqual(counts["devices"], 11)
        self.assertEqual(counts["connections"], len(elements(self.document, "ConnectivityNode")))


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_a_full_round_trip_changes_nothing(self):
        back = loads_cim(export_network(self.network)).network
        self.assertEqual(snapshot(back), snapshot(self.network))

    def test_queries_agree_across_a_round_trip(self):
        back = loads_cim(export_network(self.network)).network
        for source in (
            "FIND feeders",
            "FIND reclosers",
            'FIND devices DOWNSTREAM OF "REC-001"',
            "FIND transformers WHERE kva >= 500",
            "FIND switches WHERE state != normal_state",
            "FIND devices WHERE NOT energized",
        ):
            with self.subTest(query=source):
                self.assertEqual(
                    execute(back, source).mrids, execute(self.network, source).mrids
                )

    def test_extras_survive_in_the_extension_namespace(self):
        network = Network()
        feeder = network.add_feeder("FDR-9", voltage="12.47kV", extras={"owner": "East"})
        feeder.add_breaker("BRK-9", extras={"install_year": 1998})
        back = loads_cim(export_network(network)).network
        self.assertEqual(back.get("BRK-9").extras, {"install_year": 1998})
        self.assertEqual(back.get("FDR-9").extras, {"owner": "East"})

    def test_an_mrid_that_is_not_a_valid_xml_id_still_round_trips(self):
        network = Network()
        feeder = network.add_feeder("2024 Feeder/A", voltage="13.8kV")
        feeder.add_breaker("99 BRK#1")
        document = export_network(network)
        back = loads_cim(document).network
        self.assertEqual(sorted(back.objects), ["2024 Feeder/A", "99 BRK#1"])

    def test_an_empty_network_round_trips(self):
        self.assertEqual(len(loads_cim(export_network(Network())).network), 0)


class SubsetExportTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()

    def test_a_query_result_exports_only_what_it_selected(self):
        result = execute(self.network, "FIND transformers")
        document = export_network(self.network, result.objects)
        self.assertEqual(len(elements(document, "PowerTransformer")), 2)
        self.assertEqual(elements(document, "EnergyConsumer"), [])

    def test_a_slice_brings_its_containers_with_it(self):
        result = execute(self.network, "FIND transformers WHERE kva >= 500")
        document = export_network(self.network, result.objects)
        find(document, "Feeder", "FDR-104")
        find(document, "Substation", "SUB-001")

    def test_only_connections_inside_the_slice_are_written(self):
        result = execute(self.network, 'FIND devices FED BY "SW-002"')
        document = export_network(self.network, result.objects)
        # SW-002, XFMR-002, LOAD-002 -- two edges among them, and none to REC-001.
        self.assertEqual(len(elements(document, "ConnectivityNode")), 2)

    def test_a_slice_reimports_as_a_smaller_network(self):
        result = execute(self.network, 'FIND devices FED BY "SW-002"')
        back = loads_cim(export_network(self.network, result.objects)).network
        self.assertEqual(sorted(o.mrid for o in back.devices),
                         ["LOAD-002", "SW-002", "XFMR-002"])

    def test_cim_is_an_output_format(self):
        document = render(execute(self.network, "FIND reclosers RETURN cim"), "cim")
        find(document, "ProtectedSwitch", "REC-001")


class ForeignDocumentTests(unittest.TestCase):
    def setUp(self):
        self.document = loads_cim(FOREIGN)
        self.network = self.document.network

    def test_specialised_switch_classes_are_understood(self):
        self.assertIsInstance(self.network.get("LBS1"), Switch)
        self.assertIsInstance(self.network.get("DISC1"), Switch)
        self.assertEqual(type(self.network.get("BKR1")).__name__, "Breaker")

    def test_switch_states_come_from_the_cim_booleans(self):
        self.assertEqual(self.network.get("LBS1").normal_state, "CLOSED")
        self.assertEqual(self.network.get("LBS1").state, "OPEN")
        self.assertEqual(self.network.get("DISC1").normal_state, "OPEN")
        self.assertEqual(self.network.get("DISC1").state, "OPEN")

    def test_a_conform_load_becomes_a_load_in_kw(self):
        load = self.network.get("LD1")
        self.assertEqual(type(load).__name__, "Load")
        self.assertEqual(load.kw, 25.0)
        self.assertEqual(load.kvar, 8.0)

    def test_base_voltage_is_read_in_kv(self):
        self.assertEqual(self.network.get("BKR1").voltage, 12.47)

    def test_containers_are_linked_through(self):
        self.assertEqual(self.network.get("BKR1").feeder, "FDR_A")
        self.assertEqual(self.network.get("BKR1").substation, "ST_A")

    def test_a_bus_becomes_edges_between_every_pair_on_it(self):
        self.assertEqual(sorted(self.network.neighbors("BKR1")), ["DISC1", "LBS1"])
        self.assertEqual(sorted(self.network.neighbors("DISC1")), ["BKR1", "LBS1"])

    def test_the_bus_expansion_is_reported(self):
        self.assertTrue(
            any("more than two terminals" in note for note in self.document.report.notes)
        )

    def test_a_missing_head_is_inferred_from_the_only_breaker(self):
        self.assertEqual(self.network.feeders[0].head, "BKR1")
        self.assertTrue(any("inferred BKR1" in note for note in self.document.report.notes))

    def test_topology_works_on_the_imported_network(self):
        self.assertEqual(
            sorted(o.mrid for o in self.network.downstream_of("BKR1")),
            ["DISC1", "LBS1", "LD1"],
        )

    def test_unsupported_classes_are_reported_not_dropped_silently(self):
        self.assertEqual(self.document.report.ignored, {"SynchronousMachine": 1})

    def test_structural_classes_are_not_reported_as_ignored(self):
        for name in ("Terminal", "ConnectivityNode", "BaseVoltage"):
            self.assertNotIn(name, self.document.report.ignored)

    def test_the_summary_reads_as_a_report(self):
        summary = self.document.report.summary()
        self.assertIn("imported 4 devices", summary)
        self.assertIn("SynchronousMachine", summary)

    def test_a_feeder_with_no_inferable_head_says_so(self):
        document = loads_cim(FOREIGN.replace("cim:Breaker", "cim:LoadBreakSwitch"))
        self.assertIsNone(document.network.feeders[0].head)
        self.assertTrue(any("none could be inferred" in n for n in document.report.notes))


class FailureTests(unittest.TestCase):
    def test_a_doctype_is_refused(self):
        with self.assertRaises(CimImportError) as raised:
            loads_cim('<!DOCTYPE rdf:RDF [<!ENTITY x "y">]><rdf:RDF/>')
        self.assertIn("DOCTYPE", str(raised.exception))

    def test_malformed_xml(self):
        with self.assertRaises(CimImportError):
            loads_cim("<rdf:RDF>")

    def test_a_document_that_is_not_rdf(self):
        with self.assertRaises(CimImportError) as raised:
            loads_cim("<html></html>")
        self.assertIn("rdf:RDF", str(raised.exception))

    def test_a_missing_file(self):
        with self.assertRaises(CimImportError):
            read_cim("no-such-file.xml")


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

    def test_export_to_a_file(self):
        path = self.directory / "grid.xml"
        code, out, _ = self.run_cli(["export-cim", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("11 devices", out)
        self.assertTrue(path.exists())
        find(path.read_text(), "ProtectedSwitch", "REC-001")

    def test_export_to_stdout(self):
        code, out, _ = self.run_cli(["export-cim", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(parse(out).tag, f"{RDF}RDF")

    def test_export_a_query_slice(self):
        path = self.directory / "slice.xml"
        code, out, _ = self.run_cli(
            ["export-cim", str(path), "--query", "FIND transformers WHERE kva >= 500"]
        )
        self.assertEqual(code, 0)
        self.assertIn("1 devices", out)

    def test_export_with_a_bad_query_exits_non_zero(self):
        code, _, err = self.run_cli(
            ["export-cim", str(self.directory / "x.xml"), "--query", "FIND nonsense"]
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_import_reports_what_it_found(self):
        path = self.directory / "grid.xml"
        self.run_cli(["export-cim", str(path)])
        code, out, _ = self.run_cli(["import-cim", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("imported 11 devices", out)

    def test_import_into_a_database_then_query_it(self):
        xml = self.directory / "grid.xml"
        database = self.directory / "grid.sqlite"
        self.run_cli(["export-cim", str(xml)])

        code, out, _ = self.run_cli(["import-cim", str(xml), "--db", str(database)])
        self.assertEqual(code, 0)
        self.assertIn("saved to", out)

        code, out, _ = self.run_cli(["--db", str(database), "FIND reclosers"])
        self.assertEqual(code, 0)
        self.assertIn("REC-001", out)

    def test_import_refuses_to_clobber_a_database(self):
        xml = self.directory / "grid.xml"
        database = self.directory / "grid.sqlite"
        self.run_cli(["export-cim", str(xml)])
        self.run_cli(["import-cim", str(xml), "--db", str(database)])
        code, _, err = self.run_cli(["import-cim", str(xml), "--db", str(database)])
        self.assertEqual(code, 1)
        self.assertIn("--force", err)

    def test_the_full_pipeline(self):
        """Query a slice, export it, import it, store it, query it again."""
        xml = self.directory / "lateral.xml"
        database = self.directory / "lateral.sqlite"
        self.run_cli(["export-cim", str(xml), "--query", 'FIND devices FED BY "SW-002"'])
        self.run_cli(["import-cim", str(xml), "--db", str(database)])
        code, out, _ = self.run_cli(["--db", str(database), "FIND transformers", "-f", "csv"])
        self.assertEqual(code, 0)
        self.assertIn("XFMR-002", out)
        self.assertNotIn("XFMR-001", out)


if __name__ == "__main__":
    unittest.main()
