"""A dropped database connection is survived, not answered with a 500.  # README, "Deploying"

The shape of the bug this guards against: a managed Postgres, or the network in front of
it, closes a connection that has sat idle; the pool does not notice; the next request - the
first sign-in of the morning, the first deal opened after a quiet hour - is handed the dead
connection and fails with ``psycopg.OperationalError: server closed the connection
unexpectedly``, which the app answers as a 500. Only then does the pool discard it, so the
very next request works. ``db.session.make_engine`` pings every pooled connection before
handing it out, which turns that first request into a reconnect nobody sees.

The dead connection is produced here on purpose: the backend behind the pooled connection
is terminated from a second connection, exactly as a server would drop it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from api.main import app
from db.models import User
from db.session import get_session, make_engine
from tests.conftest import USER_PASSWORD, requires_db, sign_in

pytestmark = requires_db


def serve_with(engine: Engine) -> None:
    """Point the app's sessions at ``engine``: one Session per request, as production does."""

    def override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override


def kill_pooled_backend(engine: Engine, control: Engine) -> None:
    """Drop the server side of the one connection sitting in ``engine``'s pool."""
    with engine.connect() as pooled:
        pid = pooled.execute(text("select pg_backend_pid()")).scalar()
    with control.connect() as other:
        other.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})


@pytest.fixture
def signed_in_client(queue_user: User) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


def test_a_connection_the_server_dropped_is_replaced_before_the_next_request(
    signed_in_client: TestClient, queue_user: User, test_engine: Engine, test_database_url: str
) -> None:
    engine = make_engine(test_database_url)
    try:
        serve_with(engine)
        sign_in(signed_in_client, queue_user.email, USER_PASSWORD)
        assert signed_in_client.get("/queue").status_code == 200

        kill_pooled_backend(engine, test_engine)

        assert signed_in_client.get("/queue").status_code == 200, "the dead connection was served"
    finally:
        engine.dispose()


def test_without_the_ping_the_first_request_after_the_drop_is_a_500(
    signed_in_client: TestClient, queue_user: User, test_engine: Engine, test_database_url: str
) -> None:
    """The failure the ping exists for, reproduced on an engine without it: one 500, then fine."""
    engine = create_engine(test_database_url)
    try:
        serve_with(engine)
        sign_in(signed_in_client, queue_user.email, USER_PASSWORD)
        assert signed_in_client.get("/queue").status_code == 200

        kill_pooled_backend(engine, test_engine)

        assert signed_in_client.get("/queue").status_code == 500
        assert signed_in_client.get("/queue").status_code == 200, "the refresh clears it"
    finally:
        engine.dispose()
