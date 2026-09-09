"""Shared fixtures. Database-backed tests need TEST_DATABASE_URL (a throwaway Postgres);
they are skipped otherwise so `uv run pytest` works without a database."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from db.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to a throwaway Postgres to run"
)


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
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
