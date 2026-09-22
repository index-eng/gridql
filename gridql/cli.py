"""Command line interface: one-shot queries and an interactive REPL."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .data import build_sample_network
from .errors import GridQLError, GridQLSyntaxError
from .formats import FORMATS, render
from .lang import evaluate, parse
from .model import Network
from .model.types import class_for

_BANNER = f"""GridQL {__version__} -- sample network FDR-104 loaded.
Type a query, '.help' for help, or '.quit' to exit."""

_HELP = """Queries look like:

  FIND reclosers
  FIND switches WHERE state = OPEN
  FIND transformers WHERE kva >= 500
  FIND devices DOWNSTREAM OF "REC-001"
  FIND loads DOWNSTREAM OF "REC-001" WHERE NOT energized

Topology:   DOWNSTREAM OF / UPSTREAM OF / CONNECTED TO / FED BY
Filters:    = != > >= < <= IN (...) CONTAINS, combined with AND / OR / NOT
Units:      13.8kV, 500kVA, 0.5MVA -- bare numbers use the attribute's own unit

Commands:   .help  .types  .format <table|json|csv>  .quit"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql",
        description="Query an electric distribution network in utility terms.",
    )
    parser.add_argument("query", nargs="?", help="a GridQL query; omit to start the REPL")
    parser.add_argument(
        "-f", "--format", default="table", choices=FORMATS, help="output format (default: table)"
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show the parsed query and the plan instead of just running it",
    )
    parser.add_argument("--version", action="version", version=f"gridql {__version__}")
    return parser


def run_query(network: Network, source: str, output_format: str, explain: bool) -> str:
    query = parse(source)
    result = evaluate(network, query)

    if not explain:
        return render(result, output_format)

    plan = [
        "query:",
        *(f"  {line}" for line in query.describe().splitlines()),
        "plan:",
        f"  select {result.type_key} "
        f"({len(network.of_class(class_for(result.type_key)))} candidates)",
    ]
    plan.extend(f"  {relation.kind} -> Network.{relation.method}({relation.target!r})"
                for relation in query.relations)
    if query.where is not None:
        plan.append(f"  filter -> {query.where.describe()}")
    plan.append(f"  {len(result)} matched")
    plan.append("")
    plan.append(render(result, output_format))
    return "\n".join(plan)


def repl(network: Network, output_format: str) -> int:
    print(_BANNER)
    while True:
        try:
            line = input("gridql> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue

        lowered = line.lower()
        if lowered in (".quit", ".exit", "quit", "exit", "\\q"):
            return 0
        if lowered in (".help", "help", "?"):
            print(_HELP)
            continue
        if lowered == ".types":
            from .model.types import TYPE_CLASSES

            for key, cls in sorted(TYPE_CLASSES.items()):
                print(f"  {key + 's':16s} -> CIM {cls.CIM_CLASS}")
            continue
        if lowered.startswith(".format"):
            parts = line.split()
            if len(parts) == 2 and parts[1] in FORMATS:
                output_format = parts[1]
                print(f"output format: {output_format}")
            else:
                print(f"usage: .format <{'|'.join(FORMATS)}>")
            continue

        try:
            print(run_query(network, line, output_format, explain=False))
        except GridQLSyntaxError as error:
            print(error.render(), file=sys.stderr)
        except GridQLError as error:
            print(f"error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = build_sample_network()

    if args.query is None:
        return repl(network, args.format)

    try:
        print(run_query(network, args.query, args.format, args.explain))
    except GridQLSyntaxError as error:
        print(error.render(), file=sys.stderr)
        return 1
    except GridQLError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
