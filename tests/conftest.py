"""Shared fixtures.

Database-backed tests run against ``TEST_DATABASE_URL`` (a throwaway Postgres) when it
is set; otherwise against an embedded PostgreSQL started by the ``pgserver`` dev
dependency in a per-session temp directory. They are skipped only when neither is
available. Either way the tables are dropped and recreated around every test.

Everything the app serves is behind a session cookie (SPEC §11), so there are two clients:
``anon_client`` has no session and is what the "this route is protected" tests use, and
``client`` has signed ``queue_user`` in through the real ``POST /login``. Signing in for real
rather than forging a cookie means the sign-in path is exercised by every test that needs a
session, and a change that breaks it cannot pass unnoticed.

A signed-in client also carries the CSRF token on a form post, the way a page does: it reads
one off a rendered page and puts it in the body. That keeps every test about the thing it is
named for rather than about tokens - and it is safe to do only because
``tests/test_csrf.py`` posts to every guarded route *without* one and asserts the refusal, so
a route that quietly lost its guard fails there rather than passing everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

# Set before anything imports api.security, which reads it when a cookie is signed.
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-a-real-one")

from api.main import app  # noqa: E402
from api.security import COOKIE_NAME  # noqa: E402
from db.models import Base, Deal, User  # noqa: E402
from db.repository import create_deal_from_intake  # noqa: E402
from db.session import get_session  # noqa: E402
from intake.normalize import normalize  # noqa: E402
from intake.parsers.team_form import TeamEntryForm, parse_team_form  # noqa: E402
from schema.models import Channel  # noqa: E402
from services import create_user  # noqa: E402

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


# --- who is signed in (SPEC §11) -----------------------------------------------------------------

USER_NAME = "Sam Reed"
USER_EMAIL = "sam@glenwood.example"
USER_PASSWORD = "correct-horse-battery-staple"
# The actor every service-level test records its writes as; the API tests get the user's own
# email instead, because that is what the route passes.
ACTOR = "tester@glenwood.example"


@pytest.fixture
def queue_user(db_session: Session) -> User:
    """One active user, created exactly as ``glenwood users create`` creates one."""
    user = create_user(
        db_session,
        name=USER_NAME,
        email=USER_EMAIL,
        password=USER_PASSWORD,
        actor="conftest",
    )
    db_session.commit()
    return user


CSRF_INPUT = re.compile(r'name="_csrf" value="([^"]+)"')


class QueueClient(TestClient):
    """A TestClient that posts forms the way the review queue's own pages do.

    With a session cookie in hand it reads a CSRF token off a rendered page and puts it in
    the body, exactly as a browser submitting that page would. Without one it changes
    nothing, so ``anon_client`` still posts bare and still gets turned away at the door.

    A test can opt out by passing its own ``_csrf`` - including ``_csrf: ""`` for the
    tokenless case.
    """

    _token: str | None = None

    def csrf_token(self) -> str:
        """A token off a real page, cached: any token this app minted stays valid."""
        if self._token is None:
            response = super().get("/queue")
            assert response.status_code == 200, response.status_code
            match = CSRF_INPUT.search(response.text)
            assert match is not None, "no CSRF field on the queue page"
            self._token = match.group(1)
        return self._token

    def forget_csrf(self) -> None:
        """Drop the cached token, for a test that changes who is signed in."""
        self._token = None

    def post(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
        signed_in_here = COOKIE_NAME in self.cookies
        body_is_json = kwargs.get("json") is not None or kwargs.get("content") is not None
        if signed_in_here and not body_is_json:
            data = kwargs.get("data")
            data = dict(data) if isinstance(data, dict) else {}
            data.setdefault("_csrf", self.csrf_token())
            kwargs["data"] = data
        return super().post(url, **kwargs)


@pytest.fixture
def anon_client(db_session: Session) -> Iterator[QueueClient]:
    """A client with no session; every protected route should turn it away."""

    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    with QueueClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def sign_in(test_client: TestClient, email: str, password: str) -> None:
    """Sign a client in through the real form post, so the cookie is a real one."""
    response = test_client.post("/login", data={"email": email, "password": password})
    assert response.status_code == 200, response.text


@pytest.fixture
def client(anon_client: QueueClient, queue_user: User) -> QueueClient:
    """A client signed in as ``queue_user``."""
    sign_in(anon_client, queue_user.email, USER_PASSWORD)
    anon_client.forget_csrf()
    return anon_client
