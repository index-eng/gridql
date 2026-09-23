"""Download the reference feeders the reference-model tests read.

These are the IEEE distribution test feeders as other tools write them: in
CIM as the GridAPPS-D project publishes them, and in OpenDSS -- the IEEE 13
and 123-bus models GridAPPS-D converted those CIM files from, and EPRI's IEEE
8500-node model. They test the importers against models the rest of the
industry wrote, rather than ones written for GridQL. They are not committed
here -- the GridAPPS-D repository carries no licence, and IEEE 8500 is 33 MB
as CIM -- so the tests that need them skip until this has been run:

    python3 tests/reference/fetch.py                 # all of them
    python3 tests/reference/fetch.py IEEE13 IEEE13-dss

Each file is pinned to a commit and checked against a digest, so a test
failure means GridQL changed, not the data.
"""

import hashlib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_GRIDAPPSD = (
    "https://raw.githubusercontent.com/GRIDAPPSD/Powergrid-Models/"
    "eaa0c3dcbc607e01c3f98092e3dea18929ae4d60/models/feeders/"
)
_EPRI = (
    "https://raw.githubusercontent.com/dss-extensions/electricdss-tst/"
    "3b208397160213cae4a9e2d0a7d1aa3528ce26e1/Version8/Distrib/IEEETestCases/"
)

DIRECTORY = Path(__file__).resolve().parent / "models"


@dataclass(frozen=True)
class Model:
    """A model's files, the first of them the one to open."""

    base: str
    files: dict[str, str]  # file name -> sha256
    #: An OpenDSS model is kept in a directory of its own, so its redirects resolve.
    folder: bool = False

    def directory(self, name: str) -> Path:
        return DIRECTORY / name if self.folder else DIRECTORY


MODELS = {
    "IEEE13": Model(_GRIDAPPSD + "CIM/XML/", {
        "IEEE13.xml": "a4e2b3d3bcdfc88d07252f0f00a8935f2cb1e8cb0bc3c9bdc399d35635a79ca7",
    }),
    "IEEE123": Model(_GRIDAPPSD + "CIM/XML/", {
        "IEEE123.xml": "8752a14d585ebf869e24e1a5c2bc0111da23dfa21952036c83cc29f9ec1c9b1a",
    }),
    "IEEE8500": Model(_GRIDAPPSD + "CIM/XML/", {
        "IEEE8500.xml": "cf5e46891ab73e3e767c17b2c89de09a8a2b53b61fc735e60b4a588e348d7baa",
    }),
    "IEEE13-dss": Model(_GRIDAPPSD + "OpenDSS/IEEE/IEEE13_CDPSM/", {
        "IEEE13_CDPSM.dss": "39328234fe1a76f073f9d0bd039a643c637d4678e44356f03d1cb865ba5d351d",
    }, folder=True),
    # The variant that flags its switches switch=yes and opens the two ties,
    # as the CIM file does; IEEE123Master.dss draws them as plain short lines.
    "IEEE123-dss": Model(_GRIDAPPSD + "OpenDSS/IEEE/IEEE123/", {
        "IEEE123Switches.dss": "ac54965aed134faba191bc24fbd4c0e6538a16d6154b49c5f7bb4886acaa20b9",
        "IEEELineCodes.DSS": "44a7c97b8ba033faed9692c5ba63274b4b2ac27dc395695d1b536b41f535ea96",
        "IEEE123Regulators.DSS": "cd6ad77980dc018631ce39faab528afce40b0b9a50a6bc1c9ac8049ffb8207d7",
        "IEEE123Loads.DSS": "fd28f52836205506a1980b52c5a7e12ca585aa626c52a6249502c1c3b2fc2255",
    }, folder=True),
    "IEEE8500-dss": Model(_EPRI + "8500-Node/", {
        "Master.dss": "9bd0e17f33e9ec7e0baa46693abeec069b148cbee8477301d77450f95d601ad8",
        "LineCodes2.DSS": "3fec9199a41696a758eaff7065f86a89477f70898b1bee3295de9c74f154121a",
        "Triplex_Linecodes.dss": "7dfbfc23e19d8930c9e5ac3302bd9e8e9d52aee9c333e3fc80422f15752a886d",
        "Lines.dss": "460eb5e8179bda1926d0d70cf4fc9d8bdd29ab4dd9a101941730749f8a4a663a",
        "Transformers.dss": "cab397f65f5de08c4d82cf794c03c432b404cd5db7db37ff827869db8344b708",
        "LoadXfmrCodes.dss": "422122863efd0268cb125694b0830673baa6ce466157ceea223cb64bcbe0a533",
        "Triplex_Lines.DSS": "abf45521bc05a7f9d5c3fa4c94c4f24f7ea9bc984e7086b303ae4a143d77971d",
        "Loads.dss": "4d5b68a8095bbee59a95ba08255f34b74fcf3d89c0e413df2fc669648fb2d18f",
        "Capacitors.dss": "cc05836176a6715b121619079eb6cef96e77468a3368c8ed44815f2e9d684dcf",
        "CapControls.DSS": "562818b4d905f391e88ed58efcd54150d4296d6cfb355f8abac32c969a290348",
        "Regulators.dss": "041f353f55076feaaf751bbb20551226f8727ddfbdfc5101cf1b1f222da38617",
    }, folder=True),
}


def path_of(name: str) -> Path:
    """The file to open for a model: its only file, or its master."""
    model = MODELS[name]
    return model.directory(name) / next(iter(model.files))


def present(name: str) -> bool:
    model = MODELS[name]
    return all((model.directory(name) / file).exists() for file in model.files)


def fetch(name: str) -> None:
    model = MODELS[name]
    directory = model.directory(name)
    for file, digest in model.files.items():
        target = directory / file
        if target.exists() and _digest(target.read_bytes()) == digest:
            continue
        try:
            with urllib.request.urlopen(model.base + file) as response:
                content = response.read()
        except urllib.error.URLError as error:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in str(error.reason):
                hint = (
                    "\nPython cannot verify HTTPS certificates. On macOS with a python.org "
                    "install, run 'Install Certificates.command' from its Applications folder."
                )
            raise SystemExit(f"{name}/{file}: download failed: {error.reason}{hint}") from None
        if _digest(content) != digest:
            raise SystemExit(f"{name}/{file}: downloaded file does not match its pinned digest")
        directory.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    size = sum((directory / file).stat().st_size for file in model.files)
    print(f"{name}: {len(model.files)} file{'s' if len(model.files) != 1 else ''}, "
          f"{size:,} bytes, checked")


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
