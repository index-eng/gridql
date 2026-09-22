# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exception types shared by every layer of GridQL."""

from __future__ import annotations


class GridQLError(Exception):
    """Base class for every error GridQL raises on purpose."""


class GridQLSyntaxError(GridQLError):
    """The query could not be lexed or parsed."""

    def __init__(self, message: str, source: str = "", position: int = 0):
        super().__init__(message)
        self.message = message
        self.source = source
        self.position = position

    def render(self) -> str:
        """Return the message with the offending line and a caret beneath it."""
        if not self.source:
            return self.message

        pos = max(0, min(self.position, len(self.source)))
        line_start = self.source.rfind("\n", 0, pos) + 1
        line_end = self.source.find("\n", pos)
        if line_end == -1:
            line_end = len(self.source)

        line_no = self.source.count("\n", 0, pos) + 1
        column = pos - line_start
        line = self.source[line_start:line_end]

        return "\n".join(
            [
                f"{self.message} (line {line_no}, column {column + 1})",
                f"  {line}",
                f"  {' ' * column}^",
            ]
        )

    def __str__(self) -> str:
        return self.render()


class GridQLNameError(GridQLError):
    """A device, feeder or type named in the query does not exist."""

    def __init__(self, message: str, suggestions: tuple[str, ...] = ()):
        if suggestions:
            message = f"{message}. Did you mean: {', '.join(suggestions)}?"
        super().__init__(message)
        self.suggestions = suggestions


class UnitError(GridQLError):
    """A unit was unknown, or compared against an incompatible dimension."""
