"""GridQL's CIM export, checked against the CIM17 schema rather than itself.

The other CIM tests read GridQL's output back with GridQL, which shows a
round trip loses nothing but not that anyone else would accept the document.
These check every element and property an export writes in the CIM namespace
against the published CIM17 schema (namespace CIM100), as PNNL's CIM-Graph
renders it:

* the class exists, and the property belongs to it or one of its ancestors;
* an association is written as a reference, and an attribute as text;
* a reference points at an element in the document, of the class the
  association expects.

The schema is fetched with the reference feeders; until then these skip:

    python3 tests/reference/fetch.py CIM17-schema
"""

import re
import unittest
from xml.etree import ElementTree as ET

from gridql import build_sample_network
from gridql.cim import export_network
from gridql.cim.importer import loads_cim
from gridql.cim.vocabulary import CIM_NS, GRIDQL_NS, RDF_NS

try:
    from reference.fetch import path_of, present
    from test_reference_models import load
except ImportError:  # run as tests.<module> rather than by discovery
    from .reference.fetch import path_of, present
    from .test_reference_models import load

RESOURCE = f"{{{RDF_NS}}}resource"
ID = f"{{{RDF_NS}}}ID"

_SECTION = re.compile(r"^\{#[A-Za-z0-9]+-(\w+)\}")
_INHERITS = re.compile(r"^Inheritance path = (.*)")
_MEMBER = re.compile(r"^\| `?(\w+) \[[^\]]+\]`?(?: \(\w+\))? \| \[(\w+)\]")
_ASSOCIATION = re.compile(r'^(\w+) --> "[^"]+" (\w+) : (\w+)\s*$')


class Schema:
    """Each class's ancestors, its own members with their types, and which
    of those members are associations rather than attributes."""

    def __init__(self, text):
        self.parents = {}
        self.members = {}
        self.associations = set()
        current = None
        for line in text.splitlines():
            if match := _SECTION.match(line):
                current = match.group(1)
                self.parents.setdefault(current, [])
                self.members.setdefault(current, {})
            elif current is None:
                continue
            elif match := _INHERITS.match(line):
                self.parents[current] = re.findall(r"\[(\w+)\]", match.group(1))
            elif match := _MEMBER.match(line):
                self.members[current].setdefault(match.group(1), match.group(2))
            elif (match := _ASSOCIATION.match(line)) and match.group(1) == current:
                self.associations.add((current, match.group(3)))

    def lineage(self, cls):
        return [cls, *self.parents.get(cls, [])]


_schema = None


def schema():
    global _schema
    if not present("CIM17-schema"):
        raise unittest.SkipTest(
            "CIM17 schema not fetched; run python3 tests/reference/fetch.py CIM17-schema"
        )
    if _schema is None:
        _schema = Schema(path_of("CIM17-schema").read_text(encoding="utf-8", errors="replace"))
    return _schema


def nonconformities(document, schema):
    """Every way the document departs from the schema, as readable lines."""
    root = ET.fromstring(document)
    cim = f"{{{CIM_NS}}}"
    classes = {
        element.get(ID): element.tag[len(cim):]
        for element in root
        if isinstance(element.tag, str) and element.tag.startswith(cim)
    }
    found = set()
    for element in root:
        if not isinstance(element.tag, str):
            continue  # the header comment
        if not element.tag.startswith(cim):
            found.add(f"element outside the CIM namespace: {element.tag}")
            continue
        cls = element.tag[len(cim):]
        if cls not in schema.parents:
            found.add(f"no class {cls}")
            continue
        for prop in element:
            if prop.tag.startswith(f"{{{GRIDQL_NS}}}"):
                continue  # the extension namespace says what CIM cannot
            if not prop.tag.startswith(cim):
                found.add(f"{cls}: property outside the CIM namespace: {prop.tag}")
                continue
            owner, _, name = prop.tag[len(cim):].partition(".")
            where = f"{cls}: {owner}.{name}"
            if owner not in schema.lineage(cls):
                found.add(f"{where}: {owner} is not {cls} or an ancestor of it")
                continue
            target_type = schema.members.get(owner, {}).get(name)
            if target_type is None:
                found.add(f"{where}: {owner} has no member {name}")
                continue
            reference = prop.get(RESOURCE)
            if (owner, name) in schema.associations:
                if reference is None:
                    found.add(f"{where}: an association, written as text")
                    continue
                target = classes.get(reference.removeprefix("#"))
                if target is None:
                    found.add(f"{where}: points at {reference}, which is not in the document")
                elif target_type not in schema.lineage(target):
                    found.add(f"{where}: points at a {target}, not a {target_type}")
            elif reference is not None:
                found.add(f"{where}: an attribute, written as a reference")
    return sorted(found)


def with_everything():
    """The sample feeder, plus one of every kind of equipment GridQL writes."""
    network = build_sample_network()
    feeder = network.objects["FDR-104"]
    feeder.add_fuse("FU-9", after="REC-001")
    feeder.add_sectionalizer("SEC-9", after="REC-001")
    feeder.add_capacitor("CAP-9", after="REC-001", kvar=300)
    feeder.add_line("LN-9", after="REC-001", length="100ft", conductor="1/0 AL", ampacity=200)
    for mrid, fields in (
        ("PV-9", dict(kind="pv", kw=5, kva=6)),
        ("BAT-9", dict(kind="storage", kw=5, kva=5, kwh=13.5)),
        ("WT-9", dict(kind="wind", kw=20)),
        ("SG-9", dict(kind="synchronous", kw=500, kva=625)),
        ("IG-9", dict(kind="induction", kw=75, kva=80)),
        ("DG-9", dict(kind="diesel", kw=200)),
        ("G-9", dict(kw=10)),
    ):
        feeder.add_generator(mrid, after="LOAD-001", **fields)
    return network


class ConformanceTests(unittest.TestCase):
    def assertConforms(self, document):
        self.assertEqual(nonconformities(document, schema()), [])

    def test_the_sample_network(self):
        self.assertConforms(export_network(build_sample_network()))

    def test_every_kind_of_equipment(self):
        self.assertConforms(export_network(with_everything()))

    def test_a_query_result(self):
        network = with_everything()
        self.assertConforms(export_network(network, network.downstream_of("REC-001")))

    def test_the_ieee_feeders_read_from_gridapps_d(self):
        # Equipment GridQL has no class for goes back out under the class it
        # came in as, so this covers what other tools write too.
        for name in ("IEEE13", "IEEE123", "IEEE8500"):
            with self.subTest(feeder=name):
                self.assertConforms(export_network(load(name).network))


class CheckerTests(unittest.TestCase):
    """The checker has to catch what it is for, or a pass means nothing."""

    def check(self, document):
        return nonconformities(document, schema())

    def test_an_unknown_class_is_caught(self):
        document = export_network(with_everything()).replace(
            "cim:LinearShuntCompensator", "cim:ShuntCapacitorBank"
        )
        self.assertTrue(any("ShuntCapacitorBank" in line for line in self.check(document)))

    def test_a_property_on_the_wrong_class_is_caught(self):
        document = export_network(build_sample_network()).replace(
            "Switch.normalOpen", "Conductor.normalOpen"
        )
        self.assertTrue(any("Conductor is not" in line for line in self.check(document)))

    def test_an_association_pointing_at_the_wrong_class_is_caught(self):
        document = export_network(build_sample_network())
        root = ET.fromstring(document)
        terminal = next(e for e in root if e.tag == f"{{{CIM_NS}}}Terminal")
        node = terminal.find(f"{{{CIM_NS}}}Terminal.ConnectivityNode")
        node.set(RESOURCE, "#SUB-001")
        found = self.check(ET.tostring(root, encoding="unicode"))
        self.assertTrue(any("not a ConnectivityNode" in line for line in found))

    def test_the_pre_release_spellings_gridapps_d_writes_are_caught(self):
        document = export_network(with_everything()).replace(
            "PhotoVoltaicUnit", "PhotovoltaicUnit"
        )
        self.assertIn("no class PhotovoltaicUnit", self.check(document))


class StandardOnlyTests(unittest.TestCase):
    """What a consumer that ignores the gridql: namespace still gets."""

    def test_every_terminal_node_and_end_is_tied_in(self):
        root = ET.fromstring(export_network(with_everything()))

        def of(cls):
            return [e for e in root if e.tag == f"{{{CIM_NS}}}{cls}"]

        def has(element, name):
            return element.find(f"{{{CIM_NS}}}{name}") is not None

        for terminal in of("Terminal"):
            for name in ("Terminal.ConductingEquipment", "Terminal.ConnectivityNode",
                         "ACDCTerminal.sequenceNumber"):
                self.assertTrue(has(terminal, name), (terminal.get(ID), name))
        for node in of("ConnectivityNode"):
            self.assertTrue(has(node, "ConnectivityNode.ConnectivityNodeContainer"), node.get(ID))
        for end in of("PowerTransformerEnd"):
            self.assertTrue(has(end, "TransformerEnd.Terminal"), end.get(ID))
        heads = [
            t for t in of("Terminal") if has(t, "Terminal.NormalHeadFeeder")
        ]
        self.assertEqual(len(heads), 1)
        equipment = heads[0].find(f"{{{CIM_NS}}}Terminal.ConductingEquipment")
        self.assertEqual(equipment.get(RESOURCE), "#BRK-001")

    def test_a_transformers_first_end_is_at_the_terminal_towards_the_source(self):
        root = ET.fromstring(export_network(build_sample_network()))
        end = next(e for e in root if e.get(ID) == "XFMR-001_END_1")
        terminal_id = end.find(f"{{{CIM_NS}}}TransformerEnd.Terminal").get(RESOURCE)[1:]
        terminal = next(e for e in root if e.get(ID) == terminal_id)
        node = terminal.find(f"{{{CIM_NS}}}Terminal.ConnectivityNode").get(RESOURCE)[1:]
        on_node = {
            e.find(f"{{{CIM_NS}}}Terminal.ConductingEquipment").get(RESOURCE)[1:]
            for e in root
            if e.tag == f"{{{CIM_NS}}}Terminal"
            and e.find(f"{{{CIM_NS}}}Terminal.ConnectivityNode").get(RESOURCE)[1:] == node
        }
        self.assertEqual(on_node, {"XFMR-001", "REC-001"})  # the recloser feeds it

    def test_a_tie_node_belongs_to_the_substation_both_feeders_share(self):
        network = build_sample_network()
        other = network.add_feeder("FDR-107", substation="SUB-001")
        other.add_breaker("BRK-107")
        other.add_line("LN-107")
        network.connect("LN-107", "TIE-001")
        root = ET.fromstring(export_network(network))
        node = next(e for e in root if e.get(ID) == "CN_6_LN-107_TIE-001")
        container = node.find(f"{{{CIM_NS}}}ConnectivityNode.ConnectivityNodeContainer")
        self.assertEqual(container.get(RESOURCE), "#SUB-001")

    def test_heads_terminals_and_containers_are_all_said_in_cim(self):
        network = with_everything()
        root = ET.fromstring(export_network(network))
        for element in root:
            for prop in list(element):
                if prop.tag.startswith(f"{{{GRIDQL_NS}}}"):
                    element.remove(prop)
        document = loads_cim(ET.tostring(root, encoding="unicode"))
        self.assertEqual(document.network.objects["FDR-104"].head, "BRK-001")
        self.assertFalse(any("inferred" in note for note in document.report.notes))
        self.assertEqual(
            sorted(o.mrid for o in document.network.downstream_of("REC-001")),
            sorted(o.mrid for o in network.downstream_of("REC-001")),
        )


if __name__ == "__main__":
    unittest.main()
