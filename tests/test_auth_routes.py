"""Signing in, signing out, and what an unsigned-in caller gets.  # SPEC §11

Nothing this app serves is public, so "which routes are protected" is answered here by
enumerating them rather than by spot-checking: a route added without a user dependency shows
up as a page a stranger can read.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.main import app
from api.security import COOKIE_NAME, SESSION_HOURS, issue, read
from db.models import AuditLog, User
from schema.models import AuditAction
from services import deactivate_user
from tests.conftest import USER_EMAIL, USER_PASSWORD, requires_db

pytestmark = requires_db

# Every route the app serves, less the ones FastAPI adds for its own docs. A route that is
# not in one of these lists is a route nobody decided the access rule for.
PAGE_ROUTES = [
    ("GET", "/queue"),
    ("GET", "/queue/new"),
    ("GET", "/queue/deals/00000000-0000-0000-0000-000000000000"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/screen"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/underwrite"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/overrides"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/advance"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/decline"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/dead"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/reopen"),
    ("POST", "/queue/deals/00000000-0000-0000-0000-000000000000/note"),
]
JSON_ROUTES = [
    ("GET", "/deals/00000000-0000-0000-0000-000000000000"),
    ("POST", "/deals/00000000-0000-0000-0000-000000000000/screen"),
    ("POST", "/deals/00000000-0000-0000-0000-000000000000/underwrite"),
]


def audit(session: Session, action: AuditAction) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.action == action.value)))


# --- what the app exposes -------------------------------------------------------------------------


def test_every_route_is_accounted_for() -> None:
    """A new route has to be given an access rule here, or this fails."""
    served: set[tuple[str, str]] = set()

    def walk(routes: Iterable[Any]) -> None:
        # An included router appears as one route wrapping the router it was built from,
        # so this descends through both shapes rather than only the flat one.
        for route in routes:
            inner = getattr(route, "original_router", None)
            nested = getattr(inner, "routes", None) or getattr(route, "routes", None)
            if nested:
                walk(nested)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or methods is None:
                continue
            served.update((method, path) for method in methods if method not in {"HEAD", "OPTIONS"})

    walk(app.routes)
    covered = (
        {
            ("GET", "/"),
            ("GET", "/login"),
            ("POST", "/login"),
            ("POST", "/logout"),
            ("POST", "/intake/team"),
            ("GET", "/queue"),
            ("GET", "/queue/new"),
            ("GET", "/queue/deals/{deal_id}"),
            ("GET", "/deals/{deal_id}"),
            ("GET", "/openapi.json"),
            ("GET", "/docs"),
            ("GET", "/docs/oauth2-redirect"),
            ("GET", "/redoc"),
        }
        | {
            ("POST", f"/queue/deals/{{deal_id}}/{action}")
            for action in (
                "screen",
                "underwrite",
                "overrides",
                "advance",
                "decline",
                "dead",
                "reopen",
                "note",
            )
        }
        | {("POST", f"/deals/{{deal_id}}/{action}") for action in ("screen", "underwrite")}
    )
    assert served == covered


# --- turned away ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", PAGE_ROUTES, ids=[f"{m} {p}" for m, p in PAGE_ROUTES])
def test_a_page_route_sends_a_stranger_to_sign_in(
    anon_client: TestClient, method: str, path: str
) -> None:
    response = anon_client.request(method, path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("method,path", JSON_ROUTES, ids=[f"{m} {p}" for m, p in JSON_ROUTES])
def test_a_json_route_answers_401_rather_than_redirecting(
    anon_client: TestClient, method: str, path: str
) -> None:
    """A redirect is not something an API client can act on."""
    response = anon_client.request(method, path, json={})
    assert response.status_code == 401
    assert "/login" in response.json()["detail"]


def test_the_intake_route_turns_each_caller_away_in_its_own_language(
    anon_client: TestClient,
) -> None:
    """One route, two callers: the browser is redirected and the client gets a 401."""
    posted = anon_client.post("/intake/team", data={"borrower_name": "Sam"}, follow_redirects=False)
    assert posted.status_code == 303
    assert posted.headers["location"] == "/login"
    assert anon_client.post("/intake/team", json={}).status_code == 401


def test_the_root_and_a_protected_page_remember_where_the_caller_was_going(
    anon_client: TestClient,
) -> None:
    assert anon_client.get("/queue/new", follow_redirects=False).headers["location"] == (
        "/login?next=%2Fqueue%2Fnew"
    )
    # "/" is the queue by another name, so there is nothing worth remembering
    root = anon_client.get("/", follow_redirects=False)
    assert root.status_code == 303 and root.headers["location"] == "/queue"


def test_an_open_redirect_is_refused(anon_client: TestClient, queue_user: User) -> None:
    """``next`` only ever sends the caller to a path on this site."""
    response = anon_client.post(
        "/login",
        data={
            "email": queue_user.email,
            "password": USER_PASSWORD,
            "next": "//evil.example/steal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/queue"


# --- signing in -----------------------------------------------------------------------------------


def test_signing_in_sets_a_session_cookie_and_records_the_access(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    response = anon_client.post(
        "/login",
        data={"email": "SAM@Glenwood.Example", "password": USER_PASSWORD, "next": "/queue/new"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/queue/new"

    cookie = anon_client.cookies.get(COOKIE_NAME)
    assert cookie is not None
    claim = read(cookie)
    assert claim is not None and claim.user_id == queue_user.id

    rows = audit(db_session, AuditAction.SIGNED_IN)
    assert len(rows) == 1
    assert rows[0].actor == queue_user.email
    assert USER_PASSWORD not in str(rows[0].after)

    assert anon_client.get("/queue").status_code == 200


@pytest.mark.parametrize(
    "email,password",
    [
        (USER_EMAIL, "the-wrong-password-entirely"),
        ("nobody@glenwood.example", USER_PASSWORD),
    ],
)
def test_a_bad_sign_in_says_the_same_thing_either_way(
    anon_client: TestClient, queue_user: User, email: str, password: str
) -> None:
    del queue_user
    response = anon_client.post("/login", data={"email": email, "password": password})
    assert response.status_code == 401
    assert "Email or password is wrong" in response.text
    assert COOKIE_NAME not in anon_client.cookies


def test_the_sign_in_page_sends_a_signed_in_caller_on(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/queue"


# --- the session ----------------------------------------------------------------------------------


def test_a_forged_or_expired_cookie_is_not_a_session(
    anon_client: TestClient, queue_user: User
) -> None:
    from datetime import UTC, datetime, timedelta

    good = issue(queue_user)
    assert read(good) is not None

    body, _, signature = good.rpartition(".")
    assert read(f"{body}.{signature[:-1]}x") is None, "a tampered signature"
    assert read(f"{body}.") is None, "no signature at all"
    assert read("not-a-cookie") is None
    assert read(None) is None

    stale = issue(queue_user, now=datetime.now(UTC) - timedelta(hours=SESSION_HOURS + 1))
    assert read(stale) is None
    anon_client.cookies.set("glenwood_session", stale)
    assert anon_client.get("/queue", follow_redirects=False).status_code == 303


def test_deactivating_a_user_ends_their_live_session_at_once(
    client: TestClient, db_session: Session, queue_user: User
) -> None:
    """The whole reason sessions are not kept server-side: ``active`` is read every request."""
    assert client.get("/queue").status_code == 200
    deactivate_user(db_session, email=queue_user.email, actor="cli")
    db_session.commit()
    assert client.get("/queue", follow_redirects=False).status_code == 303


# --- signing out ----------------------------------------------------------------------------------


def test_signing_out_clears_the_cookie_and_records_it(
    client: TestClient, db_session: Session, queue_user: User
) -> None:
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert not client.cookies.get(COOKIE_NAME)
    rows = audit(db_session, AuditAction.SIGNED_OUT)
    assert len(rows) == 1 and rows[0].actor == queue_user.email
    assert client.get("/queue", follow_redirects=False).status_code == 303


def test_signing_out_of_nothing_is_not_an_error(
    anon_client: TestClient, db_session: Session
) -> None:
    response = anon_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert audit(db_session, AuditAction.SIGNED_OUT) == []
