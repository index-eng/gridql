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
"""

from __future__ import annotations

import difflib
from collections import deque
from typing import Any, Iterable, Iterator

from ..errors import GridQLNameError
from .device import MISSING, Device, GridObject, Switch
from .feeder import Feeder, Substation


class Network:
    """An in-memory electric network: objects plus a connectivity graph."""

    def __init__(self) -> None:
        self.objects: dict[str, GridObject] = {}
        self._adjacency: dict[str, set[str]] = {}
        self._topology: _Topology | None = None
        # Bumped by every change the topology is derived from. The cache
        # records the revision it was built at and rebuilds when they differ.
        self._revision = 0
        self._topology_revision = -1

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
        """Resolve an mRID or name, case-insensitively, or raise."""
        obj = self.objects.get(identifier)
        if obj is not None:
            return obj

        wanted = identifier.casefold()
        for candidate in self.objects.values():
            if candidate.mrid.casefold() == wanted or candidate.name.casefold() == wanted:
                return candidate

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

    def neighbors(self, mrid: str) -> set[str]:
        return set(self._adjacency.get(mrid, ()))

    # -- attributes -----------------------------------------------------

    def attribute(self, obj: GridObject, name: str) -> Any:
        """Attribute access for the evaluator, including network-derived ones."""
        key = name.lower()
        if key == "energized":
            return self.is_energized(obj.mrid)
        if key == "depth":
            return self.depth_of(obj.mrid)
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
        return self._resolve(self.topology().descendants(target.mrid))

    def upstream_of(self, identifier: str) -> list[GridObject]:
        """Everything between the target and its feeder head, target excluded."""
        target = self.get(identifier)
        if isinstance(target, (Feeder, Substation)):
            return []
        return self._resolve(self.topology().ancestors(target.mrid))

    def connected_to(self, identifier: str) -> list[GridObject]:
        """Immediate neighbours of the target."""
        target = self.get(identifier)
        if isinstance(target, Feeder):
            return [] if target.head is None else self._resolve(self.neighbors(target.head))
        return self._resolve(self.neighbors(target.mrid))

    def fed_by(self, identifier: str) -> list[GridObject]:
        """Everything the target supplies, the target itself included."""
        target = self.get(identifier)
        if isinstance(target, (Feeder, Substation)):
            return self.downstream_of(identifier)
        return self._resolve([target.mrid, *self.topology().descendants(target.mrid)])

    def is_energized(self, mrid: str) -> Any:
        obj = self.objects.get(mrid)
        if isinstance(obj, Feeder):
            return MISSING if obj.head is None else self.topology().energized(obj.head)
        if isinstance(obj, Substation):
            return MISSING
        return self.topology().energized(mrid)

    def _resolve(self, mrids: Iterable[str]) -> list[GridObject]:
        return [self.objects[m] for m in mrids if m in self.objects]


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
        self._energized: set[str] = set()
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

        for feeder_mrid, own in members.items():
            feeder = network.objects.get(feeder_mrid)
            head = getattr(feeder, "head", None)
            if head and head in own:
                self.unreachable |= {m for m in own if m not in self.parent}

        self._energize(network)

    def _build_feeder(
        self, network: Network, head: str, members: set[str], loops: set[tuple[str, str]]
    ) -> None:
        self.parent[head] = None
        self.root[head] = head
        self.depth[head] = 0
        seen = {head}
        queue = deque([head])

        while queue:
            current = queue.popleft()
            for neighbor in sorted(network.neighbors(current)):
                if neighbor == current:
                    continue  # a self-connection; validation reports it on its own
                if neighbor not in members:
                    continue  # another feeder's equipment, across a tie
                if neighbor in seen:
                    if self.parent.get(current) != neighbor:
                        loops.add((min(current, neighbor), max(current, neighbor)))
                    continue
                seen.add(neighbor)
                self.parent[neighbor] = current
                self.root[neighbor] = head
                self.depth[neighbor] = self.depth[current] + 1
                self.children.setdefault(current, set()).add(neighbor)
                queue.append(neighbor)

    def _energize(self, network: Network) -> None:
        """Flood from every feeder head over the real graph, stopping at open switches.

        An open device is itself still energised -- it has source-side
        potential -- but nothing beyond it is.
        """
        queue = deque(
            feeder.head
            for feeder in sorted(network.feeders, key=lambda f: f.mrid)
            if feeder.head and feeder.head in network.objects
        )
        while queue:
            current = queue.popleft()
            if current in self._energized:
                continue
            self._energized.add(current)
            obj = network.objects.get(current)
            if isinstance(obj, Switch) and obj.is_open:
                continue
            queue.extend(sorted(network.neighbors(current)))

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

    def ancestors(self, mrid: str) -> list[str]:
        chain: list[str] = []
        current = self.parent.get(mrid)
        while current is not None:
            chain.append(current)
            current = self.parent.get(current)
        return chain

    def energized(self, mrid: str) -> bool:
        return mrid in self._energized
