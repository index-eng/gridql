"""GridQL -- a query language for electric utility networks.

    >>> from gridql import build_sample_network, execute
    >>> execute(build_sample_network(), 'FIND transformers WHERE kva >= 500').mrids
    ['XFMR-001']
"""

from .data import build_sample_network
from .errors import GridQLError, GridQLNameError, GridQLSyntaxError, UnitError
from .formats import render, render_script
from .lang import Result, Script, evaluate, evaluate_script, execute, execute_script, parse, parse_script
from .model import Network
from .script import read_script, run_file

__version__ = "0.1.0"

__all__ = [
    "GridQLError",
    "GridQLNameError",
    "GridQLSyntaxError",
    "Network",
    "Result",
    "Script",
    "UnitError",
    "__version__",
    "build_sample_network",
    "evaluate",
    "evaluate_script",
    "execute",
    "execute_script",
    "parse",
    "parse_script",
    "read_script",
    "render",
    "render_script",
    "run_file",
]
