# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Terminal colour: when to use it, and what it marks.

Colour marks what needs attention, never a switch's position as such.
Utilities do not agree on what red and green mean for a switch -- North
American one-lines draw a closed breaker red and an open one green, and
anyone outside the industry reads red as the alarm -- so colouring OPEN and
CLOSED would mislead one reader or the other. What is worth a second look
is the same everywhere: equipment out of its normal position, and
equipment that is dead.

Only the table is coloured. JSON, CSV and CIM are data for another program
and stay plain whatever is asked.
"""

from __future__ import annotations

import os
from typing import TextIO

CHOICES = ("auto", "always", "never")

#: Select Graphic Rendition codes for each thing the output marks.
_ROLES = {
    "header": "1",  # bold
    "muted": "2",  # dim: rules, empty cells, row counts
    "attention": "1;33",  # bold yellow: off-normal, de-energised
    "error": "1;31",  # bold red
    "warning": "1;33",  # bold yellow
    "ok": "32",  # green
}


class Palette:
    """Paints text for one output stream, or leaves it alone."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def paint(self, text: str, role: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{_ROLES[role]}m{text}\033[0m"

    @classmethod
    def for_stream(cls, stream: TextIO, choice: str = "auto") -> "Palette":
        return cls(wanted(stream, choice))


PLAIN = Palette(False)


def wanted(stream: TextIO, choice: str = "auto") -> bool:
    """Whether to colour ``stream``.

    An explicit ``always`` or ``never`` wins. Otherwise NO_COLOR turns it
    off and FORCE_COLOR on, as their conventions ask, and failing both it
    is on only for a terminal that can show it -- never for a pipe or file.
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except ValueError:  # a closed stream
        return False
