"""Run commands from a project of the tests' own, not the repository's.

The command line finds a project config by walking up from the working
directory, so a test run from the repository reads whatever dataset the
repository's own project.gridqlconfig points at -- a developer's database,
say. Tests that run commands expect the bundled sample network, so they run
from a project that says exactly that.
"""

import contextlib
import os
import tempfile
from pathlib import Path

QUERIES = Path(__file__).resolve().parent.parent / "queries"


@contextlib.contextmanager
def sample_project():
    """A working directory whose project uses the sample network and the bundled queries."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "project.gridqlconfig").write_text(
            'name = "GridQL sample project"\n'
            f'queries = "{QUERIES.as_posix()}"\n'
            "\n[params]\n"
            'feeder = "FDR-104"\n',
            encoding="utf-8",
        )
        previous = Path.cwd()
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(previous)
