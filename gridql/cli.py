"""Command line interface: one-shot queries, .gridql files, and a REPL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .version import __version__
from .data import build_sample_network
from .errors import GridQLError, GridQLSyntaxError
from .formats import FORMATS, render, render_script
from .lang import Result, evaluate, parse
from .model import Network
from .model.types import class_for
from .cim import export_network, export_summary, read_cim
from .script import run_file
from .storage import load_network, object_counts, save_network
from .validate import validate

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

Commands:   .help  .types  .format <...>  .run <file.gridql>  .validate  .quit"""

_EPILOG = """examples:
  gridql 'FIND reclosers'
  gridql --format json 'FIND transformers WHERE kva >= 500'
  gridql run queries/large_transformers.gridql
  gridql init grid.sqlite && gridql --db grid.sqlite 'FIND feeders'
  gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-104"'
  gridql import-cim feeder.xml --db imported.sqlite
  gridql validate --db grid.sqlite

With no --db, queries run against the bundled sample feeder FDR-104."""


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
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="query a GridQL SQLite database instead of the bundled sample network",
    )


def network_for(db: str | None) -> Network:
    """The network a command runs against: a database, or the sample."""
    return build_sample_network() if db is None else load_network(db)


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


def build_init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql init",
        description="Create a GridQL SQLite database holding the sample network.",
    )
    parser.add_argument("path", help="database file to create")
    parser.add_argument(
        "--empty", action="store_true", help="create the schema without any network"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite the file if it already exists"
    )
    return parser


def init_database(path: str, empty: bool = False, force: bool = False) -> str:
    if Path(path).exists() and not force:
        raise GridQLError(f"{path} already exists; pass --force to overwrite it")

    network = Network() if empty else build_sample_network()
    save_network(network, path)
    counts = object_counts(path)
    written = ", ".join(f"{count} {table}" for table, count in counts.items() if count)
    return f"wrote {written or 'an empty schema'} to {path}"


def build_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql export-cim",
        description="Write a network, or the slice a query selects, as CIM RDF/XML.",
    )
    parser.add_argument("path", help="file to write, or - for standard output")
    parser.add_argument(
        "--query",
        metavar="GRIDQL",
        default=None,
        help="export only what this query selects, with the containers it needs",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None, help="read the network from this database"
    )
    return parser


def build_import_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql import-cim",
        description="Read a CIM RDF/XML file, reporting what it contained.",
    )
    parser.add_argument("path", help="the CIM file to read")
    parser.add_argument(
        "--db", metavar="PATH", default=None, help="save the imported network to this database"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite the database if it already exists"
    )
    return parser


def build_validate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql validate",
        description="Check a network for the conditions that make its answers unreliable.",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None, help="validate this database"
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    return parser


def run_validate(db: str | None = None, strict: bool = False) -> int:
    """Print a validation report. Exit non-zero when the model is unsound."""
    try:
        report = validate(network_for(db))
    except GridQLError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(report.summary())
    return 1 if report.errors or (strict and report.warnings) else 0


def export_cim(path: str, query: str | None = None, db: str | None = None) -> str:
    network = network_for(db)
    objects = None if query is None else evaluate(network, parse(query)).objects

    if path == "-":
        return export_network(network, objects)

    counts = export_summary(network, objects)
    export_network(network, objects, path=path)
    return (
        f"wrote {counts['devices']} devices, {counts['feeders']} feeders, "
        f"{counts['substations']} substations and {counts['connections']} connections to {path}"
    )


def import_cim(path: str, db: str | None = None, force: bool = False) -> str:
    document = read_cim(path)
    lines = [document.report.summary()]

    report = validate(document.network)
    where = f" --db {db}" if db else ""
    lines.append(
        "validation: no problems found"
        if not report
        else f"validation: {report.counts()} -- run 'gridql validate{where}' for detail"
    )

    if db is not None:
        if Path(db).exists() and not force:
            raise GridQLError(f"{db} already exists; pass --force to overwrite it")
        save_network(document.network, db)
        lines.append(f"saved to {db}")

    return "\n".join(lines)


def repl(network: Network, output_format: str | None, source: str = "sample network FDR-104") -> int:
    print(_BANNER.replace("sample network FDR-104", source))
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
            from .model.types import TYPE_CLASSES, plural

            for key, cls in sorted(TYPE_CLASSES.items()):
                print(f"  {plural(key):16s} -> CIM {cls.CIM_CLASS}")
            continue
        if lowered.startswith(".format"):
            parts = line.split()
            if len(parts) == 2 and parts[1] in FORMATS:
                output_format = parts[1]
                print(f"output format: {output_format}")
            else:
                print(f"usage: .format <{'|'.join(FORMATS)}>")
            continue
        if lowered == ".validate":
            print(validate(network).summary())
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

    if argv and argv[0] == "init":
        args = build_init_parser().parse_args(argv[1:])
        return _emit(lambda: init_database(args.path, args.empty, args.force))

    if argv and argv[0] == "validate":
        args = build_validate_parser().parse_args(argv[1:])
        return run_validate(args.db, args.strict)

    if argv and argv[0] == "export-cim":
        args = build_export_parser().parse_args(argv[1:])
        return _emit(lambda: export_cim(args.path, args.query, args.db))

    if argv and argv[0] == "import-cim":
        args = build_import_parser().parse_args(argv[1:])
        return _emit(lambda: import_cim(args.path, args.db, args.force))

    if argv and argv[0] == "run":
        args = build_run_parser().parse_args(argv[1:])
        return _emit(
            lambda: run_script(network_for(args.db), args.file, args.format, args.explain)
        )

    args = build_parser().parse_args(argv)

    if args.query is None:
        try:
            network = network_for(args.db)
        except GridQLError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return repl(network, args.format, args.db or "sample network FDR-104")

    return _emit(
        lambda: run_query(network_for(args.db), args.query, args.format, args.explain)
    )


if __name__ == "__main__":
    raise SystemExit(main())
