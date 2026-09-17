"""Shared fixtures.

Database-backed tests run against ``TEST_DATABASE_URL`` (a throwaway Postgres) when it
is set; otherwise against an embedded PostgreSQL started by the ``pgserver`` dev
dependency in a per-session temp directory. They are skipped only when neither is
available. Either way the tables are dropped and recreated around every test.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from db.models import Base, Deal
from db.repository import create_deal_from_intake
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import Channel

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/synthetic"
TEAM_ENTRY = FIXTURES / "team_entry_complete.json"
# The Go fixture's own team-entry payload, so the API tests and the CLI run the same deal.
TEAM_ENTRY_WITH_OVERRIDES = FIXTURES / "deals/go_team_overrides_tulsa.json"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
PGSERVER_AVAILABLE = importlib.util.find_spec("pgserver") is not None
TEST_DB_NAME = "glenwood_uw_test"

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL and not PGSERVER_AVAILABLE,
    reason="set TEST_DATABASE_URL to a throwaway Postgres or install the pgserver dev dependency",
)


@pytest.fixture(scope="session")
def test_database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """SQLAlchemy URL of a throwaway database: TEST_DATABASE_URL, else embedded pgserver."""
    if TEST_DATABASE_URL:
        yield TEST_DATABASE_URL
        return
    if not PGSERVER_AVAILABLE:
        pytest.skip("no TEST_DATABASE_URL and pgserver is not installed")
    import pgserver

    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"))
    try:
        server.psql(f"CREATE DATABASE {TEST_DB_NAME}")
        uri = server.get_uri(database=TEST_DB_NAME)
        yield uri.replace("postgresql://", "postgresql+psycopg://", 1)
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def test_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    """Fresh schema from the models for each test; dropped afterwards."""
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        yield session
    Base.metadata.drop_all(test_engine)


@pytest.fixture
def team_entry() -> dict[str, Any]:
    """The complete team-entry payload; a test may edit its copy before storing it."""
    payload: dict[str, Any] = json.loads(TEAM_ENTRY.read_text(encoding="utf-8"))
    return payload


def store_deal(session: Session, payload: dict[str, Any]) -> Deal:
    """Normalize a team-entry payload and store it, as ``POST /intake/team`` does."""
    record = normalize(parse_team_form(TeamEntryForm(**payload)), Channel.TEAM, raw_payload=payload)
    deal = create_deal_from_intake(session, record)
    session.commit()
    return deal


@pytest.fixture
def stored_deal(db_session: Session, team_entry: dict[str, Any]) -> Deal:
    """One complete deal in the database, ready to screen.

    No team overrides: with no valuation behind it, this deal screens to a Decline (its
    LTV is computed on the purchase price and lands over the cap), which is the honest
    state of a deal today and the reason the overrides exist.
    """
    return store_deal(db_session, team_entry)


@pytest.fixture
def team_entry_with_overrides() -> dict[str, Any]:
    """The team-entry payload out of the Go fixture: a valuation and a court search by hand."""
    fixture = json.loads(TEAM_ENTRY_WITH_OVERRIDES.read_text(encoding="utf-8"))
    payload: dict[str, Any] = fixture["team_entry"]
    return payload


@pytest.fixture
def deal_with_overrides(db_session: Session, team_entry_with_overrides: dict[str, Any]) -> Deal:
    """A deal the team has valued and searched by hand; it screens Go."""
    return store_deal(db_session, team_entry_with_overrides)
