# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

""".gridql files: reusable query artifacts.

A .gridql file is a query an engineer can keep, review, version-control and
run against different datasets, rather than something retyped each time. It
holds one or more statements separated by semicolons, and may carry comments::

    -- Large transformers on the Oakdale circuit
    FIND transformers
    DOWNSTREAM OF "FDR-104"
    WHERE kva >= 500
    SELECT name, mRID, kva
    RETURN table
"""

from __future__ import annotations

from pathlib import Path

from .errors import GridQLError
from .lang import Result, Script, evaluate_script, parse_script
from .model import Network

#: The conventional extension. Files with any extension still run.
SUFFIX = ".gridql"


def read_script(path: str | Path) -> Script:
    """Read and parse a .gridql file."""
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise GridQLError(f"cannot read {path}: {error.strerror or error}") from error
    return parse_script(source, str(path))


def run_file(network: Network, path: str | Path) -> list[Result]:
    """Run every statement of a .gridql file against ``network``, in order."""
    return evaluate_script(network, read_script(path))


def find_scripts(directory: str | Path) -> list[Path]:
    """Every .gridql file in a directory, sorted by name."""
    return sorted(Path(directory).glob(f"*{SUFFIX}"))
