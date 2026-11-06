"""Failed sign-ins in the audit log, and the lockout they add up to.  # SPEC §11 (v0.26)

Run against the real config - ten failures, fifteen minutes - and the real PBKDF2, so the
numbers under test are the ones a deployment runs on. The window is the database's clock:
expiry is tested by back-dating the rows, not by waiting or by moving the app's clock.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

import api.routes.auth
import services.users
from api.security import COOKIE_NAME
from config.config import get_config
from db.models import AuditLog, User
from schema.models import AuditAction
from services import (
    ANONYMOUS,
    UNKNOWN_ACCOUNT,
    authenticate,
    create_user,
    lockout,
    record_audit,
    record_failed_sign_in,
)
from services.passwords import ITERATIONS, decoy_hash, verify_password
from services.users import NO_ACCOUNT_HASH
from tests.conftest import USER_EMAIL, USER_PASSWORD, requires_db

pytestmark = requires_db

LIMIT = get_config().sign_in.lockout_failures
MINUTES = get_config().sign_in.lockout_minutes
WRONG = "not-the-password-at-all"
OFFICE = "198.51.100.7"


def sign_in(client: TestClient, email: str, password: str, address: str = OFFICE) -> Any:
    """A post as it arrives through Render: the proxy appends the real address last, after
    whatever the client put there."""
    return client.post(
        "/login",
        data={"email": email, "password": password},
        headers={"X-Forwarded-For": f"10.0.0.1, {address}"},
        follow_redirects=False,
    )


def failures(session: Session) -> list[AuditLog]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.SIGN_IN_FAILED.value)
            .order_by(AuditLog.created_at)
        )
    )


def age_failures(session: Session, minutes: float) -> None:
    """Move every failure ``minutes`` into the past, on the database's clock."""
    session.execute(
        update(AuditLog)
        .where(AuditLog.action == AuditAction.SIGN_IN_FAILED.value)
        .values(created_at=func.now() - timedelta(minutes=minutes))
    )
    session.commit()


def test_the_config_is_the_brief_s() -> None:
    assert (LIMIT, MINUTES) == (10, 15)


# --- one row per failure ------------------------------------------------------------------------


def test_a_failed_sign_in_is_one_anonymous_row_with_the_email_and_the_address(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    response = sign_in(anon_client, "  SAM@Glenwood.Example ", WRONG)
    assert response.status_code == 401
    assert "Email or password is wrong" in response.text
    response = sign_in(anon_client, "nobody@glenwood.example", WRONG, address="203.0.113.9")
    assert response.status_code == 401
    known, unknown = failures(db_session)
    assert (known.actor, known.table_name, known.row_id) == (ANONYMOUS, "users", str(queue_user.id))
    # the normalized email, and the address the proxy appended - not the client's first hop
    assert known.after == {"email": USER_EMAIL, "ip": OFFICE}
    assert known.before is None
    assert (unknown.actor, unknown.row_id) == (ANONYMOUS, UNKNOWN_ACCOUNT)
    assert unknown.after == {"email": "nobody@glenwood.example", "ip": "203.0.113.9"}
    # a deactivated account is still the account
    services.users.deactivate_user(db_session, email=USER_EMAIL, actor="cli")
    db_session.commit()
    assert sign_in(anon_client, USER_EMAIL, USER_PASSWORD).status_code == 401
    assert failures(db_session)[-1].row_id == str(queue_user.id)


def test_without_a_proxy_the_address_is_the_socket_s(
    anon_client: TestClient, db_session: Session
) -> None:
    anon_client.post("/login", data={"email": "a@b.example", "password": WRONG})
    (row,) = failures(db_session)
    assert row.after == {"email": "a@b.example", "ip": "testclient"}


def test_a_successful_sign_in_writes_no_failure(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    assert sign_in(anon_client, queue_user.email, USER_PASSWORD).status_code == 303
    assert failures(db_session) == []


# --- the lockout ----------------------------------------------------------------------------------


def test_ten_failures_for_one_email_lock_it_from_every_address(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    for attempt in range(LIMIT):
        response = sign_in(anon_client, USER_EMAIL, WRONG, address=f"203.0.113.{attempt + 1}")
        assert response.status_code == 401, attempt
    # the right password, from an address nobody has failed from, is still turned away
    locked = sign_in(anon_client, USER_EMAIL, USER_PASSWORD, address="192.0.2.50")
    assert locked.status_code == 429
    assert "Too many sign-in attempts. Try again in 15 minutes." in locked.text
    assert 0 < int(locked.headers["retry-after"]) <= MINUTES * 60
    assert COOKIE_NAME not in anon_client.cookies
    # ...and the refusal was not counted
    assert len(failures(db_session)) == LIMIT
    # another account from the same address is not locked
    other = create_user(
        db_session,
        name="Ana Ruiz",
        email="ana@glenwood.example",
        password=USER_PASSWORD,
        actor="conftest",
    )
    db_session.commit()
    assert sign_in(anon_client, other.email, USER_PASSWORD, address="192.0.2.50").status_code == 303
    del queue_user


def test_ten_failures_from_one_address_lock_it_for_every_email(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    for attempt in range(LIMIT):
        assert sign_in(anon_client, f"guess{attempt}@glenwood.example", WRONG).status_code == 401
    locked = sign_in(anon_client, queue_user.email, USER_PASSWORD)
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers
    assert len(failures(db_session)) == LIMIT
    # the same account from another address signs in
    assert sign_in(
        anon_client, queue_user.email, USER_PASSWORD, address="192.0.2.50"
    ).status_code == (303)


def test_nine_failures_do_not_lock(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    for _ in range(LIMIT - 1):
        assert sign_in(anon_client, queue_user.email, WRONG).status_code == 401
    assert sign_in(anon_client, queue_user.email, USER_PASSWORD).status_code == 303
    # a success does not clear the count: one more failure locks it (SPEC §13, v0.26)
    anon_client.cookies.clear()
    assert sign_in(anon_client, queue_user.email, WRONG).status_code == 401
    assert sign_in(anon_client, queue_user.email, USER_PASSWORD).status_code == 429
    del db_session


def test_the_wait_is_until_the_tenth_newest_failure_leaves_the_window(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    for _ in range(LIMIT):
        sign_in(anon_client, queue_user.email, WRONG)
    age_failures(db_session, MINUTES - 5)
    locked = sign_in(anon_client, queue_user.email, USER_PASSWORD)
    assert locked.status_code == 429
    assert 4 * 60 < int(locked.headers["retry-after"]) <= 5 * 60
    assert "Try again in 5 minutes." in locked.text


def test_the_lock_lifts_when_the_window_passes_and_the_right_password_works_again(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    for _ in range(LIMIT):
        sign_in(anon_client, queue_user.email, WRONG)
    assert sign_in(anon_client, queue_user.email, USER_PASSWORD).status_code == 429
    age_failures(db_session, MINUTES + 1)
    response = sign_in(anon_client, queue_user.email, USER_PASSWORD)
    assert response.status_code == 303
    assert anon_client.cookies.get(COOKIE_NAME)
    signed_in = db_session.scalars(
        select(AuditLog).where(AuditLog.action == AuditAction.SIGNED_IN.value)
    ).all()
    assert len(signed_in) == 1


def test_a_locked_attempt_verifies_no_password_and_goes_to_the_process_log(
    anon_client: TestClient,
    queue_user: User,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for _ in range(LIMIT):
        sign_in(anon_client, queue_user.email, WRONG)

    def no_pbkdf2(*args: object, **kwargs: object) -> None:
        raise AssertionError("a locked attempt reached the password check")

    monkeypatch.setattr(api.routes.auth, "authenticate", no_pbkdf2)
    monkeypatch.setattr(services.users, "verify_password", no_pbkdf2)
    caplog.set_level("WARNING", logger="glenwood.auth")
    assert sign_in(anon_client, queue_user.email, USER_PASSWORD).status_code == 429
    assert "sign-in refused while locked" in caplog.text
    assert USER_PASSWORD not in caplog.text
    assert len(failures(db_session)) == LIMIT


def test_no_audit_row_ever_carries_a_password(
    anon_client: TestClient, queue_user: User, db_session: Session
) -> None:
    tried = [f"wrong-password-number-{n}" for n in range(LIMIT)]
    for password in tried:
        sign_in(anon_client, queue_user.email, password)
    sign_in(anon_client, queue_user.email, USER_PASSWORD)  # locked
    age_failures(db_session, MINUTES + 1)
    sign_in(anon_client, queue_user.email, USER_PASSWORD)  # in
    rows = list(db_session.scalars(select(AuditLog)))
    assert {row.action for row in rows} >= {
        AuditAction.SIGN_IN_FAILED.value,
        AuditAction.SIGNED_IN.value,
    }
    for row in rows:
        text = json.dumps([row.before, row.after])
        for password in [*tried, USER_PASSWORD]:
            assert password not in text, (row.action, password)
        assert "pbkdf2" not in text


# --- the service ----------------------------------------------------------------------------------


def test_the_service_counts_only_failed_sign_ins_inside_the_window(
    db_session: Session, queue_user: User
) -> None:
    assert lockout(db_session, email=USER_EMAIL, address=OFFICE) is None
    for _ in range(LIMIT - 1):
        record_failed_sign_in(db_session, email=USER_EMAIL, address=OFFICE)
    # other rows for the same email do not count towards it
    record_audit(
        db_session,
        actor=USER_EMAIL,
        action=AuditAction.SIGNED_IN,
        table_name="users",
        row_id=queue_user.id,
        after={"email": USER_EMAIL, "ip": OFFICE},
    )
    db_session.commit()
    assert lockout(db_session, email=USER_EMAIL, address=OFFICE) is None
    record_failed_sign_in(db_session, email="Sam@Glenwood.Example", address=OFFICE)
    db_session.commit()
    lock = lockout(db_session, email=" sam@GLENWOOD.example", address="192.0.2.1")
    assert lock is not None
    assert (lock.by_email, lock.by_address) == (True, False)
    assert lock.minutes == MINUTES and 0 < lock.seconds <= MINUTES * 60
    by_address = lockout(db_session, email="other@glenwood.example", address=OFFICE)
    assert by_address is not None and (by_address.by_email, by_address.by_address) == (False, True)
    age_failures(db_session, MINUTES + 1)
    assert lockout(db_session, email=USER_EMAIL, address=OFFICE) is None


# --- the timing of a miss -------------------------------------------------------------------------


def test_an_unknown_email_costs_the_same_pbkdf2_as_a_wrong_password(
    db_session: Session, queue_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``authenticate`` used to return on an unknown email without hashing anything."""
    checked: list[str] = []

    def spy(password: str, encoded: str) -> bool:
        checked.append(encoded)
        return verify_password(password, encoded)

    monkeypatch.setattr(services.users, "verify_password", spy)
    assert authenticate(db_session, email="nobody@glenwood.example", password=WRONG) is None
    assert authenticate(db_session, email=USER_EMAIL, password=WRONG) is None
    assert checked == [NO_ACCOUNT_HASH, queue_user.password_hash]
    # the decoy is at the current cost, so the two derivations are the same work
    assert NO_ACCOUNT_HASH.split("$")[1] == str(ITERATIONS)
    assert NO_ACCOUNT_HASH.split("$")[1] == queue_user.password_hash.split("$")[1]


def test_no_password_verifies_against_a_decoy() -> None:
    decoy = decoy_hash()
    assert decoy != decoy_hash()
    for password in ("", WRONG, USER_PASSWORD):
        assert not verify_password(password, decoy)
    assert decoy_hash(iterations=1000).split("$")[1] == "1000"
