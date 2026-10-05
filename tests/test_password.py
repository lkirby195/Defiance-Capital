"""Changing a password: your own on the queue, somebody else's from the shell.  # SPEC §11

Both writes are audited without the credential, both hold the same length floor a created
password does, and both end every other session on the account: a session cookie is bound to
the password hash it was issued under (``api/security.py``), so the moment the row changes
every older cookie stops being a session. The one that made the change is re-issued with the
redirect and carries on.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.main import app
from api.security import COOKIE_NAME, fingerprint, issue, read
from cli.main import main
from db.models import AuditLog, User
from schema.models import AuditAction
from services import (
    USERS,
    UserNotFound,
    WrongPassword,
    authenticate,
    change_password,
    reset_password,
)
from services.passwords import WeakPassword
from tests.conftest import USER_EMAIL, USER_PASSWORD, QueueClient, requires_db, sign_in

pytestmark = requires_db

NEW = "a-brand-new-long-password"
SAM = "sam@glenwood.example"
PASSWORD_PATH = "/account/password"


def audit(session: Session, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.table_name == USERS, AuditLog.action == action.value)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def change(client: QueueClient, current: str, new: str, again: str | None = None) -> Any:
    return client.post(
        PASSWORD_PATH,
        data={
            "current_password": current,
            "new_password": new,
            "new_password_again": new if again is None else again,
        },
        follow_redirects=False,
    )


# --- the page -------------------------------------------------------------------------------------


def test_the_top_bar_links_to_change_password_for_the_signed_in_user(
    client: QueueClient,
) -> None:
    body = client.get("/queue").text
    assert f'href="{PASSWORD_PATH}"' in body and ">Change password</a>" in body
    assert body.index(">Change password</a>") < body.index(">Sign out</button>")
    # nobody signed in, no link: the sign-in page has no top-bar account controls
    with QueueClient(app) as stranger:
        assert f'href="{PASSWORD_PATH}"' not in stranger.get("/login").text


def test_the_page_asks_for_the_current_password_and_the_new_one_twice(client: QueueClient) -> None:
    response = client.get(PASSWORD_PATH)
    assert response.status_code == 200
    body = response.text
    for name in ("current_password", "new_password", "new_password_again"):
        assert f'name="{name}" type="password"' in body, name
    assert 'autocomplete="current-password"' in body
    assert body.count('autocomplete="new-password"') == 2
    assert 'minlength="12"' in body
    assert "At least 12 characters" in body


# --- the change -----------------------------------------------------------------------------------


def test_changing_the_password_records_it_and_keeps_this_session_signed_in(
    client: QueueClient, db_session: Session, queue_user: User
) -> None:
    before = client.cookies.get(COOKIE_NAME)
    response = change(client, USER_PASSWORD, NEW)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/queue?notice=Password%20changed")
    # a fresh cookie came back with the redirect, and the page still opens on it
    after = client.cookies.get(COOKIE_NAME)
    assert after is not None and after != before
    assert client.get("/queue").status_code == 200

    db_session.expire_all()
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is not None
    assert authenticate(db_session, email=USER_EMAIL, password=USER_PASSWORD) is None
    (row,) = audit(db_session, AuditAction.USER_PASSWORD_CHANGED)
    assert row.actor == queue_user.email and row.row_id == str(queue_user.id)
    assert row.after == {"email": queue_user.email}
    for secret in (USER_PASSWORD, NEW):
        assert secret not in str(row.after) and secret not in str(row.before)


def test_changing_the_password_signs_out_every_other_session(
    client: QueueClient, queue_user: User
) -> None:
    with QueueClient(app) as elsewhere:
        sign_in(elsewhere, queue_user.email, USER_PASSWORD)
        assert elsewhere.get("/queue").status_code == 200
        assert change(client, USER_PASSWORD, NEW).status_code == 303
        turned_away = elsewhere.get("/queue", follow_redirects=False)
        assert turned_away.status_code == 303
        assert turned_away.headers["location"].startswith("/login")
        # ...and signing in again with the new password works there too (the dead cookie
        # is dropped first, as a browser told to sign in would drop it)
        elsewhere.cookies.clear()
        sign_in(elsewhere, queue_user.email, NEW)
        assert elsewhere.get("/queue").status_code == 200
    assert client.get("/queue").status_code == 200, "the session that made the change stays"


def test_the_wrong_current_password_changes_nothing(
    client: QueueClient, db_session: Session, queue_user: User
) -> None:
    was = queue_user.password_hash
    response = change(client, "not-the-current-password", NEW)
    assert response.status_code == 422
    assert "The current password is wrong." in response.text
    db_session.expire_all()
    assert db_session.get(User, queue_user.id).password_hash == was  # type: ignore[union-attr]
    assert audit(db_session, AuditAction.USER_PASSWORD_CHANGED) == []
    assert client.get("/queue").status_code == 200


def test_two_different_new_passwords_are_refused_before_anything_is_checked(
    client: QueueClient, db_session: Session, queue_user: User
) -> None:
    was = queue_user.password_hash
    response = change(client, USER_PASSWORD, NEW, again=NEW + "x")
    assert response.status_code == 422
    assert "The two new passwords do not match." in response.text
    db_session.expire_all()
    assert db_session.get(User, queue_user.id).password_hash == was  # type: ignore[union-attr]


def test_a_short_new_password_is_refused_on_the_same_floor(
    client: QueueClient, db_session: Session, queue_user: User
) -> None:
    response = change(client, USER_PASSWORD, "short-one")
    assert response.status_code == 422
    assert "Password must be at least 12 characters." in response.text
    db_session.expire_all()
    assert authenticate(db_session, email=USER_EMAIL, password=USER_PASSWORD) is not None


def test_a_stranger_is_sent_to_sign_in(anon_client: QueueClient) -> None:
    assert anon_client.get(PASSWORD_PATH, follow_redirects=False).status_code == 303
    posted = anon_client.post(PASSWORD_PATH, data={"new_password": NEW}, follow_redirects=False)
    assert posted.status_code == 303 and posted.headers["location"].startswith("/login")


# --- the service, and the cookie it invalidates ---------------------------------------------------


def test_the_service_verifies_the_current_password_and_the_floor(
    db_session: Session, queue_user: User
) -> None:
    with pytest.raises(WrongPassword):
        change_password(db_session, queue_user, current="wrong", new=NEW, actor=SAM)
    with pytest.raises(WeakPassword):
        change_password(db_session, queue_user, current=USER_PASSWORD, new="short", actor=SAM)
    change_password(db_session, queue_user, current=USER_PASSWORD, new=NEW, actor=SAM)
    db_session.commit()
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is not None


def test_a_cookie_issued_under_the_old_password_is_not_a_session(
    anon_client: QueueClient, db_session: Session, queue_user: User
) -> None:
    """The binding itself: a valid, unexpired, correctly signed cookie, and still not a session."""
    old = issue(queue_user)
    claim = read(old)
    assert claim is not None and claim.fingerprint == fingerprint(queue_user)
    anon_client.cookies.set(COOKIE_NAME, old)
    assert anon_client.get("/queue").status_code == 200

    reset_password(db_session, email=queue_user.email, password=NEW, actor="cli")
    db_session.commit()
    assert fingerprint(queue_user) != claim.fingerprint
    assert read(old) is not None, "the signature is still good; the row is what moved"
    assert anon_client.get("/queue", follow_redirects=False).status_code == 303


# --- the admin reset ------------------------------------------------------------------------------


def test_reset_password_sets_a_new_one_and_records_who_did_it(
    db_session: Session, queue_user: User
) -> None:
    reset_password(db_session, email=" SAM@Glenwood.Example ", password=NEW, actor="logan")
    db_session.commit()
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is not None
    assert authenticate(db_session, email=USER_EMAIL, password=USER_PASSWORD) is None
    (row,) = audit(db_session, AuditAction.USER_PASSWORD_RESET)
    assert row.actor == "logan" and row.row_id == str(queue_user.id)
    assert row.after == {"email": queue_user.email, "active": True}
    assert NEW not in str(row.after)


def test_reset_password_refuses_an_unknown_user_and_a_short_password(
    db_session: Session, queue_user: User
) -> None:
    with pytest.raises(UserNotFound):
        reset_password(db_session, email="nobody@glenwood.example", password=NEW, actor="cli")
    with pytest.raises(WeakPassword):
        reset_password(db_session, email=queue_user.email, password="short", actor="cli")
    db_session.rollback()
    assert authenticate(db_session, email=USER_EMAIL, password=USER_PASSWORD) is not None


def test_a_reset_leaves_a_deactivated_account_deactivated(
    db_session: Session, queue_user: User
) -> None:
    queue_user.active = False
    db_session.commit()
    reset_password(db_session, email=queue_user.email, password=NEW, actor="cli")
    db_session.commit()
    assert queue_user.active is False
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is None  # still inactive
    (row,) = audit(db_session, AuditAction.USER_PASSWORD_RESET)
    assert row.after == {"email": queue_user.email, "active": False}


# --- glenwood users reset-password ----------------------------------------------------------------


@pytest.fixture
def cli_database(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> Any:
    """Point the CLI's own engine at the test database for the duration of one test."""
    import db.session as db_session_module

    monkeypatch.setenv("DATABASE_URL", test_database_url)
    db_session_module.get_engine.cache_clear()
    yield
    db_session_module.get_engine.cache_clear()


def test_the_cli_resets_a_password_and_signs_the_user_out_everywhere(
    cli_database: None,
    client: QueueClient,
    db_session: Session,
    queue_user: User,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert client.get("/queue").status_code == 200
    code = main(["users", "reset-password", "--email", "Sam@Glenwood.Example", "--password", NEW])
    assert code == 0
    out = capsys.readouterr().out
    assert "reset the password for sam@glenwood.example" in out and "signed out" in out
    assert NEW not in out
    db_session.expire_all()
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is not None
    (row,) = audit(db_session, AuditAction.USER_PASSWORD_RESET)
    assert row.actor == "cli"
    assert client.get("/queue", follow_redirects=False).status_code == 303


def test_the_cli_prompts_when_no_password_is_given(
    cli_database: None,
    db_session: Session,
    queue_user: User,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import cli.main as cli_main

    monkeypatch.setattr(cli_main, "ask_for_password", lambda: NEW)
    assert main(["users", "reset-password", "--email", USER_EMAIL, "--actor", "logan"]) == 0
    assert "reset the password" in capsys.readouterr().out
    db_session.expire_all()
    assert authenticate(db_session, email=USER_EMAIL, password=NEW) is not None


def test_the_cli_reports_a_refusal_rather_than_tracing(
    cli_database: None, queue_user: User, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["users", "reset-password", "--email", "nobody@g.example", "--password", NEW]) == 2
    assert "no user with email" in capsys.readouterr().err
    assert main(["users", "reset-password", "--email", USER_EMAIL, "--password", "x"]) == 2
    assert "at least" in capsys.readouterr().err
