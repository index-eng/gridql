"""Protection zones: PROTECTED BY, and the protected_by attribute."""

import unittest

from gridql import Network, execute
from gridql.errors import GridQLError, GridQLSyntaxError
from gridql.lang.ast import Relation
from gridql.lang.evaluator import evaluate_script
from gridql.lang.parser import parse, parse_script


def protected_feeder():
    """A feeder with a zone at each level.

        B -- L1 -- R -+- C1
                      +- L2 -+- F1 -- X1 -- D1
                             +- F2 -- L3 -- S -- D2
                             +- SW -- D3

    B breaker, R recloser, F fuses, S sectionalizer, SW a plain switch.
    """
    network = Network()
    network.add_substation("SUB", voltage="12.47kV")
    feeder = network.add_feeder("FDR", substation="SUB")
    feeder.add_breaker("B")
    feeder.add_line("L1")
    feeder.add_recloser("R")
    feeder.add_capacitor("C1", after="R", kvar=600)
    feeder.add_line("L2", after="R")
    feeder.add_fuse("F1", after="L2")
    feeder.add_transformer("X1", kva=50)
    feeder.add_load("D1", kw=40)
    feeder.add_fuse("F2", after="L2")
    feeder.add_line("L3")
    feeder.add_sectionalizer("S")
    feeder.add_load("D2", kw=25)
    feeder.add_switch("SW", after="L2")
    feeder.add_load("D3", kw=10)
    return network


class ZoneTests(unittest.TestCase):
    def setUp(self):
        self.network = protected_feeder()

    def zone(self, target):
        return [o.mrid for o in self.network.protected_by(target)]

    def test_a_zone_runs_down_to_the_next_protective_devices(self):
        # F1, F2 bound the recloser's zone and are in it; what they protect is not.
        self.assertEqual(self.zone("R"), ["C1", "L2", "F1", "F2", "SW", "D3"])

    def test_a_plain_switch_does_not_bound_a_zone(self):
        self.assertIn("D3", self.zone("R"))

    def test_a_sectionalizer_bounds_one(self):
        self.assertEqual(self.zone("F2"), ["L3", "S"])
        self.assertEqual(self.zone("S"), ["D2"])

    def test_the_head_breaker_protects_down_to_the_recloser(self):
        self.assertEqual(self.zone("B"), ["L1", "R"])

    def test_a_feeder_s_zone_is_its_head_s(self):
        self.assertEqual(self.zone("FDR"), self.zone("B"))

    def test_the_zones_cover_the_feeder_once(self):
        protective = ["B", "R", "F1", "F2", "S"]
        covered = [mrid for target in protective for mrid in self.zone(target)]
        self.assertEqual(len(covered), len(set(covered)))
        self.assertEqual(set(covered) | {"B"}, {d.mrid for d in self.network.devices})

    def test_zones_follow_the_normal_configuration_not_the_present_one(self):
        before = self.zone("R")
        self.network.get("F1").state = "OPEN"
        self.network.get("SW").state = "OPEN"
        self.assertEqual(self.zone("R"), before)

    def test_zones_follow_a_change_of_equipment(self):
        feeder = self.network.get("FDR")
        self.zone("R")  # build the cached topology first
        feeder.add_fuse("F3", after="SW")
        feeder.add_load("D4")
        self.assertEqual(self.zone("F3"), ["D4"])
        self.assertIn("F3", self.zone("R"))
        self.assertNotIn("D4", self.zone("R"))


class ProtectedByAttributeTests(unittest.TestCase):
    def setUp(self):
        self.network = protected_feeder()

    def value_of(self, mrid):
        rows = execute(self.network, f'FIND devices WHERE mrid = "{mrid}" SELECT protected_by').rows()
        return rows[0]["protected_by"]

    def test_the_nearest_protective_device_above(self):
        self.assertEqual(self.value_of("D1"), "F1")
        self.assertEqual(self.value_of("D2"), "S")
        self.assertEqual(self.value_of("D3"), "R")

    def test_a_protective_device_is_covered_by_the_one_above_it(self):
        self.assertEqual(self.value_of("F1"), "R")
        self.assertEqual(self.value_of("R"), "B")

    def test_nothing_protects_the_head(self):
        self.assertIsNone(self.value_of("B"))

    def test_it_agrees_with_protected_by(self):
        for target in ("B", "R", "F1", "F2", "S"):
            with self.subTest(target=target):
                self.assertEqual(
                    execute(self.network, f'FIND devices WHERE protected_by = "{target}"').mrids,
                    sorted(o.mrid for o in self.network.protected_by(target)),
                )

    def test_load_per_protective_device(self):
        rows = execute(
            self.network,
            "FIND loads SELECT protected_by, COUNT(*), SUM(kw) GROUP BY protected_by",
        ).rows()
        self.assertEqual(
            {r["protected_by"]: (r["COUNT(*)"], r["SUM(kw)"]) for r in rows},
            {"F1": (1, 40), "S": (1, 25), "R": (1, 10)},
        )


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.network = protected_feeder()

    def mrids(self, source):
        return execute(self.network, source).mrids

    def refusal(self, source):
        with self.assertRaises(GridQLError) as raised:
            execute(self.network, source)
        return str(raised.exception)

    def test_it_parses_as_a_relation(self):
        query = parse('FIND loads PROTECTED BY "R"')
        self.assertEqual(query.relations, (Relation("PROTECTED BY", "R", query.relations[0].position),))
        self.assertEqual(parse(query.describe()), query)

    def test_it_narrows_by_type_in_walking_order(self):
        self.assertEqual(self.mrids('FIND devices PROTECTED BY "R"'),
                         ["C1", "L2", "F1", "F2", "SW", "D3"])
        self.assertEqual(self.mrids('FIND loads PROTECTED BY "R"'), ["D3"])

    def test_hops_counts_from_the_protective_device(self):
        rows = execute(self.network, 'FIND devices PROTECTED BY "R" SELECT mrid, hops').rows()
        self.assertEqual({r["mrid"]: r["hops"] for r in rows},
                         {"C1": 1, "L2": 1, "F1": 2, "F2": 2, "SW": 2, "D3": 3})

    def test_it_intersects_with_other_relations(self):
        self.assertEqual(self.mrids('FIND devices PROTECTED BY "R" DOWNSTREAM OF "L2"'),
                         ["F1", "F2", "SW", "D3"])

    def test_a_parameter_can_name_the_target(self):
        script = parse_script("PARAM fuse\nFIND devices PROTECTED BY $fuse")
        results = evaluate_script(self.network, script, {"fuse": "F1"})
        self.assertEqual(results[0].mrids, ["X1", "D1"])

    def test_a_device_that_clears_nothing_is_refused(self):
        message = self.refusal('FIND devices PROTECTED BY "SW"')
        self.assertIn("'SW' is a switch, which isolates no fault on its own", message)
        self.assertIn("SELECT protected_by", message)

    def test_a_substation_is_refused(self):
        self.assertIn("name one of its feeders", self.refusal('FIND devices PROTECTED BY "SUB"'))

    def test_a_feeder_headed_by_something_unprotective_is_refused(self):
        network = Network()
        feeder = network.add_feeder("FDR")
        feeder.add_device("SOURCE")
        feeder.add_breaker("B")
        with self.assertRaises(GridQLError) as raised:
            execute(network, 'FIND devices PROTECTED BY "FDR"')
        self.assertIn("feeder 'FDR' starts at device 'SOURCE'", str(raised.exception))
        self.assertEqual(execute(network, 'FIND devices PROTECTED BY "B"').mrids, [])

    def test_containers_are_refused_as_for_every_relation(self):
        self.assertIn("containers, not equipment", self.refusal('FIND feeders PROTECTED BY "B"'))

    def test_out_of_order_is_a_syntax_error(self):
        with self.assertRaises(GridQLSyntaxError) as raised:
            parse('FIND devices WHERE kw > 1 PROTECTED BY "R"')
        self.assertIn("PROTECTED must come earlier", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
