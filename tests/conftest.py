"""Shared fixtures.

Database-backed tests run against ``TEST_DATABASE_URL`` (a throwaway Postgres) when it
is set; otherwise against an embedded PostgreSQL started by the ``pgserver`` dev
dependency in a per-session temp directory. They are skipped only when neither is
available. Either way the tables are dropped and recreated around every test.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from db.models import Base

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
