"""Generation: the equipment that puts power onto the network, and its ratings.

Every way a model can arrive -- built in code, CSV, CIM, OpenDSS, SQLite --
should agree on what a generator is, what kind it is, and what it is rated
at, and should say nothing about a kind or a rating its source did not give.
"""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gridql import build_sample_network, execute, load_network, save_network
from gridql.cim.exporter import export_network
from gridql.cim.importer import loads_cim
from gridql.dss.importer import loads_dss
from gridql.ingest import read_csv, write_csv
from gridql.model import Device, Generator

QUERY = "FIND generators SELECT mrid, kind, kw, kva, cim_class ORDER BY mrid"


def with_generation():
    """The sample feeder with one generator of every kind, and one of none."""
    network = build_sample_network()
    feeder = network.objects["FDR-104"]
    feeder.add_generator("PV-1", after="LOAD-001", kind="solar", kw=50, kva=60)
    feeder.add_generator("BESS-1", after="LOAD-001", kind="battery", kw=100, kva=100)
    feeder.add_generator("WT-1", after="LOAD-001", kind="wind", kw=20)
    feeder.add_generator("SG-1", after="LOAD-001", kind="synchronous", kw=500, kva=625)
    feeder.add_generator("IG-1", after="LOAD-001", kind="induction", kw=75)
    feeder.add_generator("DG-1", after="LOAD-001", kind="Diesel", kw="200kW", kva="250kVA")
    feeder.add_generator("U-1", after="LOAD-001", kw=10)
    return network


def rows(network, query=QUERY):
    return execute(network, query).rows()


class ModelTests(unittest.TestCase):
    def test_every_name_for_generation_finds_it(self):
        network = with_generation()
        for name in ("generators", "generator", "gens", "der", "ders", "generation"):
            with self.subTest(name=name):
                self.assertEqual(len(execute(network, f"FIND {name}").mrids), 7)

    def test_a_kind_is_kept_in_one_spelling(self):
        kinds = {row["mrid"]: row["kind"] for row in rows(with_generation())}
        self.assertEqual(kinds["PV-1"], "pv")
        self.assertEqual(kinds["BESS-1"], "storage")
        self.assertEqual(kinds["DG-1"], "diesel")  # not one GridQL knows, but what it was called
        self.assertIsNone(kinds["U-1"])

    def test_the_cim_class_follows_the_kind(self):
        classes = {row["mrid"]: row["cim_class"] for row in rows(with_generation())}
        self.assertEqual(classes["PV-1"], "PowerElectronicsConnection")
        self.assertEqual(classes["WT-1"], "PowerElectronicsConnection")
        self.assertEqual(classes["SG-1"], "SynchronousMachine")
        self.assertEqual(classes["IG-1"], "AsynchronousMachine")
        # An unstated kind is not assumed to be an inverter.
        self.assertEqual(classes["U-1"], "ConductingEquipment")
        self.assertEqual(classes["DG-1"], "ConductingEquipment")

    def test_ratings_are_quantities(self):
        generator = with_generation().objects["DG-1"]
        self.assertEqual((generator.kw, generator.kva), (200.0, 250.0))
        self.assertEqual(
            rows(build_with("PV-2", kind="pv", kw="0.5MW"), "FIND der SELECT kw"), [{"kw": 500.0}]
        )

    def test_rated_output_aggregates_by_kind(self):
        result = rows(
            with_generation(),
            "FIND generators WHERE kind = \"pv\" OR kind = \"storage\" SELECT SUM(kw)",
        )
        self.assertEqual(result, [{"SUM(kw)": 150.0}])

    def test_loads_are_not_generation(self):
        self.assertNotIn("LOAD-001", execute(with_generation(), "FIND generators").mrids)


def build_with(mrid, **fields):
    network = build_sample_network()
    network.objects["FDR-104"].add_generator(mrid, after="LOAD-001", **fields)
    return network


class EnergisationTests(unittest.TestCase):
    def test_a_generator_behind_an_open_switch_leaves_its_section_unknown(self):
        network = build_sample_network()
        network.objects["FDR-104"].add_generator("PV-1", after="XFMR-002", kind="pv", kw=5)
        self.assertEqual(
            execute(network, "FIND devices WHERE energized IS MISSING").mrids,
            ["LOAD-002", "PV-1", "XFMR-002"],
        )
        self.assertEqual(execute(network, "FIND devices WHERE NOT energized").mrids, [])

    def test_a_generator_the_feeder_reaches_is_energised(self):
        self.assertEqual(
            execute(with_generation(), "FIND generators WHERE NOT energized").mrids, []
        )
        self.assertEqual(
            execute(with_generation(), "FIND generators WHERE energized IS MISSING").mrids, []
        )


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "grid.sqlite"

    def test_generators_survive_a_save(self):
        network = with_generation()
        save_network(network, self.path)
        loaded = load_network(self.path)
        self.assertIsInstance(loaded.objects["PV-1"], Generator)
        self.assertEqual(rows(loaded), rows(network))

    def test_a_database_from_before_generators_still_loads(self):
        save_network(build_sample_network(), self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE generators")
            connection.commit()
        self.assertEqual(
            sorted(load_network(self.path).objects), sorted(build_sample_network().objects)
        )


class CsvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def load(self, devices):
        (self.directory / "devices.csv").write_text(devices.lstrip("\n"), encoding="utf-8")
        return read_csv(self.directory)

    def test_a_type_can_say_the_kind(self):
        document = self.load("""
mrid,type,feeder,kw,kva
PV-1,solar,F1,50,60
BAT-1,Battery,F1,100,
SG-1,SynchronousMachine,F1,500,625
G-1,generator,F1,10,
""")
        self.assertEqual(document.report.problems, [])
        kinds = {row["mrid"]: row["kind"] for row in rows(document.network)}
        self.assertEqual(
            kinds, {"BAT-1": "storage", "G-1": None, "PV-1": "pv", "SG-1": "synchronous"}
        )

    def test_a_kind_column_is_read(self):
        network = self.load("""
mrid,type,feeder,der_type,kw
G-1,der,F1,wind,20
""").network
        self.assertEqual(rows(network, "FIND der SELECT kind, kw"), [{"kind": "wind", "kw": 20.0}])

    def test_generators_survive_a_round_trip(self):
        network = with_generation()
        out = self.directory / "out"
        write_csv(network, out)
        self.assertEqual(rows(read_csv(out).network), rows(network))


class CimTests(unittest.TestCase):
    def test_generators_survive_a_round_trip(self):
        network = with_generation()
        document = loads_cim(export_network(network))
        self.assertEqual(document.report.ignored, {})
        self.assertEqual(rows(document.network), rows(network))

    def test_an_inverter_takes_its_kind_and_rating_from_its_units(self):
        for direction in ("unit names the connection", "connection names the unit"):
            with self.subTest(direction=direction):
                network = loads_cim(inverter(direction)).network
                self.assertEqual(
                    rows(network, "FIND generators SELECT mrid, kind, kw, kva"),
                    [{"mrid": "PV1", "kind": "pv", "kw": 300.0, "kva": 330.0}],
                )

    def test_an_inverter_with_units_of_two_kinds_is_given_neither(self):
        text = inverter("unit names the connection").replace(
            "</rdf:RDF>",
            '<cim:BatteryUnit rdf:ID="U2">'
            '<cim:PowerElectronicsUnit.PowerElectronicsConnection rdf:resource="#PV1"/>'
            "<cim:PowerElectronicsUnit.maxP>100000</cim:PowerElectronicsUnit.maxP>"
            "</cim:BatteryUnit></rdf:RDF>",
        )
        self.assertEqual(
            rows(loads_cim(text).network, "FIND generators SELECT kind, kw"),
            [{"kind": None, "kw": 400.0}],
        )

    def test_a_machine_is_rated_by_its_ratedS(self):
        text = inverter("unit names the connection").replace(
            "</rdf:RDF>",
            '<cim:SynchronousMachine rdf:ID="SG1">'
            "<cim:RotatingMachine.ratedS>2500000</cim:RotatingMachine.ratedS>"
            "</cim:SynchronousMachine></rdf:RDF>",
        )
        self.assertIn(
            {"mrid": "SG1", "kind": "synchronous", "kw": None, "kva": 2500.0},
            rows(loads_cim(text).network, "FIND generators SELECT mrid, kind, kw, kva"),
        )


def inverter(direction):
    """A PV inverter as a standards-only tool writes it: no gridql: namespace."""
    on_unit = direction == "unit names the connection"
    unit_link = (
        '<cim:PowerElectronicsUnit.PowerElectronicsConnection rdf:resource="#PV1"/>'
        if on_unit else ""
    )
    connection_link = (
        "" if on_unit
        else '<cim:PowerElectronicsConnection.PowerElectronicsUnit rdf:resource="#U1"/>'
    )
    return f"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cim="http://iec.ch/TC57/CIM100#">
  <cim:PhotovoltaicUnit rdf:ID="U1">
    {unit_link}
    <cim:PowerElectronicsUnit.maxP>300000</cim:PowerElectronicsUnit.maxP>
  </cim:PhotovoltaicUnit>
  <cim:PowerElectronicsConnection rdf:ID="PV1">
    {connection_link}
    <cim:PowerElectronicsConnection.ratedS>330000</cim:PowerElectronicsConnection.ratedS>
  </cim:PowerElectronicsConnection>
</rdf:RDF>"""


class DssTests(unittest.TestCase):
    MODEL = """
Clear
New Circuit.c1 bus1=src basekv=12.47
New Line.l1 bus1=src bus2=b1
New Line.sw bus1=b1 bus2=b2 switch=yes
Open Line.sw
New Load.ld bus1=b2 kw=10
New PVSystem.pv bus1=b2 pmpp=50 kva=55
New Storage.bat bus1=b1 kwrated=100
New Generator.g bus1=b1 kw=300
New WindGen.w bus1=b1
"""

    def test_generation_is_read_with_its_ratings(self):
        network = loads_dss(self.MODEL).network
        query = "FIND generators SELECT mrid, kind, kw, kva ORDER BY mrid"
        self.assertEqual(rows(network, query), [
            {"mrid": "bat", "kind": "storage", "kw": 100.0, "kva": 100.0},
            {"mrid": "g", "kind": "synchronous", "kw": 300.0, "kva": 360.0},
            {"mrid": "pv", "kind": "pv", "kw": 50.0, "kva": 55.0},
            {"mrid": "w", "kind": "wind", "kw": None, "kva": None},  # no defaults assumed
        ])

    def test_opendss_defaults_fill_what_the_model_leaves_unset(self):
        network = loads_dss(
            "New Circuit.c1 bus1=src\nNew PVSystem.pv bus1=src\nNew Storage.bat bus1=src\n"
        ).network
        self.assertEqual(rows(network, "FIND generators SELECT mrid, kw, kva ORDER BY mrid"), [
            {"mrid": "bat", "kw": 25.0, "kva": 25.0},
            {"mrid": "pv", "kw": 500.0, "kva": 500.0},
        ])

    def test_the_pv_behind_the_open_switch_may_backfeed(self):
        network = loads_dss(self.MODEL).network
        self.assertEqual(
            execute(network, "FIND devices WHERE energized IS MISSING").mrids, ["ld", "pv"]
        )

    def test_a_source_is_still_a_plain_device(self):
        source = loads_dss(self.MODEL).network.objects["source"]
        self.assertIs(type(source), Device)


if __name__ == "__main__":
    unittest.main()
