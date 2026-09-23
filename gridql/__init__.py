# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GridQL -- a query language for electric utility networks.

Copyright (C) 2026 Index Labs, LLC. Free software under the GNU Affero General
Public License, version 3 or later; see the LICENSE file. There is NO
WARRANTY, to the extent permitted by law.

Using GridQL inside your own organisation carries no obligations. The
licence asks something of you only when you pass copies on, or when you run
a *modified* GridQL as a network service -- in which case its users must be
offered your modified source.

    >>> from gridql import build_sample_network, execute
    >>> execute(build_sample_network(), 'FIND transformers WHERE kva >= 500').mrids
    ['XFMR-001']
"""

from .cim import export_network, import_network, read_cim
from .config import Config, find_config, load_config, project_config
from .data import build_sample_network
from .dss import read_dss
from .ingest import load_csv, read_csv, write_csv
from .errors import (
    GridQLError,
    GridQLNameError,
    GridQLSyntaxError,
    ParameterError,
    UnitError,
)
from .formats import render, render_script
from .lang import (
    Param,
    Result,
    Script,
    bind_script,
    evaluate,
    evaluate_script,
    execute,
    execute_script,
    parse,
    parse_script,
)
from .model import Network
from .script import read_script, run_file
from .storage import load_network, save_network
from .validate import Finding, ValidationReport, validate
from .version import __version__

__all__ = [
    "Config",
    "GridQLError",
    "GridQLNameError",
    "GridQLSyntaxError",
    "Network",
    "Finding",
    "Param",
    "ParameterError",
    "Result",
    "Script",
    "UnitError",
    "ValidationReport",
    "__version__",
    "bind_script",
    "build_sample_network",
    "evaluate",
    "evaluate_script",
    "export_network",
    "execute",
    "find_config",
    "execute_script",
    "import_network",
    "load_config",
    "load_csv",
    "load_network",
    "parse",
    "parse_script",
    "project_config",
    "read_cim",
    "read_csv",
    "read_dss",
    "read_script",
    "render",
    "render_script",
    "run_file",
    "save_network",
    "write_csv",
    "validate",
]
