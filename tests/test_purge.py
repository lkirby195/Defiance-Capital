"""``glenwood deals purge --all --confirm`` on a seeded database.  # README

Every deal table is emptied, the users are not, and one audit row says what went. The
command refuses to run without both flags, and refuses before it opens the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import db.session as db_session_module
from cli.main import main
from config.config import Config
from db.models import (
    AuditLog,
    Borrower,
    Deal,
    Entity,
    IntakeSubmission,
    Property,
    Screen,
    Underwrite,
    User,
    borrower_entities,
)
from schema.models import AuditAction
from services import UnderwriteRequest, add_note, create_user, purge_deals, run_underwrite
from tests.conftest import ACTOR, requires_db, store_deal

pytestmark = requires_db

CONFIG = Config.load()


@pytest.fixture
def cli_database(
    monkeypatch: pytest.MonkeyPatch, test_engine: Engine, test_database_url: str
) -> Iterator[None]:
    """Point the CLI at the test database for the duration of one test."""
    del test_engine
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    db_session_module.get_engine.cache_clear()
    yield
    db_session_module.get_engine.cache_clear()


def count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def seed(session: Session, team_entry: dict[str, Any], with_overrides: dict[str, Any]) -> None:
    """Two deals with everything hanging off them, and a user who stays."""
    store_deal(session, team_entry)
    priced = store_deal(session, with_overrides)
    run_underwrite(session, priced.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    add_note(session, priced.id, actor=ACTOR, note="seeded")
    create_user(
        session, name="Sam Reed", email="sam@glenwood.example", password="x" * 16, actor="t"
    )
    session.commit()
    assert count(session, Deal) == 2
    assert count(session, Borrower) == 2 and count(session, Property) == 2
    assert count(session, Entity) >= 1 and count(session, borrower_entities) >= 1
    assert count(session, IntakeSubmission) == 2
    assert count(session, Screen) == 1 and count(session, Underwrite) == 1
    assert count(session, AuditLog) >= 4
    assert count(session, User) == 1


def test_the_service_empties_every_deal_table_and_leaves_the_users(
    db_session: Session, team_entry: dict[str, Any], team_entry_with_overrides: dict[str, Any]
) -> None:
    seed(db_session, team_entry, team_entry_with_overrides)
    deleted = purge_deals(db_session, actor="logan")
    db_session.commit()

    for table in (
        Deal,
        Borrower,
        Entity,
        Property,
        IntakeSubmission,
        Screen,
        Underwrite,
        borrower_entities,
    ):
        assert count(db_session, table) == 0, table
    assert count(db_session, User) == 1
    assert deleted["deals"] == 2 and deleted["underwrites"] == 1
    assert "users" not in deleted
    # the one row left: the purge itself, by whom, and what went
    [row] = list(db_session.scalars(select(AuditLog)))
    assert row.action == AuditAction.DEALS_PURGED.value
    assert row.actor == "logan" and row.row_id == "*"
    assert row.after is not None and row.after["deleted"]["deals"] == 2
    assert row.after["deleted"]["audit_log"] >= 4


def test_the_command_purges_a_seeded_database(
    cli_database: None,
    db_session: Session,
    team_entry: dict[str, Any],
    team_entry_with_overrides: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(db_session, team_entry, team_entry_with_overrides)
    assert main(["deals", "purge", "--all", "--confirm", "--actor", "logan"]) == 0
    out = capsys.readouterr().out
    assert "purged every deal" in out and "deals: 2" in out and "users kept" in out

    db_session.expire_all()
    assert count(db_session, Deal) == 0 and count(db_session, Screen) == 0
    assert count(db_session, User) == 1
    [row] = list(db_session.scalars(select(AuditLog)))
    assert row.action == AuditAction.DEALS_PURGED.value and row.actor == "logan"


@pytest.mark.parametrize("argv", [["--all"], ["--confirm"], []])
def test_the_command_refuses_without_both_flags_and_touches_nothing(
    cli_database: None,
    db_session: Session,
    team_entry: dict[str, Any],
    team_entry_with_overrides: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    seed(db_session, team_entry, team_entry_with_overrides)
    before = count(db_session, AuditLog)
    assert main(["deals", "purge", *argv]) == 2
    assert "pass both --all and --confirm" in capsys.readouterr().err
    db_session.expire_all()
    assert count(db_session, Deal) == 2 and count(db_session, AuditLog) == before
