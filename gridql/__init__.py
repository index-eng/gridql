"""GridQL -- a query language for electric utility networks.

    >>> from gridql import build_sample_network, execute
    >>> execute(build_sample_network(), 'FIND transformers WHERE kva >= 500').mrids
    ['XFMR-001']
"""

from .data import build_sample_network
from .errors import GridQLError, GridQLNameError, GridQLSyntaxError, UnitError
from .formats import render
from .lang import Result, evaluate, execute, parse
from .model import Network

__version__ = "0.1.0"

__all__ = [
    "GridQLError",
    "GridQLNameError",
    "GridQLSyntaxError",
    "Network",
    "Result",
    "UnitError",
    "__version__",
    "build_sample_network",
    "evaluate",
    "execute",
    "parse",
    "render",
]
