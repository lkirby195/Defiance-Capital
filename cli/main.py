"""``glenwood`` command line: run a fixture deal, or export one to a workbook.

    uv run glenwood run fixtures/synthetic/deals/go_split_draw_denver.json --underwrite
    uv run glenwood export fixtures/synthetic/deals/go_split_draw_denver.json out.xlsx

Both read a fixture off disk, load ``config/glenwood.yaml``, and call the pure engine. No
database, no network. The point is to check the math by hand before it is wired to
anything, so nothing is rounded for presentation beyond the place it is printed at.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from cli.fixtures import FixtureError, FixtureRun, run_fixture
from config.config import Config, ConfigError, get_config
from outputs.console import render
from outputs.excel import write_workbook

PROGRAM = "glenwood"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Run the GLENWOOD engine on a fixture deal, with no database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="screen (and underwrite) a fixture, print it")
    run_cmd.add_argument("fixture", type=Path, help="path to a fixture JSON document")
    run_cmd.add_argument(
        "--underwrite",
        action="store_true",
        help="also run the underwrite from the fixture's 'underwrite' block",
    )

    export_cmd = commands.add_parser("export", help="write the run to an .xlsx workbook")
    export_cmd.add_argument("fixture", type=Path, help="path to a fixture JSON document")
    export_cmd.add_argument("out", type=Path, help="workbook to write (.xlsx)")
    return parser


def load_config() -> Config:
    """The validated tunables; ``ConfigError`` is reported, not traced."""
    return get_config()


def command_run(fixture: Path, with_underwrite: bool, config: Config) -> str:
    run = run_fixture(fixture, config, with_underwrite=with_underwrite)
    return render(run.name, run.description, run.screen_result, run.underwrite_result, config)


def command_export(fixture: Path, out: Path, config: Config) -> str:
    """Export always underwrites: the lender, grid and exit sheets are the reason it exists."""
    run: FixtureRun = run_fixture(fixture, config, with_underwrite=True)
    if run.underwrite_inputs is None or run.underwrite_result is None:  # pragma: no cover
        raise FixtureError(f"{fixture.name} produced no underwrite")
    write_workbook(out, run.screen_inputs, run.underwrite_inputs, run.underwrite_result, config)
    return f"wrote {out}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status rather than raising at the user."""
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
        if args.command == "run":
            print(command_run(args.fixture, args.underwrite, config))
        else:
            print(command_export(args.fixture, args.out, config))
    except (FixtureError, ConfigError) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
