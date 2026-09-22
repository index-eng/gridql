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

    # -- construction ---------------------------------------------------

    def add(self, obj: GridObject) -> GridObject:
        if obj.mrid in self.objects:
            raise ValueError(f"duplicate mRID '{obj.mrid}'")
        self.objects[obj.mrid] = obj
        self._adjacency.setdefault(obj.mrid, set())
        self._invalidate()
        return obj

    def add_substation(self, mrid: str, **kwargs) -> Substation:
        return self.add(Substation(mrid=mrid, **kwargs))  # type: ignore[return-value]

    def add_feeder(self, mrid: str, **kwargs) -> Feeder:
        feeder = Feeder(mrid=mrid, **kwargs)
        self.add(feeder)
        feeder.network = self
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
        self._invalidate()

    def _invalidate(self) -> None:
        self._topology = None

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
        if name.lower() == "energized":
            return self.is_energized(obj.mrid)
        return obj.attribute(name)

    # -- topology -------------------------------------------------------

    def topology(self) -> "_Topology":
        if self._topology is None:
            self._topology = _Topology(self)
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
        return self._resolve(self.topology().descendants(target.mrid) | {target.mrid})

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
    """Derived radial structure: parents, descendants and energisation.

    Rebuilt lazily whenever the network changes, which keeps the model simple
    at the scale this is designed for.
    """

    def __init__(self, network: Network) -> None:
        self.parent: dict[str, str | None] = {}
        self.children: dict[str, set[str]] = {}
        self.root: dict[str, str] = {}
        self._energized: set[str] = set()
        self._build(network)

    def _build(self, network: Network) -> None:
        # Sorted for deterministic ownership when two feeders share a device.
        heads = [f.head for f in sorted(network.feeders, key=lambda f: f.mrid) if f.head]
        seen: set[str] = set()

        for head in heads:
            if head in seen:
                continue
            self.parent[head] = None
            self.root[head] = head
            seen.add(head)
            queue = deque([head])
            while queue:
                current = queue.popleft()
                for neighbor in sorted(network.neighbors(current)):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    self.parent[neighbor] = current
                    self.root[neighbor] = head
                    self.children.setdefault(current, set()).add(neighbor)
                    queue.append(neighbor)

            self._energize(network, head)

    def _energize(self, network: Network, head: str) -> None:
        """Walk the tree from the head, stopping at each open switch.

        The open device itself is still energised -- it has source-side
        potential -- but nothing beyond it is.
        """
        queue = deque([head])
        while queue:
            current = queue.popleft()
            if current in self._energized:
                continue
            self._energized.add(current)
            obj = network.objects.get(current)
            if isinstance(obj, Switch) and obj.is_open:
                continue
            queue.extend(sorted(self.children.get(current, ())))

    def descendants(self, mrid: str) -> set[str]:
        found: set[str] = set()
        queue = deque(self.children.get(mrid, ()))
        while queue:
            current = queue.popleft()
            if current in found:
                continue
            found.add(current)
            queue.extend(self.children.get(current, ()))
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
