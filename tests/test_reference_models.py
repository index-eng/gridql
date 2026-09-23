"""The IEEE test feeders, as other tools write them in CIM and OpenDSS.

Every other import test reads either GridQL's own output or a document
written for the test, so neither can show where GridQL's reading of a format
parts from the industry's. These read the IEEE 13, 123 and 8500-node feeders
-- the GridAPPS-D CIM exports, and the OpenDSS models behind them -- and
check what GridQL makes of them against what those feeders are published to
contain. Every check runs against both formats, and the two readings of each
feeder are compared with each other device by device.

The files are not in the repository; fetch them with

    python3 tests/reference/fetch.py

and until then these tests skip.
"""

import unittest

from gridql import execute, read_dss, validate
from gridql.cim.importer import read_cim

try:
    from reference.fetch import path_of, present
except ImportError:  # run as tests.<module> rather than by discovery
    from .reference.fetch import path_of, present

_loaded = {}


def load(name):
    if not present(name):
        raise unittest.SkipTest(f"{name} not fetched; run python3 tests/reference/fetch.py")
    if name not in _loaded:
        read = read_dss if name.endswith("-dss") else read_cim
        _loaded[name] = read(path_of(name))
    return _loaded[name]


class ReferenceFeeder:
    """Checks that hold for any well-formed radial feeder.

    Names are compared without regard to case: OpenDSS keeps them as written
    (``Cap1``), and the GridAPPS-D CIM writes them in lower case (``cap1``).
    """

    NAME = ""

    @classmethod
    def setUpClass(cls):
        cls.document = load(cls.NAME)
        cls.network = cls.document.network

    def rows(self, query):
        return [
            {key: value.casefold() if key == "name" else value for key, value in row.items()}
            for row in execute(self.network, query).rows()
        ]

    def names(self, query):
        return [row["name"] for row in self.rows(query + " SELECT name")]

    def test_the_feeder_is_headed_by_its_energy_source(self):
        (feeder,) = self.network.feeders
        self.assertEqual(self.network.get(feeder.head).attribute("cim_class"), "EnergySource")

    def test_every_device_is_below_the_source(self):
        below = self.rows('FIND devices DOWNSTREAM OF "source" SELECT COUNT(*)')[0]["COUNT(*)"]
        self.assertEqual(below, len(self.network.devices) - 1)

    def test_every_device_is_energized_in_the_normal_configuration(self):
        self.assertEqual(self.names("FIND devices WHERE NOT energized"), [])

    def test_nothing_is_fed_through_a_normally_open_switch(self):
        for switch in self.names("FIND switches WHERE normal_state = OPEN"):
            with self.subTest(switch=switch):
                self.assertEqual(self.names(f'FIND devices DOWNSTREAM OF "{switch}"'), [])

    def test_every_transformer_has_a_rating(self):
        self.assertEqual(self.names("FIND transformers WHERE NOT kva"), [])

    def test_every_capacitor_has_a_rating(self):
        self.assertEqual(self.names("FIND capacitors WHERE NOT kvar"), [])

    def test_it_survives_a_round_trip(self):
        from gridql.cim import export_network
        from gridql.cim.importer import loads_cim

        again = loads_cim(export_network(self.network)).network
        self.assertEqual(
            {o.mrid: o.to_row() for o in again}, {o.mrid: o.to_row() for o in self.network}
        )
        self.assertEqual(
            {m: sorted(again.neighbors(m)) for m in again.objects},
            {m: sorted(self.network.neighbors(m)) for m in self.network.objects},
        )


class IEEE13Tests(ReferenceFeeder, unittest.TestCase):
    NAME = "IEEE13"

    def test_the_breaker_is_below_the_substation_transformer(self):
        self.assertEqual(self.names('FIND devices UPSTREAM OF "brkr1"'), ["sub3", "source"])

    def test_the_path_to_a_lateral_load(self):
        self.assertEqual(
            self.names('FIND devices UPSTREAM OF "611"'),
            ["684611", "sect1", "671684", "670671", "632670", "rec1", "650632", "reg",
             "brkr1", "sub3", "source"],
        )

    def test_the_regulator_is_three_single_phase_units(self):
        # Three 1666 kVA regulators on the 4.16 kV system.
        (regulator,) = self.rows('FIND transformers WHERE name = "Reg" SELECT phases, kva, voltage')
        self.assertEqual(regulator, {"phases": "ABC", "kva": 4998.0, "voltage": 4.16})

    def test_capacitors(self):
        # 600 kVAr three-phase at bus 675, 100 kVAr on phase C at bus 611.
        self.assertEqual(
            self.rows("FIND capacitors SELECT name, phases, kvar ORDER BY name"),
            [{"name": "cap1", "phases": "ABC", "kvar": 600.0},
             {"name": "cap2", "phases": "C", "kvar": 100.0}],
        )

    def test_single_phase_laterals(self):
        self.assertEqual(self.rows('FIND lines WHERE name = "684611" SELECT phases'),
                         [{"phases": "C"}])
        self.assertEqual(self.rows('FIND lines WHERE name = "684652" SELECT phases'),
                         [{"phases": "A"}])

    def test_it_validates_clean(self):
        self.assertEqual(list(validate(self.network)), [])


class IEEE123Tests(ReferenceFeeder, unittest.TestCase):
    NAME = "IEEE123"

    def test_published_load(self):
        # 3490 kW and 1920 kVAr of spot load on 91 buses.
        self.assertEqual(
            self.rows("FIND loads SELECT COUNT(*), SUM(kw), SUM(kvar)"),
            [{"COUNT(*)": 91, "SUM(kw)": 3490.0, "SUM(kvar)": 1920.0}],
        )

    def test_the_normally_open_ties(self):
        self.assertEqual(sorted(self.names("FIND switches WHERE normal_state = OPEN")),
                         ["sw7", "sw8"])

    def test_the_feeder_is_radial_in_its_normal_configuration(self):
        # Its two internal loops are closed only through sw7 and sw8.
        self.assertNotIn("loop", {finding.code for finding in validate(self.network)})

    def test_a_load_is_fed_the_normal_way_round(self):
        path = self.names('FIND devices UPSTREAM OF "s114a"')
        self.assertIn("reg4", path)
        self.assertNotIn("sw7", path)

    def test_the_regulators(self):
        # A gang-operated three-phase regulator at the source, one on phase A,
        # an open-delta pair on A and C, and three single-phase units.
        self.assertEqual(
            self.rows("FIND transformers WHERE name IN (reg1a, reg2, reg3, reg4) "
                      "SELECT name, phases, kva ORDER BY name"),
            [{"name": "reg1a", "phases": "ABC", "kva": 5000.0},
             {"name": "reg2", "phases": "A", "kva": 2000.0},
             {"name": "reg3", "phases": "AC", "kva": 4000.0},
             {"name": "reg4", "phases": "ABC", "kva": 6000.0}],
        )

    def test_capacitors(self):
        self.assertEqual(
            self.rows("FIND capacitors SELECT name, phases, kvar ORDER BY name"),
            [{"name": "c83", "phases": "ABC", "kvar": 600.0},
             {"name": "c88a", "phases": "A", "kvar": 50.0},
             {"name": "c90b", "phases": "B", "kvar": 50.0},
             {"name": "c92c", "phases": "C", "kvar": 50.0}],
        )

    def test_laterals_are_single_phase_on_every_phase(self):
        by_phases = {row["phases"]: row["COUNT(*)"]
                     for row in self.rows("FIND lines GROUP BY phases")}
        for phase in ("A", "B", "C"):
            self.assertGreater(by_phases.get(phase, 0), 0, phase)


class IEEE8500Tests(ReferenceFeeder, unittest.TestCase):
    NAME = "IEEE8500"

    def test_the_source_reaches_the_feeder_through_equipment_gridql_does_not_model(self):
        self.assertEqual(
            self.rows('FIND devices WHERE depth <= 2 SELECT name, cim_class ORDER BY depth'),
            [{"name": "source", "cim_class": "EnergySource"},
             {"name": "hvmv_sub_hsb", "cim_class": "SeriesCompensator"},
             {"name": "hvmv_sub", "cim_class": "PowerTransformer"}],
        )

    def test_every_customer_is_on_a_split_phase_service(self):
        self.assertEqual(self.rows("FIND loads GROUP BY phases"),
                         [{"phases": "s1s2", "COUNT(*)": 1177}])

    def test_service_transformers_are_single_phase_and_rated(self):
        (row,) = self.rows("FIND transformers WHERE primary_voltage = 7.2kV AND kva < 1000 "
                           "SELECT COUNT(*), MIN(kva), MAX(kva)")
        self.assertEqual(row["COUNT(*)"], 1177)
        self.assertGreater(row["MIN(kva)"], 0)
        self.assertEqual(
            self.rows("FIND transformers WHERE primary_voltage = 7.2kV AND kva < 1000 "
                      "AND NOT (phases IN (A, B, C)) SELECT COUNT(*)"),
            [{"COUNT(*)": 0}],
        )

    def test_capacitor_banks(self):
        # Three switched single-phase banks and one fixed three-phase bank.
        self.assertEqual(
            self.rows("FIND capacitors SELECT SUM(kvar), COUNT(*)"),
            [{"SUM(kvar)": 3900.0, "COUNT(*)": 10}],
        )

    def test_load(self):
        (row,) = self.rows("FIND loads SELECT COUNT(*), SUM(kw)")
        self.assertEqual(row["COUNT(*)"], 1177)
        self.assertAlmostEqual(row["SUM(kw)"], 10773.17, places=2)


class IEEE13DssTests(IEEE13Tests):
    NAME = "IEEE13-dss"


class IEEE123DssTests(IEEE123Tests):
    NAME = "IEEE123-dss"


class IEEE8500DssTests(IEEE8500Tests):
    NAME = "IEEE8500-dss"

    def test_disabled_switches_are_there_and_open(self):
        # EPRI's model disables five switches rather than opening them.
        self.assertEqual(
            sorted(self.names("FIND switches WHERE normal_state = OPEN")),
            ["v7995_48332_sw", "wd701_48332_sw", "wf586_48332_sw", "wf856_48332_sw",
             "wg127_48332_sw"],
        )


class SameFeederTwoWaysTests(unittest.TestCase):
    """One feeder read from CIM and from the OpenDSS model it was made from.

    GridAPPS-D converted these CIM files from OpenDSS, so apart from what the
    conversion itself changed, the two readings must agree device by device:
    the same equipment, joined the same way, with the same ratings.
    """

    FIELDS = ("type", "kva", "primary_voltage", "secondary_voltage", "kw", "kvar",
              "length", "normal_state")

    def compare(self, name):
        cim, dss = load(name).network, load(f"{name}-dss").network
        by_name = [
            {device.name.casefold(): device for device in network.devices}
            for network in (cim, dss)
        ]
        return cim, dss, *by_name

    def check(self, name, only_in_dss=()):
        cim, dss, from_cim, from_dss = self.compare(name)
        self.assertEqual(set(from_dss) - set(from_cim), set(only_in_dss))
        self.assertEqual(set(from_cim) - set(from_dss), set())

        def neighbours(network, device):
            found = {network.objects[m].name.casefold() for m in network.neighbors(device.mrid)}
            return found - set(only_in_dss)

        for key in sorted(set(from_cim) & set(from_dss)):
            a, b = from_cim[key], from_dss[key]
            if a.TYPE != b.TYPE:
                continue  # a name two devices share in OpenDSS; see test_ieee13
            with self.subTest(device=key):
                for field in self.FIELDS:
                    x, y = a.attribute(field), b.attribute(field)
                    if isinstance(x, float) and isinstance(y, float):
                        self.assertAlmostEqual(x, y, delta=1e-3 * max(1.0, abs(x)), msg=field)
                    else:
                        self.assertEqual(x, y, field)
                # A load across two phases is on both; CIM names just one.
                self.assertLessEqual(set(a.phases), set(b.phases))
                if a.voltage is not None:
                    self.assertEqual(a.voltage, b.voltage)
                self.assertEqual(neighbours(cim, a), neighbours(dss, b))

    def test_ieee13(self):
        self.check("IEEE13")

    def test_ieee123(self):
        self.check("IEEE123")

    def test_ieee8500(self):
        # The five switches EPRI's model disables are left out of the CIM.
        self.check("IEEE8500", only_in_dss=(
            "v7995_48332_sw", "wd701_48332_sw", "wf586_48332_sw", "wf856_48332_sw",
            "wg127_48332_sw",
        ))


if __name__ == "__main__":
    unittest.main()
