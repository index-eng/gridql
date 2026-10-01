"""Refusing rather than guessing: what GridQL does when the model cannot say.

Each test here is a case that used to come back as a confident answer -- an
empty result, a false outage, a match on a value that was never recorded --
where the honest answer is "cannot tell".
"""

import tempfile
import unittest
from pathlib import Path

from gridql import Network, build_sample_network, execute, load_csv
from gridql.errors import GridQLError, GridQLNameError


def headless_feeder():
    """A feeder with two breakers and no recorded head, so none can be inferred.

        A -- R -+- X -- L1
                +- B -- L2
    """
    network = Network()
    network.add_substation("SUB")
    feeder = network.add_feeder("F1", substation="SUB")
    feeder.add_breaker("A")
    feeder.add_recloser("R")
    feeder.add_transformer("X", kva=50)
    feeder.add_load("L1", kw=10)
    feeder.add_breaker("B", after="R")
    feeder.add_load("L2", kw=5)
    feeder.head = None
    return network


def mrids(network, query):
    return execute(network, query).mrids


class TopologyOffTheTreeTests(unittest.TestCase):
    """A tree relation whose target is on no feeder tree is refused, saying why."""

    def test_every_tree_relation_refuses_a_headless_feeder(self):
        network = headless_feeder()
        for relation in ("DOWNSTREAM OF", "UPSTREAM OF", "FED BY", "PROTECTED BY"):
            with self.subTest(relation=relation):
                with self.assertRaisesRegex(GridQLError, "no head device"):
                    execute(network, f'FIND devices {relation} "R"')

    def test_an_island_is_refused(self):
        network = build_sample_network()
        feeder = network.objects["FDR-104"]
        feeder.add_line("ISLAND-1", after=None)
        feeder.add_load("ISLAND-2", after="ISLAND-1")
        with self.assertRaisesRegex(GridQLError, "island"):
            execute(network, 'FIND devices DOWNSTREAM OF "ISLAND-1"')

    def test_equipment_on_no_feeder_is_refused(self):
        network = build_sample_network()
        network.add(type(network.objects["LOAD-001"])(mrid="STRAY"))
        network.connect("STRAY", "LOAD-001")
        with self.assertRaisesRegex(GridQLError, "on no feeder"):
            execute(network, 'FIND devices UPSTREAM OF "STRAY"')

    def test_containers_with_nothing_to_answer_are_refused(self):
        network = build_sample_network()
        with self.assertRaisesRegex(GridQLError, "nothing upstream"):
            execute(network, 'FIND devices UPSTREAM OF "FDR-104"')
        with self.assertRaisesRegex(GridQLError, "nothing upstream"):
            execute(network, 'FIND devices UPSTREAM OF "SUB-001"')
        with self.assertRaisesRegex(GridQLError, "no terminals"):
            execute(network, 'FIND devices CONNECTED TO "SUB-001"')

    def test_a_headless_feeders_zone_and_neighbours_are_refused(self):
        network = headless_feeder()
        with self.assertRaisesRegex(GridQLError, "no head device"):
            execute(network, 'FIND devices PROTECTED BY "F1"')
        with self.assertRaisesRegex(GridQLError, "no head device"):
            execute(network, 'FIND devices CONNECTED TO "F1"')

    def test_membership_still_answers_for_a_headless_feeder(self):
        # Which feeder equipment belongs to is recorded, not derived.
        self.assertEqual(len(mrids(headless_feeder(), 'FIND devices FED BY "F1"')), 6)

    def test_a_target_on_the_tree_is_answered_as_before(self):
        network = build_sample_network()
        self.assertIn("LOAD-001", mrids(network, 'FIND loads DOWNSTREAM OF "REC-001"'))


class EnergisationTests(unittest.TestCase):
    def test_a_headless_feeder_is_unknown_not_dead(self):
        network = headless_feeder()
        rows = execute(network, "FIND loads SELECT mrid, energized").rows()
        self.assertEqual([row["energized"] for row in rows], [None, None])
        self.assertEqual(mrids(network, "FIND loads WHERE NOT energized"), [])
        self.assertEqual(mrids(network, "FIND loads WHERE energized IS MISSING"), ["L1", "L2"])

    def test_an_unreadable_switch_state_makes_what_is_beyond_it_unknown(self):
        network = build_sample_network()
        network.objects["REC-001"].state = "AJAR"
        value = lambda mrid: execute(
            network, f'FIND devices WHERE mrid = "{mrid}" SELECT energized'
        ).rows()[0]["energized"]
        self.assertIs(value("REC-001"), True)  # live on its source side
        self.assertIsNone(value("LOAD-001"))
        # Behind a switch known to be open, it is dead whatever is above.
        self.assertIs(value("LOAD-002"), False)

    def test_a_tripped_feeder_breaker_leaves_the_feeder_out(self):
        network = build_sample_network()
        network.objects["BRK-001"].state = "OPEN"
        self.assertEqual(mrids(network, "FIND feeders WHERE NOT energized"), ["FDR-104"])

    def test_a_feeder_with_its_breaker_closed_is_energised(self):
        self.assertEqual(mrids(build_sample_network(), "FIND feeders WHERE energized"), ["FDR-104"])


class AmbiguousNameTests(unittest.TestCase):
    def setUp(self):
        self.network = build_sample_network()
        self.network.objects["SW-001"].name = "Midline Switch"
        self.network.objects["SW-002"].name = "Midline Switch"

    def test_a_name_shared_by_two_objects_is_refused_listing_both(self):
        with self.assertRaises(GridQLNameError) as caught:
            execute(self.network, 'FIND devices DOWNSTREAM OF "Midline Switch"')
        self.assertIn("SW-001", str(caught.exception))
        self.assertIn("SW-002", str(caught.exception))

    def test_a_unique_name_and_an_mrid_in_any_case_still_resolve(self):
        self.assertEqual(
            mrids(self.network, 'FIND loads DOWNSTREAM OF "Elm St Bank"'), ["LOAD-001"]
        )
        self.assertEqual(mrids(self.network, 'FIND loads DOWNSTREAM OF "xfmr-001"'), ["LOAD-001"])


class MissingValueTests(unittest.TestCase):
    def setUp(self):
        network = Network()
        feeder = network.add_feeder("F1")
        feeder.add_breaker("BKR")
        feeder.add_line("L-NONE")
        feeder.add_line("L-ACSR", conductor="336 ACSR")
        feeder.add_transformer("TX-500", kva=500)
        feeder.add_transformer("TX-NONE")
        self.network = network

    def test_contains_does_not_search_a_missing_value(self):
        self.assertEqual(mrids(self.network, 'FIND lines WHERE conductor CONTAINS "on"'), [])

    def test_a_negated_comparison_agrees_with_its_opposite(self):
        for query in (
            "FIND devices WHERE kva != 500",
            "FIND devices WHERE NOT kva = 500",
            "FIND devices WHERE NOT kva >= 100",
        ):
            with self.subTest(query=query):
                self.assertEqual(mrids(self.network, query), [])

    def test_is_missing_finds_what_comparisons_cannot(self):
        self.assertEqual(mrids(self.network, "FIND transformers WHERE kva IS MISSING"), ["TX-NONE"])
        self.assertEqual(
            mrids(self.network, "FIND transformers WHERE kva IS NOT MISSING"), ["TX-500"]
        )
        self.assertEqual(
            mrids(self.network, "FIND transformers WHERE kva < 100 OR kva IS MISSING"), ["TX-NONE"]
        )

    def test_unknown_or_true_is_true(self):
        self.assertEqual(
            mrids(self.network, 'FIND devices WHERE kva >= 100 OR mrid = "BKR"'), ["BKR", "TX-500"]
        )


class MessyCsvValueTests(unittest.TestCase):
    """Values as a utility's own columns hold them."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        Path(self.directory.name, "devices.csv").write_text(
            "mrid,type,feeder,from_node,to_node,link_type,nameplate,transformer\n"
            "BKR,breaker,F1,N0,N1,,,\n"
            "FU-K,fuse,F1,N1,N2,K,,\n"
            "FU-T,fuse,F1,N1,N3,T,,\n"
            "TX-25,transformer,F1,N2,N4,,25 kVA,\n"
            "TX-UNK,transformer,F1,N3,N5,,UNKNOWN,\n"
            "TX-167,transformer,F1,N3,N6,,167,\n"
            "SP-1,load,F1,N4,,,,TX-25\n",
            encoding="utf-8",
        )
        self.network = load_csv(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_a_single_letter_stays_text(self):
        self.assertEqual(self.network.objects["FU-T"].extras["link_type"], "T")
        self.assertEqual(mrids(self.network, "FIND fuses WHERE link_type = K"), ["FU-K"])

    def test_a_number_does_not_order_against_text(self):
        self.assertEqual(
            mrids(self.network, "FIND transformers WHERE nameplate >= 100"), ["TX-167"]
        )
        self.assertEqual(mrids(self.network, "FIND transformers WHERE nameplate < 100"), [])
        # Unequal, though: text is never the number.
        self.assertEqual(
            mrids(self.network, "FIND transformers WHERE nameplate != 167"), ["TX-25", "TX-UNK"]
        )

    def test_a_boolean_compares_only_with_a_boolean(self):
        network = build_sample_network()
        self.assertEqual(mrids(network, "FIND switches WHERE is_tie = yes"), ["TIE-001"])
        self.assertEqual(mrids(network, "FIND switches WHERE is_tie = K"), [])

    def test_a_bare_word_that_is_both_a_column_and_a_value_is_refused(self):
        with self.assertRaisesRegex(GridQLError, "both a GridQL value and a column"):
            execute(self.network, "FIND devices WHERE type = transformer")
        with self.assertRaisesRegex(GridQLError, "both a GridQL value and a column"):
            execute(self.network, "FIND devices WHERE type IN (load, transformer)")

    def test_quoting_says_the_value_is_meant(self):
        self.assertEqual(
            mrids(self.network, 'FIND devices WHERE type = "transformer"'),
            ["TX-167", "TX-25", "TX-UNK"],
        )

    def test_a_column_that_is_no_gridql_value_still_reads_as_one(self):
        # A utility's own column on the right is still an attribute.
        self.assertEqual(
            mrids(self.network, "FIND transformers WHERE nameplate = nameplate"),
            ["TX-167", "TX-25", "TX-UNK"],
        )


if __name__ == "__main__":
    unittest.main()
