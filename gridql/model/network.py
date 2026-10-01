# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The graph model: object registry, connectivity, and topology traversal.

This is the layer GridQL actually talks to. Storage lives underneath it (a
SQLite loader would populate exactly this API), and the language lives above
it -- neither one knows about the other.

A note on what "downstream" means here: traversal is **topological**, so it
ignores whether switches happen to be open. ``DOWNSTREAM OF "REC-001"`` answers
"what is physically below this recloser", which is the question an engineer is
asking when they plan work. Whether that equipment is currently *energized* is
a separate question, answered by the derived ``energized`` attribute.

Connectivity is recorded two ways, and a network may mix them. A plain
connection joins two devices directly. A **connectivity node** is a point
several devices' terminals meet -- the way GIS exports and CIM record it --
and every device on a node is connected to every other one. The node is
kept, not just the pairs it implies, because the pairs alone cannot tell a
three-way branch point from a ring of three devices.
"""

from __future__ import annotations

import difflib
from collections import deque
from typing import Any, Iterable, Iterator

from ..errors import GridQLError, GridQLNameError
from .device import MISSING, VALID_STATES, Breaker, Device, GridObject, Switch, can_generate
from .feeder import Feeder, Substation


class Network:
    """An in-memory electric network: objects plus a connectivity graph."""

    def __init__(self, source: str | None = None) -> None:
        #: Where the network came from -- a file, or the bundled sample --
        #: so a message about what it lacks can say which data was searched.
        self.source = source
        self.objects: dict[str, GridObject] = {}
        self._adjacency: dict[str, set[str]] = {}
        #: Connectivity node -> the devices attached to it, in attach order.
        self._members: dict[str, list[str]] = {}
        #: Device -> the nodes its terminals are attached to, in attach order.
        self._terminals: dict[str, list[str]] = {}
        self._topology: _Topology | None = None
        # Bumped by every change the topology is derived from. The cache
        # records the revision it was built at and rebuilds when they differ.
        self._revision = 0
        self._topology_revision = -1
        #: Feeder -> its equipment, rebuilt like the topology when stale.
        self._by_feeder: dict[str, list[Device]] = {}
        self._members_revision = -1

    # -- construction ---------------------------------------------------

    def add(self, obj: GridObject) -> GridObject:
        if obj.mrid in self.objects:
            raise ValueError(f"duplicate mRID '{obj.mrid}'")
        self.objects[obj.mrid] = obj
        self._adjacency.setdefault(obj.mrid, set())
        # So the object can report changes the network could not otherwise see.
        obj._network = self
        self.invalidate()
        return obj

    def add_substation(self, mrid: str, **kwargs) -> Substation:
        return self.add(Substation(mrid=mrid, **kwargs))  # type: ignore[return-value]

    def add_feeder(self, mrid: str, **kwargs) -> Feeder:
        feeder = Feeder(mrid=mrid, **kwargs)
        self.add(feeder)
        if feeder.substation is not None and feeder.voltage is None:
            substation = self.objects.get(feeder.substation)
            if isinstance(substation, Substation):
                feeder.voltage = substation.voltage
        return feeder

    def connect(self, a: str | GridObject, b: str | GridObject) -> None:
        """Record an undirected electrical connection between two devices."""
        a_mrid = a.mrid if isinstance(a, GridObject) else a
        b_mrid = b.mrid if isinstance(b, GridObject) else b
        for mrid in (a_mrid, b_mrid):
            if mrid not in self.objects:
                raise GridQLNameError(f"cannot connect unknown device '{mrid}'")
        self._adjacency[a_mrid].add(b_mrid)
        self._adjacency[b_mrid].add(a_mrid)
        self.invalidate()

    def attach(self, device: str | GridObject, node: str) -> None:
        """Attach one of a device's terminals to a connectivity node.

        Every device on a node is connected to every other one on it, so
        attaching both ends of each device is all a node-based source needs.
        Attaching a device to a node it is already on changes nothing.
        """
        mrid = device.mrid if isinstance(device, GridObject) else device
        obj = self.objects.get(mrid)
        if obj is None:
            raise GridQLNameError(f"cannot attach unknown device '{mrid}'")
        if not isinstance(obj, Device):
            raise ValueError(f"'{mrid}' is a {obj.TYPE}, not equipment with terminals")
        if not node:
            raise ValueError(f"{mrid}: a connectivity node needs an identifier")

        members = self._members.setdefault(node, [])
        if mrid in members:
            return
        for other in members:
            self._adjacency[mrid].add(other)
            self._adjacency[other].add(mrid)
        members.append(mrid)
        self._terminals.setdefault(mrid, []).append(node)
        self.invalidate()

    def invalidate(self) -> None:
        """Record that the derived topology is out of date.

        Called for you whenever the model changes in a way the network can
        observe: equipment added, a connection made, a switch operated, a
        feeder head reassigned. Call it yourself only if you reach past all
        of those to mutate the model some other way.
        """
        self._revision += 1

    # -- lookup ---------------------------------------------------------

    def get(self, identifier: str) -> GridObject:
        """Resolve an mRID or name, case-insensitively, or raise.

        An exact mRID wins, then an mRID in another case, then a name. Names
        are rarely unique -- every feeder may have a "Midline Recloser" --
        so a word that names several objects is refused, listing them,
        rather than answered for whichever came first.
        """
        obj = self.objects.get(identifier)
        if obj is not None:
            return obj

        wanted = identifier.casefold()
        for matches in (
            [c for c in self.objects.values() if c.mrid.casefold() == wanted],
            [c for c in self.objects.values() if c.name.casefold() == wanted],
        ):
            if len(matches) == 1:
                return matches[0]
            if matches:
                described = ", ".join(_described(match) for match in matches[:6])
                more = f" and {len(matches) - 6} more" if len(matches) > 6 else ""
                raise GridQLNameError(
                    f"'{identifier}' could mean any of {len(matches)} objects: "
                    f"{described}{more}; name one by its mRID"
                )

        raise GridQLNameError(
            f"no device, feeder or substation named '{identifier}'",
            tuple(difflib.get_close_matches(identifier, self.objects, n=3, cutoff=0.5)),
        )

    def __contains__(self, identifier: str) -> bool:
        return identifier in self.objects

    def __iter__(self) -> Iterator[GridObject]:
        return iter(self.objects.values())

    def __len__(self) -> int:
        return len(self.objects)

    def of_class(self, cls: type) -> list[GridObject]:
        return [obj for obj in self.objects.values() if isinstance(obj, cls)]

    @property
    def devices(self) -> list[Device]:
        return [obj for obj in self.objects.values() if isinstance(obj, Device)]

    @property
    def feeders(self) -> list[Feeder]:
        return [obj for obj in self.objects.values() if isinstance(obj, Feeder)]

    @property
    def substations(self) -> list[Substation]:
        return [obj for obj in self.objects.values() if isinstance(obj, Substation)]

    def members(self, feeder: str) -> list[Device]:
        """The equipment on a feeder, in the order it was added.

        Indexed once per change to the model, so asking for every feeder's
        members costs one pass over the network rather than one per feeder.
        """
        if self._members_revision != self._revision:
            index: dict[str, list[Device]] = {}
            for device in self.devices:
                if device.feeder is not None:
                    index.setdefault(device.feeder, []).append(device)
            self._by_feeder = index
            self._members_revision = self._revision
        return list(self._by_feeder.get(feeder, ()))

    def neighbors(self, mrid: str) -> set[str]:
        return set(self._adjacency.get(mrid, ()))

    @property
    def nodes(self) -> dict[str, list[str]]:
        """Every connectivity node, with the devices attached to it, by mRID."""
        return {node: sorted(members) for node, members in self._members.items()}

    def nodes_of(self, mrid: str) -> list[str]:
        """The connectivity nodes a device is attached to, in attach order."""
        return list(self._terminals.get(mrid, ()))

    def at_node(self, node: str) -> list[str]:
        """The devices attached to a connectivity node, by mRID."""
        return sorted(self._members.get(node, ()))

    def shares_node(self, a: str, b: str) -> bool:
        """Whether two devices are on a common connectivity node."""
        first, second = self._terminals.get(a, ()), self._terminals.get(b, ())
        return any(node in second for node in first)

    def links(self) -> list[tuple[str, str]]:
        """The plain connections: adjacent devices that share no node.

        Together with :attr:`nodes` this is the whole of the connectivity;
        a pair on a common node is already implied by the node.
        """
        return sorted(
            (mrid, neighbor)
            for mrid, neighbors in self._adjacency.items()
            for neighbor in neighbors
            if mrid < neighbor and not self.shares_node(mrid, neighbor)
        )

    # -- attributes -----------------------------------------------------

    def attribute(self, obj: GridObject, name: str) -> Any:
        """Attribute access for the evaluator, including network-derived ones."""
        key = name.lower()
        if key == "energized":
            return self.is_energized(obj.mrid)
        if key == "depth":
            return self.depth_of(obj.mrid)
        if key == "protected_by":
            return self.topology().protector.get(obj.mrid, MISSING)
        return obj.attribute(name)

    def depth_of(self, mrid: str) -> Any:
        """How many devices lie between this one and its feeder head."""
        return self.topology().depth.get(mrid, MISSING)

    # -- topology -------------------------------------------------------

    def topology(self) -> "_Topology":
        """The derived topology, rebuilt only when the model has changed."""
        if self._topology is None or self._topology_revision != self._revision:
            self._topology = _Topology(self)
            self._topology_revision = self._revision
        return self._topology

    def downstream_of(self, identifier: str) -> list[GridObject]:
        """Everything electrically below the target, target excluded."""
        target = self.get(identifier)
        if isinstance(target, Feeder):
            return [obj for obj in self.devices if obj.feeder == target.mrid]
        if isinstance(target, Substation):
            return [obj for obj in self.devices if obj.substation == target.mrid]
        self._require_tree(target, "DOWNSTREAM OF")
        return self._resolve(self.topology().descendants(target.mrid))

    def upstream_of(self, identifier: str) -> list[GridObject]:
        """Everything between the target and its feeder head, target excluded."""
        target = self.get(identifier)
        if isinstance(target, (Feeder, Substation)):
            raise GridQLError(
                f"'{target.mrid}' is a {target.TYPE}, and UPSTREAM OF walks from a piece of "
                "equipment back to its feeder head, so a container has nothing upstream of it; "
                "name a device on it instead"
            )
        self._require_tree(target, "UPSTREAM OF")
        return self._resolve(self.topology().ancestors(target.mrid))

    def connected_to(self, identifier: str) -> list[GridObject]:
        """Immediate neighbours of the target."""
        target = self.get(identifier)
        if isinstance(target, Substation):
            raise GridQLError(
                f"'{target.mrid}' is a substation, which has no terminals of its own, so "
                "nothing is connected to it; name a piece of its equipment, or one of its "
                "feeders' head devices"
            )
        if isinstance(target, Feeder):
            if target.head is None or target.head not in self.objects:
                raise GridQLError(
                    f"CONNECTED TO a feeder means its head device's neighbours, and feeder "
                    f"'{target.mrid}' has no head device; name a piece of its equipment instead"
                )
            return self._resolve(self.neighbors(target.head))
        return self._resolve(self.neighbors(target.mrid))

    def fed_by(self, identifier: str) -> list[GridObject]:
        """Everything the target supplies, the target itself included."""
        target = self.get(identifier)
        if isinstance(target, (Feeder, Substation)):
            return self.downstream_of(identifier)
        self._require_tree(target, "FED BY")
        return self._resolve([target.mrid, *self.topology().descendants(target.mrid)])

    def _require_tree(self, target: GridObject, relation: str) -> None:
        """Refuse a tree relation whose target is on no feeder tree, saying why.

        Off the tree, "below" and "above" have no meaning, and an empty answer
        would read as "nothing there" when the truth is "cannot tell".
        """
        if target.mrid in self.topology().parent:
            return
        problem = self.placement_problem(target)
        raise GridQLError(f"cannot answer {relation} \"{target.mrid}\": {problem}")

    def placement_problem(self, target: GridObject) -> str:
        """Why a device is on no feeder tree, as a sentence fragment."""
        feeder_mrid = getattr(target, "feeder", None)
        if feeder_mrid is None:
            return (
                f"it is on no feeder, so it belongs to no feeder tree and GridQL cannot "
                "tell which way is downstream"
            )
        feeder = self.objects.get(feeder_mrid)
        if not isinstance(feeder, Feeder):
            return f"it names feeder '{feeder_mrid}', which does not exist"
        if feeder.head is None:
            return (
                f"its feeder {feeder.mrid} has no head device, so GridQL cannot tell which "
                "way is downstream; record the feeder's head ('gridql validate' reports "
                "it as headless-feeder)"
            )
        head = self.objects.get(feeder.head)
        if not isinstance(head, Device) or head.feeder != feeder.mrid:
            return (
                f"its feeder {feeder.mrid} names head '{feeder.head}', which is not "
                "equipment on that feeder, so the feeder has no tree"
            )
        return (
            f"it is on {feeder.mrid} but cannot be reached from the feeder's head "
            f"{feeder.head}, so it is on an island and has no place in the tree"
        )

    def protected_by(self, identifier: str) -> list[GridObject]:
        """The target's protection zone, nearest first, the target excluded.

        That is everything below it down to, and including, the next
        protective devices: a fault anywhere in it is the target's to clear,
        and a fault beyond one of those is that device's. A feeder's zone is
        its head's.
        """
        target = self.get(identifier)
        if isinstance(target, Substation):
            raise GridQLError(
                f"'{target.mrid}' is a substation, which has a zone per feeder; "
                "name one of its feeders, or the breaker at its head"
            )
        if isinstance(target, Feeder):
            if target.head is None or target.head not in self.objects:
                raise GridQLError(
                    f"feeder '{target.mrid}' has no head device, so it has no zone of its "
                    "own; record its head, or name the breaker or recloser that protects it"
                )
            feeder, target = target, self.objects[target.head]
            if not getattr(target, "PROTECTIVE", False):
                raise GridQLError(
                    f"feeder '{feeder.mrid}' starts at {target.TYPE} '{target.mrid}', which "
                    "isolates no fault on its own, so the feeder has no zone of its own; "
                    "name the breaker or recloser that protects it instead"
                )
        if not getattr(target, "PROTECTIVE", False):
            raise GridQLError(
                f"'{target.mrid}' is a {target.TYPE}, which isolates no fault on its own, "
                "so it has no protection zone; PROTECTED BY takes a breaker, recloser, "
                "fuse or sectionalizer, and SELECT protected_by shows which one covers it"
            )
        self._require_tree(target, "PROTECTED BY")
        return self._resolve(self.topology().zone(target.mrid))

    def is_energized(self, mrid: str) -> Any:
        """True, False, or MISSING when the model cannot tell.

        A feeder is energised when its head is live *and closed*: a tripped
        feeder breaker is live on its source side, but the feeder is out.
        """
        obj = self.objects.get(mrid)
        if isinstance(obj, Feeder):
            head = self.objects.get(obj.head) if obj.head else None
            if head is None:
                return MISSING
            live = self.topology().energized(head.mrid)
            if live is not True or not isinstance(head, Switch):
                return live
            if head.state not in VALID_STATES:
                return MISSING
            return not head.is_open
        if isinstance(obj, Substation):
            return MISSING
        return self.topology().energized(mrid)

    def infer_heads(self) -> list[str]:
        """Give every headless feeder a source device where one is obvious.

        A feeder is energised through a breaker at the substation, so a
        feeder carrying exactly one breaker has an unambiguous head. Anything
        less clear is left alone and reported: a wrong head would silently
        invert a circuit's topology, which is worse than no topology at all.

        Returns a note per feeder it had to think about.
        """
        notes: list[str] = []
        # Read before any head is set: setting one changes the model, and
        # would rebuild the index once per feeder.
        members = {feeder.mrid: self.members(feeder.mrid) for feeder in self.feeders}
        for feeder in self.feeders:
            if feeder.head is not None:
                continue
            breakers = [
                device.mrid for device in members[feeder.mrid] if isinstance(device, Breaker)
            ]
            if len(breakers) == 1:
                feeder.head = breakers[0]
                notes.append(
                    f"{feeder.mrid}: no head recorded, inferred {breakers[0]} "
                    "as the only breaker on the feeder"
                )
            else:
                notes.append(
                    f"{feeder.mrid}: no head recorded and none could be inferred, so "
                    "topology queries on it are refused and its equipment's energized "
                    "is unknown; set feeder.head to fix"
                )
        return notes

    def _resolve(self, mrids: Iterable[str]) -> list[GridObject]:
        return [self.objects[m] for m in mrids if m in self.objects]


def _described(obj: GridObject) -> str:
    """An object as an ambiguity message lists it: mRID, type, and feeder."""
    feeder = getattr(obj, "feeder", None)
    return f"{obj.mrid} ({obj.TYPE}{f' on {feeder}' if feeder else ''})"


class _Topology:
    """Derived structure: the tree of each feeder, and what is energised.

    Two different questions, deliberately answered two different ways.

    **The feeder tree** is scoped to one feeder's own equipment. Distribution
    feeders are tied to their neighbours through normally open switches, so
    the physical graph runs right across the whole system; a traversal that
    followed it would have one feeder swallow the next. A device belongs to
    exactly one feeder -- CIM puts it in exactly one EquipmentContainer --
    so the tree for a feeder is built from its own members and stops at the
    tie. Adjacency stays physical: CONNECTED TO still reaches across it.

    **Energisation** ignores feeder boundaries and follows the real graph
    from every feeder head, blocked by open switches. That is what makes a
    closed tie back-feed the neighbouring circuit, which is the question an
    engineer is actually asking.

    Rebuilt lazily whenever the network changes.
    """

    def __init__(self, network: Network) -> None:
        self.parent: dict[str, str | None] = {}
        self.children: dict[str, set[str]] = {}
        self.root: dict[str, str] = {}
        #: Edges between a device and its feeder head. The head is at 0.
        self.depth: dict[str, int] = {}
        #: Edges that close a loop inside a feeder. A distribution feeder is
        #: meant to be radial, so these are reported by validation: the tree
        #: had to pick one path and the choice is arbitrary.
        self.loop_edges: list[tuple[str, str]] = []
        #: Devices on a feeder that its head cannot reach.
        self.unreachable: set[str] = set()
        #: Device -> the nearest protective device above it on its feeder.
        self.protector: dict[str, str] = {}
        self._energized: set[str] = set()
        #: Devices whose energisation the model cannot establish.
        self._unknown: set[str] = set()
        self._build(network)

    def _build(self, network: Network) -> None:
        members: dict[str, set[str]] = {}
        for device in network.devices:
            if device.feeder is not None:
                members.setdefault(device.feeder, set()).add(device.mrid)

        loops: set[tuple[str, str]] = set()
        for feeder in sorted(network.feeders, key=lambda f: f.mrid):
            own = members.get(feeder.mrid, set())
            if feeder.head and feeder.head in own:
                self._build_feeder(network, feeder.head, own, loops)

        self.loop_edges = sorted(loops)
        self._protect(network)

        for feeder_mrid, own in members.items():
            feeder = network.objects.get(feeder_mrid)
            head = getattr(feeder, "head", None)
            if head and head in own:
                self.unreachable |= {m for m in own if m not in self.parent}

        self._energize(network)

    def _build_feeder(
        self, network: Network, head: str, members: set[str], loops: set[tuple[str, str]]
    ) -> None:
        """Walk one feeder out from its head, breadth first.

        Devices on a common connectivity node are all adjacent, so at a
        three-way branch point the two outgoing devices are neighbours of
        each other as well as of the one feeding them. Walked as plain
        edges, that triangle would read as a loop. So a node is walked as a
        node: whoever reaches it first is the parent of everything else on
        it, and the node is then spent. Only reaching a device, or a node, a
        second way closes a loop. Plain connections are walked as edges.

        A normally open switch is walked through last. A loop inside one
        feeder is closed by such a switch -- that is what makes the feeder
        radial in its normal configuration -- so the tree must break the
        loop there rather than wherever the walk happens to meet itself.
        Equipment reachable only through one is still below it; it is just
        reached after everything the normal paths reach. The switch's
        present position plays no part, so operating one moves nothing.
        """
        self.parent[head] = None
        self.root[head] = head
        self.depth[head] = 0
        seen = {head}
        #: The node each device was reached through; None for a plain edge.
        via: dict[str, str | None] = {head: None}
        #: Node -> the device that walked it.
        walked: dict[str, str] = {}
        queue = deque([head])

        def reach(device: str, parent: str, node: str | None) -> None:
            seen.add(device)
            via[device] = node
            self.parent[device] = parent
            self.root[device] = head
            self.depth[device] = self.depth[parent] + 1
            self.children.setdefault(parent, set()).add(device)
            queue.append(device)

        def normally_open(mrid: str) -> bool:
            obj = network.objects.get(mrid)
            return isinstance(obj, Switch) and obj.normal_state == "OPEN"

        def loop(first: str, second: str) -> None:
            if normally_open(first) or normally_open(second):
                return  # the feeder's own open point, not a fault in it
            loops.add((min(first, second), max(first, second)))

        #: Normally open switches reached but not yet walked through.
        held: deque[str] = deque()
        released: set[str] = set()

        while queue or held:
            if not queue:
                current = held.popleft()
                released.add(current)
            else:
                current = queue.popleft()
                if current != head and normally_open(current) and current not in released:
                    held.append(current)
                    continue

            for node in network.nodes_of(current):
                if node == via[current]:
                    continue
                if node in walked:
                    loop(current, walked[node])
                    continue
                walked[node] = current
                for neighbor in sorted(network.at_node(node)):
                    if neighbor == current or neighbor not in members:
                        continue
                    if neighbor in seen:
                        loop(current, neighbor)
                    else:
                        reach(neighbor, current, node)

            for neighbor in sorted(network.neighbors(current)):
                if neighbor == current:
                    continue  # a self-connection; validation reports it on its own
                if neighbor not in members:
                    continue  # another feeder's equipment, across a tie
                if network.shares_node(current, neighbor):
                    continue  # walked with the node they share
                if neighbor in seen:
                    if self.parent.get(current) != neighbor:
                        loop(current, neighbor)
                    continue
                reach(neighbor, current, None)

    def _protect(self, network: Network) -> None:
        """Record, for every device on a tree, the protective device above it.

        Walked in the order the trees were built, which reaches every parent
        before its children, so each device inherits its parent's answer
        unless the parent is itself protective.
        """
        for mrid, parent in self.parent.items():
            if parent is None:
                continue
            if getattr(network.objects.get(parent), "PROTECTIVE", False):
                self.protector[mrid] = parent
            elif parent in self.protector:
                self.protector[mrid] = self.protector[parent]

    def _energize(self, network: Network) -> None:
        """Flood from every feeder head over the real graph, stopping at open switches.

        An open device is itself still energised -- it has source-side
        potential -- but nothing beyond it is.

        Three things leave the answer unknown rather than false, because the
        model cannot say: a switch whose state is neither OPEN nor CLOSED
        (whatever is beyond it may or may not be live), equipment on a
        feeder with no head (its source is not recorded), and generation the
        feeder heads do not reach. A section cut off with a generator or an
        inverter on it is not known to be dead: whether that backfeeds it
        depends on anti-islanding and dispatch, which the model does not
        hold, and calling it dead is the dangerous mistake. Unknown spreads
        the way energy would, stopping at open switches, and never covers
        equipment a known source already reaches.
        """
        feeders = sorted(network.feeders, key=lambda f: f.mrid)
        queue = deque(
            feeder.head for feeder in feeders if feeder.head and feeder.head in network.objects
        )
        #: Switches with an unreadable state that the flood reached.
        blocked: list[str] = []
        while queue:
            current = queue.popleft()
            if current in self._energized:
                continue
            self._energized.add(current)
            obj = network.objects.get(current)
            if isinstance(obj, Switch):
                if obj.state not in VALID_STATES:
                    blocked.append(current)
                    continue
                if obj.is_open:
                    continue
            queue.extend(sorted(network.neighbors(current)))

        sourced = {
            feeder.mrid for feeder in feeders if feeder.head and feeder.head in network.objects
        }
        unknown = deque(
            neighbor for mrid in blocked for neighbor in sorted(network.neighbors(mrid))
        )
        unknown.extend(
            device.mrid
            for device in network.devices
            if device.feeder is not None and device.feeder not in sourced
        )
        unknown.extend(device.mrid for device in network.devices if can_generate(device))
        while unknown:
            current = unknown.popleft()
            if current in self._energized or current in self._unknown:
                continue
            self._unknown.add(current)
            obj = network.objects.get(current)
            if isinstance(obj, Switch) and obj.is_open:
                continue
            unknown.extend(sorted(network.neighbors(current)))

    def descendants(self, mrid: str) -> list[str]:
        """Everything below ``mrid``, nearest first.

        Breadth-first and alphabetical within a level, so the walking order
        is both meaningful and reproducible.
        """
        found: list[str] = []
        seen: set[str] = set()
        queue = deque(sorted(self.children.get(mrid, ())))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            found.append(current)
            queue.extend(sorted(self.children.get(current, ())))

        # A plain breadth-first walk groups a level by parent. Sorting on
        # (depth, mRID) puts the whole level together in a predictable order,
        # and makes this agree with how the language reports the same query.
        found.sort(key=lambda candidate: (self.depth.get(candidate, 0), candidate))
        return found

    def zone(self, mrid: str) -> list[str]:
        """The descendants of ``mrid`` it is the nearest protection for.

        Ordered like :meth:`descendants`, of which it is a subset.
        """
        return [m for m in self.descendants(mrid) if self.protector.get(m) == mrid]

    def ancestors(self, mrid: str) -> list[str]:
        chain: list[str] = []
        current = self.parent.get(mrid)
        while current is not None:
            chain.append(current)
            current = self.parent.get(current)
        return chain

    def energized(self, mrid: str) -> Any:
        """True, False, or MISSING when the model cannot tell."""
        if mrid in self._energized:
            return True
        if mrid in self._unknown:
            return MISSING
        return False
