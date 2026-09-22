"""Command line interface: one-shot queries, .gridql files, and a REPL."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .data import build_sample_network
from .errors import GridQLError, GridQLSyntaxError
from .formats import FORMATS, render, render_script
from .lang import Result, evaluate, parse
from .model import Network
from .model.types import class_for
from .script import run_file

_BANNER = f"""GridQL {__version__} -- sample network FDR-104 loaded.
Type a query, '.help' for help, or '.quit' to exit."""

_HELP = """Queries look like:

  FIND reclosers
  FIND switches WHERE state = OPEN
  FIND transformers WHERE kva >= 500
  FIND devices DOWNSTREAM OF "REC-001"
  FIND transformers DOWNSTREAM OF "FDR-104" SELECT name, mRID, kva RETURN table

Clauses:    FIND <type>, topology, WHERE, SELECT, RETURN -- in that order
Topology:   DOWNSTREAM OF / UPSTREAM OF / CONNECTED TO / FED BY
Filters:    = != > >= < <= IN (...) CONTAINS, combined with AND / OR / NOT
Units:      13.8kV, 500kVA, 0.5MVA -- bare numbers use the attribute's own unit

Save a query as a .gridql file and run it with:  gridql run queries/foo.gridql

Commands:   .help  .types  .format <table|json|csv>  .run <file.gridql>  .quit"""

_EPILOG = """examples:
  gridql 'FIND reclosers'
  gridql --format json 'FIND transformers WHERE kva >= 500'
  gridql run queries/large_transformers.gridql"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql",
        description="Query an electric distribution network in utility terms.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="a GridQL query; omit to start the REPL, or use 'gridql run FILE.gridql'",
    )
    _add_shared_arguments(parser)
    parser.add_argument("--version", action="version", version=f"gridql {__version__}")
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql run",
        description="Run the statements of a .gridql file, in order.",
    )
    parser.add_argument("file", help="path to a .gridql file")
    _add_shared_arguments(parser)
    return parser


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-f",
        "--format",
        default=None,
        choices=FORMATS,
        help="output format; overrides any RETURN clause (default: table)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show the parsed query and the plan alongside the result",
    )


def explain(network: Network, result: Result, output_format: str | None) -> str:
    """The parsed statement, how it will be answered, and its result."""
    query = result.query
    lines = [
        "query:",
        *(f"  {line}" for line in query.describe().splitlines()),
        "plan:",
        f"  select {result.type_key} "
        f"({len(network.of_class(class_for(result.type_key)))} candidates)",
    ]
    lines.extend(
        f"  {relation.kind} -> Network.{relation.method}({relation.target!r})"
        for relation in query.relations
    )
    if query.where is not None:
        lines.append(f"  filter -> {query.where.describe()}")
    if query.select is not None:
        lines.append(f"  project -> {', '.join(query.select)}")
    lines.append(f"  {len(result)} matched")
    lines.append("")
    lines.append(render(result, output_format or query.return_format or "table"))
    return "\n".join(lines)


def run_query(
    network: Network, source: str, output_format: str | None = None, explain_plan: bool = False
) -> str:
    result = evaluate(network, parse(source))
    if explain_plan:
        return explain(network, result, output_format)
    return render(result, output_format or result.output_format or "table")


def run_script(
    network: Network, path: str, output_format: str | None = None, explain_plan: bool = False
) -> str:
    results = run_file(network, path)
    if not results:
        return f"{path} contains no statements"
    if explain_plan:
        return "\n\n".join(explain(network, result, output_format) for result in results)
    return render_script(results, output_format)


def repl(network: Network, output_format: str | None) -> int:
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
        if lowered.startswith(".run"):
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                print("usage: .run <file.gridql>")
                continue
            _emit(lambda: run_script(network, parts[1].strip(), output_format))
            continue

        _emit(lambda: run_query(network, line, output_format))


def _emit(produce) -> int:
    """Print what ``produce`` returns, or report the GridQL error it raised."""
    try:
        print(produce())
    except GridQLSyntaxError as error:
        print(error.render(), file=sys.stderr)
        return 1
    except GridQLError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    network = build_sample_network()

    if argv and argv[0] == "run":
        args = build_run_parser().parse_args(argv[1:])
        return _emit(lambda: run_script(network, args.file, args.format, args.explain))

    args = build_parser().parse_args(argv)
    if args.query is None:
        return repl(network, args.format)
    return _emit(lambda: run_query(network, args.query, args.format, args.explain))


if __name__ == "__main__":
    raise SystemExit(main())
