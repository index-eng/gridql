# SPDX-FileCopyrightText: 2026 Index Labs, LLC
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command line interface: one-shot queries, .gridql files, and a REPL."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from .version import __version__
from .color import CHOICES, PLAIN, Palette
from .config import CONFIG_NAME, Config, project_config, resolve_script
from .data import build_sample_network
from .errors import GridQLError, GridQLSyntaxError
from .formats import FORMATS, render, render_script
from .lang import Result, evaluate, evaluate_script, parse
from .model import Network
from .model.types import class_for
from .cim import export_network, export_summary, read_cim
from .ingest import read_csv, write_csv
from .script import read_script
from .storage import StorageError, feeder_ids, load_network, object_counts, save_network
from .validate import ValidationReport, validate

COPYRIGHT = "Copyright (C) 2026 Index Labs, LLC"

#: Shown by --version and by the REPL command, as AGPL section 5(d) asks of
#: an interactive interface.
_LICENSE_NOTICE = f"""GridQL {__version__} -- a query language for electric utility networks
{COPYRIGHT}

This program is free software: you may redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the licence, or (at your option) any
later version. It comes with ABSOLUTELY NO WARRANTY. See the LICENSE file, or
<https://www.gnu.org/licenses/agpl-3.0.html>.

Running GridQL inside your own organisation carries no obligations. The licence
asks something of you only when you pass copies on, or when you offer a MODIFIED
GridQL to users over a network -- who must then be offered your modified source."""


def _banner(source: str) -> str:
    return (
        f"GridQL {__version__}  {COPYRIGHT}\n"
        "Free software under AGPL-3.0-or-later, with NO WARRANTY; "
        "type '.license' for details.\n"
        f"Loaded: {source}.\n"
        "Type a query, '.help' for help, or '.quit' to exit."
    )

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
A file may declare PARAM feeder = "FDR-104" and use $feeder; supply another
with:  gridql run queries/foo.gridql --feeder FDR-201

Commands:   .help  .types  .format <...>  .run <file> [name=value ...]
            .config  .validate  .license  .quit"""

_EPILOG = """examples:
  gridql 'FIND reclosers'
  gridql --format json 'FIND transformers WHERE kva >= 500'
  gridql run queries/large_transformers.gridql
  gridql run queries/feeder_report.gridql --feeder FDR-104 --min_kva 0.5MVA
  gridql config
  gridql init grid.sqlite && gridql --db grid.sqlite 'FIND feeders'
  gridql export-cim feeder.xml --query 'FIND devices FED BY "FDR-104"'
  gridql import-cim feeder.xml --db imported.sqlite
  gridql validate --db grid.sqlite
  gridql --csv ./gis-export 'FIND transformers WHERE kva >= 500'
  gridql import-csv ./gis-export --db grid.sqlite
  gridql import-csv ./gis-export --mapping gis-export.toml

With no --db and no project.gridqlconfig, queries run against the bundled
sample feeder FDR-104. Run 'gridql config' to see what is in effect."""


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
    parser.add_argument("--version", action="version", version=_LICENSE_NOTICE)
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql run",
        description="Run the statements of a .gridql file, in order.",
        epilog=(
            "A file's PARAM declarations are supplied as options:\n"
            "  gridql run export_feeder.gridql --feeder FDR-104\n"
            "  gridql run export_feeder.gridql --param feeder=FDR-104\n\n"
            "Use --param for a parameter whose name is also a gridql option."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # A parameter must not be swallowed as an abbreviation of an option.
        allow_abbrev=False,
    )
    parser.add_argument("file", help="a .gridql file, or a query name in the project")
    parser.add_argument(
        "--param",
        metavar="NAME=VALUE",
        action="append",
        default=[],
        dest="params",
        help="supply a PARAM the file declares; repeatable",
    )
    _add_shared_arguments(parser)
    return parser


def build_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql config",
        description=f"Show the {CONFIG_NAME} in effect, and the queries it points at.",
    )
    _add_config_arguments(parser)
    _add_color_argument(parser)
    return parser


def show_config(explicit: str | None = None, disabled: bool = False) -> str:
    config = project_config(explicit, not disabled)
    lines = [config.describe()]
    scripts = config.scripts()
    if scripts:
        lines.append("")
        lines.append(f"{len(scripts)} quer{'y' if len(scripts) == 1 else 'ies'}:")
        lines.extend(f"  {script.stem}" for script in scripts)
    return "\n".join(lines)


#: The options 'gridql run' defines itself, and whether each takes a value.
#: Anything else on the command line is a parameter the file declares, which
#: is how --feeder FDR-104 can mean what the file says without gridql having
#: heard of a feeder.
#: The --color choice of the command being run. main() sets it, so an error
#: raised deep inside loading data is coloured the way the command asked.
_color = "auto"


def _use_color(choice: str) -> None:
    global _color
    _color = choice


def _stdout() -> Palette:
    return Palette.for_stream(sys.stdout, _color)


def _say(kind: str, message: object) -> None:
    """An 'error:' or 'warning:' line on stderr."""
    label = Palette.for_stream(sys.stderr, _color).paint(f"{kind}:", kind)
    print(f"{label} {message}", file=sys.stderr)


RUN_OPTIONS = {
    "-f": True,
    "--format": True,
    "--db": True,
    "--csv": True,
    "--config": True,
    "--mapping": True,
    "--param": True,
    "--color": True,
    "--explain": False,
    "--no-config": False,
    "-h": False,
    "--help": False,
}


def split_parameters(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Separate a file's parameters from gridql's own arguments.

    Done before argparse rather than after, because argparse cannot know that
    an option it has never heard of takes a value -- left to itself it reads
    ``run --feeder FDR-104 report`` as a file named FDR-104.
    """
    remaining: list[str] = []
    values: dict[str, str] = {}

    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1

        if not token.startswith("--") or RUN_OPTIONS.get(token) is not None:
            remaining.append(token)
            # gridql's own option: its value travels with it.
            if RUN_OPTIONS.get(token) and index < len(argv):
                remaining.append(argv[index])
                index += 1
            continue

        name, separator, inline = token[2:].partition("=")
        if not name:
            raise GridQLError(f"expected a parameter name in '{token}'")
        if RUN_OPTIONS.get(f"--{name}") is not None:
            remaining.append(token)
            continue

        if separator:
            values[name] = inline
            continue

        following = argv[index] if index < len(argv) else None
        if following is None or following.startswith("--"):
            raise GridQLError(f"--{name} needs a value")
        values[name] = following
        index += 1

    return remaining, values


def parameters(supplied: list[str], options: dict[str, str]) -> dict[str, str]:
    """Merge ``--param name=value`` entries with the parameters given as options.

    Both spellings exist because a parameter named ``format`` would otherwise
    be unreachable behind gridql's own option of that name.
    """
    values: dict[str, str] = {}

    for entry in supplied:
        name, separator, value = entry.partition("=")
        if not separator or not name.strip():
            raise GridQLError(f"--param takes NAME=VALUE, not '{entry}'")
        values[name.strip()] = value

    values.update(options)
    return values


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
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="query a directory of CSV files (or a single devices file) directly",
    )
    _add_mapping_argument(parser)
    _add_config_arguments(parser)
    _add_color_argument(parser)


def _add_color_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--color",
        choices=CHOICES,
        default="auto",
        help="colour a table's header, off-normal switches and dead equipment, and errors "
        "(default: auto -- on for a terminal, off for a pipe or file, off under NO_COLOR)",
    )


def _add_mapping_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mapping",
        metavar="PATH",
        default=None,
        help="a TOML file saying what the CSV files' columns mean",
    )


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"use this {CONFIG_NAME} instead of searching for one",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help=f"ignore any {CONFIG_NAME} found above the working directory",
    )


def network_for(
    db: str | None = None,
    csv: str | None = None,
    config: Config | None = None,
    mapping: str | None = None,
) -> Network:
    """The network a command runs against: a database, CSV files, or the sample.

    The project config supplies the dataset when the command line does not,
    which is what lets ``gridql 'FIND feeders'`` mean the utility's own data
    inside a project directory. Its mapping describes the utility's export
    format, so it applies to any CSV read here unless --mapping names another.
    """
    for option, value in (("--csv", csv), ("--db", db), ("--mapping", mapping)):
        _refuse_query_as_path(option, value)
    if db and csv:
        raise GridQLError("pass either --db or --csv, not both")
    if not db and not csv and config is not None:
        db, csv = config.db, config.csv
    if mapping and not csv:
        raise GridQLError("--mapping says what CSV columns mean; use it with --csv")
    if csv:
        mapping = mapping or (config.mapping if config else None)
        document = read_csv(csv, mapping)
        _warn_about_load(document, csv, mapping)
        return document.network
    if db:
        return load_network(db)
    network = build_sample_network()
    # Said where the network is named, so an error about what it lacks
    # explains why this data and not the user's own.
    network.source += ", used because no --db, --csv or project dataset was given"
    return network


#: What each dataset option takes, and an example of it, for the message
#: that catches a query given in its place.
_DATASET_OPTIONS = {
    "--csv": ("the folder holding your CSV files", "./gis-export"),
    "--db": ("a database file", "grid.sqlite"),
    "--mapping": ("a mapping file", "gis-export.toml"),
}


def _refuse_query_as_path(option: str, value: str | None) -> None:
    """Catch ``--csv "FIND ..."``: the option took the query as its path.

    Left alone it fails as "no device file at find transformers ...", which
    names the symptom and hides the cause -- the path is missing and the
    query was swallowed.
    """
    if not value or Path(value).exists():
        return
    words = value.split(maxsplit=1)
    if not words or words[0].upper() != "FIND":
        return
    what, example = _DATASET_OPTIONS[option]
    raise GridQLError(
        f"{option} takes {what}, but was given the query. Put the path first, then the "
        f"query: gridql {option} {example} {shlex.quote(value)}"
    )


def _warn_about_load(document, csv: str, mapping: str | None) -> None:
    """Say so on stderr when CSV files loaded badly, and still run the query.

    A query shows only its answer, so without this a file whose every row
    was skipped reads as "no transformers" -- a true statement about the
    wrong problem. import-csv prints the whole report; this points at it.
    """
    report = document.report
    if not report.problems and document.network.devices:
        return
    count = len(report.problems)
    skipped = f"{count} row{'s' if count != 1 else ''} skipped or incomplete"
    if not document.network.devices:
        headline = f"no equipment loaded from {csv}" + (f": {skipped}" if count else "")
    else:
        headline = f"{skipped} in {csv}"
    if count:
        headline += f", starting with {report.problems[0]}"
    command = f"gridql import-csv {shlex.quote(csv)}" + (
        f" --mapping {shlex.quote(mapping)}" if mapping else ""
    )
    _say("warning", headline)
    print(f"  run '{command}' for the full report", file=sys.stderr)


def source_of(db: str | None, csv: str | None, config: Config | None = None) -> str:
    """How to describe what a command is querying, for the REPL banner."""
    if not db and not csv and config is not None and config:
        name = f"{config.name}: " if config.name else ""
        return f"{name}{config.dataset()}"
    return db or csv or "sample network FDR-104"


def explain(
    network: Network, result: Result, output_format: str | None, palette: Palette = PLAIN
) -> str:
    """The parsed statement, how it will be answered, and its result."""
    query = result.query
    lines = [
        palette.paint("query:", "header"),
        *(f"  {line}" for line in query.describe().splitlines()),
        palette.paint("plan:", "header"),
        f"  select {result.type_key} "
        f"({len(network.of_class(class_for(result.type_key)))} candidates)",
    ]
    lines.extend(
        f"  {relation.kind} -> Network.{relation.method}({relation.target!r})"
        for relation in query.relations
    )
    if query.where is not None:
        lines.append(f"  filter -> {query.where.describe()}")
    if result.select is not None:
        lines.append(f"  project -> {', '.join(i.written for i in result.select)}")
    if query.group_by:
        lines.append(f"  group by -> {', '.join(query.group_by)}")
    if query.order_by:
        lines.append(f"  order by -> {', '.join(k.describe() for k in query.order_by)}")
    if query.limit is not None:
        lines.append(f"  limit -> {query.limit}")
    lines.append(f"  {len(result)} matched, {len(result.rows())} row(s) out")
    lines.append("")
    lines.append(render(result, output_format or query.return_format or "table", palette))
    return "\n".join(lines)


def run_query(
    network: Network,
    source: str,
    output_format: str | None = None,
    explain_plan: bool = False,
    palette: Palette = PLAIN,
) -> str:
    result = evaluate(network, parse(source))
    if explain_plan:
        return explain(network, result, output_format, palette)
    return render(result, output_format or result.output_format or "table", palette)


def run_script(
    network: Network,
    path: str,
    output_format: str | None = None,
    explain_plan: bool = False,
    params: dict[str, str] | None = None,
    defaults: dict[str, object] | None = None,
    palette: Palette = PLAIN,
) -> str:
    """Run a .gridql file, binding its parameters before anything else.

    ``defaults`` are the project config's parameters. They apply only where
    the file declares them, so one project-wide ``feeder`` does not make
    every unrelated query fail; ``params`` came from the command line, so an
    unknown one is a typo worth reporting.
    """
    script = read_script(path)
    values = dict(params or {})
    named = {key.lower() for key in values}
    for key, value in (defaults or {}).items():
        if key.lower() not in named and script.param(key) is not None:
            values[key] = value

    results = evaluate_script(network, script, values)
    if not results:
        return f"{path} contains no statements"
    if explain_plan:
        return "\n\n".join(
            explain(network, result, output_format, palette) for result in results
        )
    return render_script(results, output_format, palette)


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
    _add_color_argument(parser)
    return parser


def init_database(path: str, empty: bool = False, force: bool = False) -> str:
    if Path(path).exists() and not force:
        raise GridQLError(f"{path} already exists; pass --force to overwrite it")

    network = Network() if empty else build_sample_network()
    save_network(network, path)
    counts = object_counts(path)
    written = ", ".join(f"{count} {table}" for table, count in counts.items() if count)
    return f"wrote {written or 'an empty schema'} to {path}"


def build_import_csv_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql import-csv",
        description="Read a directory of CSV files, reporting what they contained.",
    )
    parser.add_argument("path", help="directory of CSV files, or a single devices file")
    parser.add_argument(
        "--db", metavar="PATH", default=None, help="save the loaded network to this database"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite the database if it already exists"
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="replace an existing database even when the refresh checks object",
    )
    _add_mapping_argument(parser)
    _add_config_arguments(parser)
    _add_color_argument(parser)
    return parser


def build_export_csv_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridql export-csv",
        description="Write a network out as CSV files.",
    )
    parser.add_argument("path", help="directory to write into")
    parser.add_argument("--db", metavar="PATH", default=None, help="read from this database")
    parser.add_argument("--csv", metavar="PATH", default=None, help="read from these CSV files")
    _add_mapping_argument(parser)
    _add_config_arguments(parser)
    _add_color_argument(parser)
    return parser


def import_csv(
    path: str,
    db: str | None = None,
    force: bool = False,
    mapping: str | None = None,
    config: Config | None = None,
    skip_checks: bool = False,
) -> str:
    """Read CSV files and report what they held -- the way to try out a mapping."""
    document = read_csv(path, mapping or (config.mapping if config else None))
    lines = [document.report.summary()]

    report = validate(document.network)
    # Point at the files just read, not the database: a refused save leaves
    # the database holding the old data.
    where = f" --csv {path}" + (f" --mapping {mapping}" if mapping else "")
    lines.append(
        "validation: no problems found"
        if not report
        else f"validation: {report.counts()} -- run 'gridql validate{where}' for detail"
    )

    if db is not None:
        _save_import(document.network, report, db, force, skip_checks, lines)
        lines.extend(_where_to_query(db))
    else:
        # Without --db this is a trial run, and a plain 'gridql' afterwards
        # would answer from the project's own dataset instead.
        query = f"gridql --csv {shlex.quote(path)}" + (
            f" --mapping {shlex.quote(mapping)}" if mapping else ""
        )
        lines.append(
            "not saved: pass --db PATH to keep it, or query the files in place with "
            f"'{query} <query>'"
        )

    return "\n".join(lines)


def export_csv(
    path: str,
    db: str | None = None,
    csv: str | None = None,
    config: Config | None = None,
    mapping: str | None = None,
) -> str:
    network = network_for(db, csv, config, mapping)
    written = write_csv(network, path)
    return f"wrote {', '.join(p.name for p in written)} to {path}"


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
    parser.add_argument(
        "--csv", metavar="PATH", default=None, help="read the network from CSV files"
    )
    _add_mapping_argument(parser)
    _add_config_arguments(parser)
    _add_color_argument(parser)
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
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="replace an existing database even when the refresh checks object",
    )
    _add_color_argument(parser)
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
        "--csv", metavar="PATH", default=None, help="validate these CSV files"
    )
    _add_mapping_argument(parser)
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    _add_config_arguments(parser)
    _add_color_argument(parser)
    return parser


def run_validate(
    db: str | None = None,
    strict: bool = False,
    csv: str | None = None,
    config: Config | None = None,
    mapping: str | None = None,
) -> int:
    """Print a validation report. Exit non-zero when the model is unsound."""
    try:
        report = validate(network_for(db, csv, config, mapping))
    except GridQLError as error:
        _say("error", error)
        return 1

    print(report.summary(_stdout()))
    return 1 if report.errors or (strict and report.warnings) else 0


def export_cim(
    path: str,
    query: str | None = None,
    db: str | None = None,
    csv: str | None = None,
    config: Config | None = None,
    mapping: str | None = None,
) -> str:
    network = network_for(db, csv, config, mapping)
    objects = None if query is None else evaluate(network, parse(query)).objects

    if path == "-":
        return export_network(network, objects)

    counts = export_summary(network, objects)
    export_network(network, objects, path=path)
    return (
        f"wrote {counts['devices']} devices, {counts['feeders']} feeders, "
        f"{counts['substations']} substations and {counts['connections']} connections to {path}"
    )


def import_cim(
    path: str,
    db: str | None = None,
    force: bool = False,
    skip_checks: bool = False,
    palette: Palette = PLAIN,
) -> str:
    document = read_cim(path)
    lines = [document.report.summary()]

    report = validate(document.network)
    if not report:
        lines.append(f"validation: {palette.paint('no problems found', 'ok')}")
    else:
        # 'gridql validate' cannot read a CIM file, and --db may still hold
        # the old data if the save is refused. Show the findings here instead.
        lines.append(f"validation: {report.counts()}")
        lines.extend(f"  {finding.format(palette)}" for finding in report.errors + report.warnings)

    if db is not None:
        _save_import(document.network, report, db, force, skip_checks, lines)
        lines.extend(_where_to_query(db))
    else:
        lines.append("not saved: pass --db PATH to keep it and query it")

    return "\n".join(lines)


#: A refresh that would drop more than this share of the equipment already
#: stored is refused unless --skip-checks says the drop is intended.
SHRINK_LIMIT = 0.10


class SaveRefused(GridQLError):
    """An import was read but not saved. ``output`` is its report, still worth showing."""

    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.output = output


def _save_import(
    network: Network,
    report: ValidationReport,
    db: str,
    force: bool,
    skip_checks: bool,
    lines: list[str],
) -> None:
    """Save an imported network, guarding a database it would replace.

    A new database has nothing to lose. Replacing one is a refresh, and a
    refresh from a bad export -- truncated, filtered to one circuit, broken
    -- would otherwise swap good data for less of it, with a warning at most.
    """
    if not Path(db).exists():
        save_network(network, db)
        lines.append(f"saved to {db}")
        return
    if not force:
        raise GridQLError(f"{db} already exists; pass --force to overwrite it")

    if skip_checks:
        save_network(network, db)
        lines.append(f"saved to {db}")
        return

    reasons, before = refresh_risks(network, report, db)
    if reasons:
        raise SaveRefused(
            "\n".join(
                [f"not saved: {db} was left as it was, because"]
                + [f"  - {reason}" for reason in reasons]
                + ["Check the new data, or pass --skip-checks to replace it anyway."]
            ),
            "\n".join(lines),
        )
    save_network(network, db)
    lines.append(f"saved to {db} (was {before} devices, now {len(network.devices)})")


def _where_to_query(db: str) -> list[str]:
    """How to query a database just saved, and whether a plain 'gridql' will.

    A plain 'gridql' reads the dataset the project config names, or the
    sample network when none does -- not the database saved last -- so a
    save anywhere else would otherwise look as if it had done nothing.
    """
    command = f"query it with 'gridql --db {shlex.quote(db)}'"
    try:
        config = project_config()
    except GridQLError:
        return [command]

    saved = Path(db).resolve()
    if config.db and Path(config.db).resolve() == saved and not config.csv:
        return [f"{CONFIG_NAME} names this database, so a plain 'gridql' here reads it"]

    if config.root is None:
        return [
            command,
            f"note: a plain 'gridql' reads the bundled sample network, because no "
            f"{CONFIG_NAME} names a dataset; create one here with db = \"{db}\" to change that",
        ]

    try:
        setting = saved.relative_to(config.root.resolve()).as_posix()
    except ValueError:
        setting = str(saved)
    change = f'replace csv = ... with db = "{setting}"' if config.csv else f'set db = "{setting}"'
    reason = "names no dataset" if not (config.db or config.csv) else "names that one"
    return [
        command,
        f"note: a plain 'gridql' here still reads {config.dataset()}, because "
        f"{config.path.name} {reason}; {change} in it to change that",
    ]


def refresh_risks(
    network: Network, report: ValidationReport, db: str
) -> tuple[list[str], int | None]:
    """Why replacing ``db`` with ``network`` looks like a mistake, if it does.

    Returns the reasons, and how many devices the database held.
    """
    try:
        before = object_counts(db)["devices"]
        stored_feeders = feeder_ids(db)
    except StorageError as error:
        return [f"it could not be read to compare against ({error})"], None

    reasons: list[str] = []
    if report.errors:
        shown = "; ".join(f"{f.code}: {f.message}" for f in report.errors[:3])
        more = f"; and {len(report.errors) - 3} more" if len(report.errors) > 3 else ""
        reasons.append(
            f"validation found {len(report.errors)} "
            f"error{'s' if len(report.errors) != 1 else ''}: {shown}{more}"
        )

    now = len(network.devices)
    if before and now < before * (1 - SHRINK_LIMIT):
        reasons.append(
            f"it would replace {before} devices with {now}, "
            f"{round(100 * (before - now) / before)}% fewer"
        )

    missing = sorted(set(stored_feeders) - {feeder.mrid for feeder in network.feeders})
    if missing:
        shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        reasons.append(
            f"{len(missing)} feeder{'s' if len(missing) != 1 else ''} in the database "
            f"{'are' if len(missing) != 1 else 'is'} not in the new data: {shown}"
        )
    return reasons, before


def repl(
    network: Network,
    output_format: str | None,
    source: str = "sample network FDR-104",
    config: Config | None = None,
) -> int:
    config = config or Config()
    palette = _stdout()
    print(_banner(source))
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
        if lowered == ".license":
            print(_LICENSE_NOTICE)
            continue
        if lowered == ".config":
            print(config.describe())
            continue
        if lowered == ".validate":
            print(validate(network).summary(palette))
            continue
        if lowered.startswith(".run"):
            parts = line.split()
            if len(parts) < 2:
                print("usage: .run <file.gridql> [name=value ...]")
                continue
            file, assignments = parts[1], parts[2:]
            if any("=" not in entry for entry in assignments):
                print("usage: .run <file.gridql> [name=value ...]")
                continue
            _emit(
                lambda: run_script(
                    network,
                    resolve_script(config, file),
                    output_format,
                    params=parameters(assignments, {}),
                    defaults=config.params,
                    palette=palette,
                )
            )
            continue

        _emit(lambda: run_query(network, line, output_format, palette=palette))


def _emit(produce) -> int:
    """Print what ``produce`` returns, or report the GridQL error it raised."""
    try:
        print(produce())
    except GridQLSyntaxError as error:
        print(_paint_syntax_error(error), file=sys.stderr)
        return 1
    except GridQLError as error:
        # A refused save still read its input; what it found is the evidence.
        output = getattr(error, "output", None)
        if output:
            print(output)
        _say("error", error)
        return 1
    return 0


def _paint_syntax_error(error: GridQLSyntaxError) -> str:
    """The message and the caret under the offending text stand out; the query does not."""
    palette = Palette.for_stream(sys.stderr, _color)
    lines = error.render().split("\n")
    lines[0] = palette.paint(lines[0], "error")
    if len(lines) > 1:
        caret = lines[-1].rstrip()
        lines[-1] = caret[:-1] + palette.paint(caret[-1:], "error")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    _use_color("auto")

    if argv and argv[0] == "init":
        args = build_init_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(lambda: init_database(args.path, args.empty, args.force))

    if argv and argv[0] == "config":
        args = build_config_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(lambda: show_config(args.config, args.no_config))

    if argv and argv[0] == "validate":
        args = build_validate_parser().parse_args(argv[1:])
        _use_color(args.color)
        try:
            config = project_config(args.config, not args.no_config)
        except GridQLError as error:
            _say("error", error)
            return 1
        return run_validate(args.db, args.strict, args.csv, config, args.mapping)

    if argv and argv[0] == "import-csv":
        args = build_import_csv_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(
            lambda: import_csv(
                args.path, args.db, args.force, args.mapping, _config(args), args.skip_checks
            )
        )

    if argv and argv[0] == "export-csv":
        args = build_export_csv_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(
            lambda: export_csv(args.path, args.db, args.csv, _config(args), args.mapping)
        )

    if argv and argv[0] == "export-cim":
        args = build_export_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(
            lambda: export_cim(
                args.path, args.query, args.db, args.csv, _config(args), args.mapping
            )
        )

    if argv and argv[0] == "import-cim":
        args = build_import_parser().parse_args(argv[1:])
        _use_color(args.color)
        return _emit(
            lambda: import_cim(args.path, args.db, args.force, args.skip_checks, _stdout())
        )

    if argv and argv[0] == "run":
        return _emit(lambda: _run(argv[1:]))

    args = build_parser().parse_args(argv)
    _use_color(args.color)

    try:
        config = _config(args)
        network = network_for(args.db, args.csv, config, args.mapping)
    except GridQLError as error:
        _say("error", error)
        return 1

    if args.query is None:
        return repl(network, args.format, source_of(args.db, args.csv, config), config)

    return _emit(lambda: run_query(network, args.query, args.format, args.explain, _stdout()))


def _config(args: argparse.Namespace) -> Config:
    return project_config(args.config, not args.no_config)


def _run(argv: list[str]) -> str:
    """'gridql run': the file's parameters, then the file and the dataset."""
    rest, options = split_parameters(argv)
    args = build_run_parser().parse_args(rest)
    _use_color(args.color)
    params = parameters(args.params, options)
    config = _config(args)
    return run_script(
        network_for(args.db, args.csv, config, args.mapping),
        resolve_script(config, args.file),
        args.format,
        args.explain,
        params,
        config.params,
        _stdout(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
