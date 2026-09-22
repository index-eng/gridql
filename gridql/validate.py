# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model validation: say what is wrong with a network instead of guessing.

A query engine that quietly picks an answer when the model is ambiguous is
worse than one that refuses, because the engineer has no way to tell the two
apart. These checks surface the conditions where GridQL would otherwise have
to choose -- a loop in a circuit that is meant to be radial, a feeder with no
source, equipment pointing at a container that does not exist -- so the model
can be fixed rather than worked around.

Findings are graded:

``error``
    The model is broken and answers derived from it will be wrong.
``warning``
    The model is readable but something will behave unexpectedly, usually by
    returning less than the user expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .model import Device, Feeder, Network, Substation, Switch

ERROR = "error"
WARNING = "warning"

_VALID_STATES = frozenset({"OPEN", "CLOSED"})


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a model."""

    severity: str
    code: str
    message: str
    objects: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.severity:7s} {self.code:22s} {self.message}"


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, code: str, message: str, *objects: str) -> None:
        self.findings.append(Finding(severity, code, message, tuple(objects)))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing is outright broken. Warnings do not clear this flag."""
        return not self.errors

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def __bool__(self) -> bool:
        return bool(self.findings)

    def counts(self) -> str:
        errors, warnings = len(self.errors), len(self.warnings)
        return (
            f"{errors} error{'' if errors == 1 else 's'}, "
            f"{warnings} warning{'' if warnings == 1 else 's'}"
        )

    def summary(self) -> str:
        if not self.findings:
            return "no problems found"
        # Errors first, then in the order they were found.
        ordered = self.errors + self.warnings
        return "\n".join([self.counts(), "", *(str(f) for f in ordered)])


def validate(network: Network) -> ValidationReport:
    """Check a network for the conditions that make its answers unreliable."""
    report = ValidationReport()

    _check_references(network, report)
    _check_feeder_heads(network, report)
    _check_switch_states(network, report)
    _check_connections(network, report)
    _check_structure(network, report)

    return report


# -- references ---------------------------------------------------------


def _check_references(network: Network, report: ValidationReport) -> None:
    """Every container a device names must exist, and be the right kind.

    Grouped by the container: one missing substation on a real feeder is one
    problem to fix, not a finding per device that points at it.
    """
    references: list[tuple[str, str, str, type]] = []
    for device in network.devices:
        references.append((device.mrid, "feeder", device.feeder, Feeder))
        references.append((device.mrid, "substation", device.substation, Substation))
    for feeder in network.feeders:
        references.append((feeder.mrid, "substation", feeder.substation, Substation))

    broken: dict[tuple[str, str, str], list[str]] = {}
    for owner, kind, named, expected in references:
        if named is None:
            continue
        target = network.objects.get(named)
        if target is None:
            problem = "dangling-reference"
            detail = f"{kind} '{named}' does not exist"
        elif not isinstance(target, expected):
            problem = "wrong-container"
            detail = f"{kind} '{named}' is a {target.TYPE}, not a {expected.TYPE}"
        else:
            continue
        broken.setdefault((problem, named, detail), []).append(owner)

    for (problem, named, detail), owners in broken.items():
        report.add(
            ERROR, problem,
            f"{detail}, referenced by {_describe(owners)}",
            named, *owners,
        )


def _describe(owners: list[str], limit: int = 3) -> str:
    if len(owners) <= limit:
        return ", ".join(owners)
    shown = ", ".join(owners[:limit])
    return f"{shown} and {len(owners) - limit} more"


# -- feeders ------------------------------------------------------------


def _check_feeder_heads(network: Network, report: ValidationReport) -> None:
    for feeder in network.feeders:
        members = [d for d in network.devices if d.feeder == feeder.mrid]

        if feeder.head is None:
            if members:
                report.add(
                    WARNING, "headless-feeder",
                    f"{feeder.mrid}: no head device, so DOWNSTREAM OF and UPSTREAM OF "
                    f"return nothing for its {len(members)} devices",
                    feeder.mrid,
                )
            else:
                report.add(
                    WARNING, "empty-feeder",
                    f"{feeder.mrid}: no equipment on this feeder",
                    feeder.mrid,
                )
            continue

        head = network.objects.get(feeder.head)
        if head is None:
            report.add(
                ERROR, "bad-feeder-head",
                f"{feeder.mrid}: head '{feeder.head}' does not exist",
                feeder.mrid, feeder.head,
            )
        elif not isinstance(head, Device):
            report.add(
                ERROR, "bad-feeder-head",
                f"{feeder.mrid}: head '{feeder.head}' is a {head.TYPE}, not equipment",
                feeder.mrid, feeder.head,
            )
        elif head.feeder != feeder.mrid:
            report.add(
                ERROR, "bad-feeder-head",
                f"{feeder.mrid}: head '{feeder.head}' belongs to "
                f"{head.feeder or 'no feeder'}, so the feeder has no reachable source",
                feeder.mrid, feeder.head,
            )


# -- equipment ----------------------------------------------------------


def _check_switch_states(network: Network, report: ValidationReport) -> None:
    for device in network.devices:
        if not isinstance(device, Switch):
            continue
        for attribute in ("state", "normal_state"):
            value = getattr(device, attribute)
            if value not in _VALID_STATES:
                report.add(
                    ERROR, "invalid-state",
                    f"{device.mrid}: {attribute} is '{value}', expected OPEN or CLOSED",
                    device.mrid,
                )


def _check_connections(network: Network, report: ValidationReport) -> None:
    for device in network.devices:
        neighbors = network.neighbors(device.mrid)

        if device.mrid in neighbors:
            report.add(
                ERROR, "self-connection",
                f"{device.mrid}: connected to itself",
                device.mrid,
            )

        if not neighbors and not _is_head(network, device):
            report.add(
                WARNING, "isolated-device",
                f"{device.mrid}: no connections, so no traversal will ever reach it",
                device.mrid,
            )

    _check_cross_feeder_links(network, report)


def _is_head(network: Network, device: Device) -> bool:
    feeder = network.objects.get(device.feeder) if device.feeder else None
    return getattr(feeder, "head", None) == device.mrid


def _check_cross_feeder_links(network: Network, report: ValidationReport) -> None:
    """A hard link between two feeders that is not a tie will not be traversed."""
    for first, second in _edges(network):
        one, two = network.objects.get(first), network.objects.get(second)
        if not isinstance(one, Device) or not isinstance(two, Device):
            continue
        if one.feeder is None or two.feeder is None or one.feeder == two.feeder:
            continue
        if _is_tie(one) or _is_tie(two):
            continue  # a tie between feeders is the normal arrangement
        report.add(
            WARNING, "cross-feeder-link",
            f"{first} ({one.feeder}) is connected to {second} ({two.feeder}) through "
            "no tie switch; feeder traversal will not cross it",
            first, second,
        )


def _is_tie(device: Device) -> bool:
    return isinstance(device, Switch) and (device.is_tie or device.normal_state == "OPEN")


def _edges(network: Network) -> list[tuple[str, str]]:
    return sorted(
        {
            (min(mrid, neighbor), max(mrid, neighbor))
            for mrid in network.objects
            for neighbor in network.neighbors(mrid)
        }
    )


# -- derived structure --------------------------------------------------


def _check_structure(network: Network, report: ValidationReport) -> None:
    """Problems only the traversal can see: loops, islands, unassigned equipment."""
    topology = network.topology()

    for first, second in topology.loop_edges:
        feeder = getattr(network.objects.get(first), "feeder", None)
        report.add(
            WARNING, "loop",
            f"{feeder or 'network'}: {first} and {second} close a loop; the feeder is "
            "not radial, so upstream and downstream follow one arbitrary path",
            first, second,
        )

    for mrid in sorted(topology.unreachable):
        device = network.objects.get(mrid)
        if not network.neighbors(mrid):
            continue  # already reported, more precisely, as isolated
        report.add(
            WARNING, "unreachable",
            f"{mrid}: on {getattr(device, 'feeder', None)} but not reachable from its "
            "head, so it is in no traversal",
            mrid,
        )

    for device in network.devices:
        if device.feeder is None:
            report.add(
                WARNING, "unassigned-device",
                f"{device.mrid}: on no feeder, so it belongs to no feeder tree",
                device.mrid,
            )
