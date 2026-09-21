"""``glenwood`` command line: run a fixture deal, export one, or manage queue users.

    uv run glenwood run fixtures/synthetic/deals/go_split_draw_denver.json --underwrite
    uv run glenwood export fixtures/synthetic/deals/go_split_draw_denver.json out.xlsx
    uv run glenwood users create --name "Sam Reed" --email sam@glenwood.example
    uv run glenwood users deactivate --email sam@glenwood.example
    uv run glenwood users list

``run`` and ``export`` read a fixture off disk, load ``config/glenwood.yaml``, and call the
pure engine. No database, no network. The point is to check the math by hand before it is
wired to anything, so nothing is rounded for presentation beyond the place it is printed at.

``users`` is the other kind of command and the only one that opens the database. It is here
rather than in the queue because there is no self-signup and no password reset (SPEC §11):
the set of people who can read credit and court findings is a list someone maintains
deliberately, from a shell, on the machine that holds the data. Deactivating is not
deleting - ``audit_log.actor`` names people who have left, and the row has to keep resolving
to someone.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session

from cli.fixtures import FixtureError, FixtureRun, run_fixture
from config.config import Config, ConfigError, get_config
from db.session import get_engine
from outputs.console import render
from outputs.excel import write_workbook
from services import ServiceError, create_user, deactivate_user, list_users
from services.passwords import MIN_LENGTH, WeakPassword

PROGRAM = "glenwood"
# Who a command-line write is recorded as when nobody says otherwise. Not a person: these
# commands run on the server, and pretending otherwise would put a name on the row that
# nobody chose.
DEFAULT_ACTOR = "cli"


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

    users_cmd = commands.add_parser("users", help="create, deactivate and list queue users")
    users = users_cmd.add_subparsers(dest="users_command", required=True)

    create_user_cmd = users.add_parser("create", help="add a user who can sign in")
    create_user_cmd.add_argument("--name", required=True, help="the person's name")
    create_user_cmd.add_argument("--email", required=True, help="sign-in name; lower-cased")
    create_user_cmd.add_argument(
        "--password",
        help=(
            f"at least {MIN_LENGTH} characters; omit it to be prompted, which keeps it out "
            "of the shell history"
        ),
    )
    create_user_cmd.add_argument(
        "--actor", default=DEFAULT_ACTOR, help="who to record on the audit row"
    )

    deactivate_cmd = users.add_parser("deactivate", help="turn a user's sign-in off")
    deactivate_cmd.add_argument("--email", required=True, help="the user to deactivate")
    deactivate_cmd.add_argument(
        "--actor", default=DEFAULT_ACTOR, help="who to record on the audit row"
    )

    users.add_parser("list", help="every user, active first")
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


def ask_for_password() -> str:
    """Prompt twice and compare, so a typo is caught here and not at the first sign-in."""
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Password again: "):
        raise ValueError("the two passwords do not match")
    return first


def command_users(args: argparse.Namespace) -> str:
    """The user commands. The only place the CLI opens the database."""
    with Session(get_engine()) as session:
        if args.users_command == "create":
            password = args.password if args.password is not None else ask_for_password()
            user = create_user(
                session,
                name=args.name,
                email=args.email,
                password=password,
                actor=args.actor,
            )
            session.commit()
            return f"created {user.email} ({user.name})"
        if args.users_command == "deactivate":
            user = deactivate_user(session, email=args.email, actor=args.actor)
            session.commit()
            return f"deactivated {user.email} ({user.name})"
        users = list_users(session)
        if not users:
            return "no users yet; create one with `glenwood users create`"
        return "\n".join(
            f"{'active  ' if user.active else 'inactive'}  {user.email}  {user.name}"
            for user in users
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status rather than raising at the user."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "users":
            print(command_users(args))
            return 0
        config = load_config()
        if args.command == "run":
            print(command_run(args.fixture, args.underwrite, config))
        else:
            print(command_export(args.fixture, args.out, config))
    except (FixtureError, ConfigError, ServiceError, WeakPassword, ValueError) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
