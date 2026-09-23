"""Download the reference feeders the reference-model tests read.

These are the IEEE distribution test feeders as the GridAPPS-D project
publishes them in CIM: written by another tool, so they test the importer
against CIM as the rest of the industry writes it rather than as GridQL
does. They are not committed here -- the repository they come from carries
no licence, and IEEE 8500 is 33 MB -- so the tests that need them skip until
this has been run:

    python3 tests/reference/fetch.py            # all of them
    python3 tests/reference/fetch.py IEEE13     # just one

Each file is pinned to a commit and checked against a digest, so a test
failure means GridQL changed, not the data.
"""

import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/GRIDAPPSD/Powergrid-Models/"
    "eaa0c3dcbc607e01c3f98092e3dea18929ae4d60/models/feeders/CIM/XML/{name}.xml"
)

MODELS = {
    "IEEE13": "a4e2b3d3bcdfc88d07252f0f00a8935f2cb1e8cb0bc3c9bdc399d35635a79ca7",
    "IEEE123": "8752a14d585ebf869e24e1a5c2bc0111da23dfa21952036c83cc29f9ec1c9b1a",
    "IEEE8500": "cf5e46891ab73e3e767c17b2c89de09a8a2b53b61fc735e60b4a588e348d7baa",
}

DIRECTORY = Path(__file__).resolve().parent / "models"


def path_of(name: str) -> Path:
    return DIRECTORY / f"{name}.xml"


def fetch(name: str) -> None:
    target = path_of(name)
    if target.exists() and _digest(target.read_bytes()) == MODELS[name]:
        print(f"{name}: already present")
        return
    try:
        with urllib.request.urlopen(SOURCE.format(name=name)) as response:
            content = response.read()
    except urllib.error.URLError as error:
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(error.reason):
            hint = (
                "\nPython cannot verify HTTPS certificates. On macOS with a python.org "
                "install, run 'Install Certificates.command' from its Applications folder."
            )
        raise SystemExit(f"{name}: download failed: {error.reason}{hint}") from None
    if _digest(content) != MODELS[name]:
        raise SystemExit(f"{name}: downloaded file does not match its pinned digest")
    DIRECTORY.mkdir(exist_ok=True)
    target.write_bytes(content)
    print(f"{name}: {len(content):,} bytes -> {target}")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def main(names: list[str]) -> None:
    unknown = [name for name in names if name not in MODELS]
    if unknown:
        raise SystemExit(f"unknown model: {', '.join(unknown)}; choose from {', '.join(MODELS)}")
    for name in names or MODELS:
        fetch(name)


if __name__ == "__main__":
    main(sys.argv[1:])
