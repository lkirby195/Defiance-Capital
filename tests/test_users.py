"""services/users.py and ``glenwood users``: who exists, and what it records.  # SPEC §5, §11"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cli.main import main
from db.models import AuditLog, User
from schema.models import AuditAction
from services import (
    USERS,
    UserExists,
    UserNotFound,
    authenticate,
    create_user,
    deactivate_user,
    list_users,
    normalize_email,
    user_by_email,
)
from services.passwords import WeakPassword, verify_password
from tests.conftest import requires_db

pytestmark = requires_db

PASSWORD = "correct-horse-battery-staple"
OTHER = "a-different-long-password"


def make(session: Session, email: str = "sam@glenwood.example", **overrides: object) -> User:
    kwargs: dict[str, object] = {
        "name": "Sam Reed",
        "email": email,
        "password": PASSWORD,
        "actor": "conftest",
    }
    kwargs.update(overrides)
    user = create_user(session, **kwargs)  # type: ignore[arg-type]
    session.commit()
    return user


def audit_rows(session: Session, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(AuditLog.table_name == USERS, AuditLog.action == action.value)
        )
    )


# --- creating ------------------------------------------------------------------------------------


def test_a_user_is_created_active_with_a_hashed_password(db_session: Session) -> None:
    user = make(db_session)
    assert user.active is True
    assert user.password_hash != PASSWORD
    assert PASSWORD not in user.password_hash
    assert verify_password(PASSWORD, user.password_hash)


def test_the_email_is_lower_cased_and_stripped(db_session: Session) -> None:
    """One account, however it was typed: the unique index is the rule a person expects."""
    user = make(db_session, email="  Sam@Glenwood.Example  ")
    assert user.email == "sam@glenwood.example"
    assert normalize_email(" SAM@GLENWOOD.EXAMPLE ") == user.email
    found = user_by_email(db_session, "SAM@glenwood.EXAMPLE")
    assert found is not None and found.id == user.id


def test_a_second_user_on_the_same_email_is_refused(db_session: Session) -> None:
    make(db_session)
    with pytest.raises(UserExists):
        create_user(
            db_session,
            name="Someone Else",
            email="SAM@glenwood.example",
            password=OTHER,
            actor="conftest",
        )


def test_a_short_password_leaves_no_row_behind(db_session: Session) -> None:
    with pytest.raises(WeakPassword):
        create_user(
            db_session, name="Sam", email="sam@glenwood.example", password="short", actor="cli"
        )
    db_session.rollback()
    assert db_session.scalars(select(User)).all() == []


def test_creating_a_user_records_an_audit_row_without_the_credential(db_session: Session) -> None:
    user = make(db_session, actor="cli")
    rows = audit_rows(db_session, AuditAction.USER_CREATED)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "cli"
    assert row.row_id == str(user.id)
    assert row.after == {"name": "Sam Reed", "email": user.email, "active": True}
    assert PASSWORD not in str(row.after)


# --- deactivating --------------------------------------------------------------------------------


def test_deactivating_keeps_the_row_and_records_the_change(db_session: Session) -> None:
    """Not a delete: ``audit_log.actor`` has to keep resolving to someone."""
    user = make(db_session)
    deactivate_user(db_session, email="SAM@glenwood.example", actor="cli")
    db_session.commit()
    assert user.active is False
    assert db_session.get(User, user.id) is not None
    rows = audit_rows(db_session, AuditAction.USER_DEACTIVATED)
    assert len(rows) == 1
    assert rows[0].before == {"active": True}
    assert rows[0].after == {"active": False}


def test_deactivating_an_unknown_user_is_refused(db_session: Session) -> None:
    with pytest.raises(UserNotFound):
        deactivate_user(db_session, email="nobody@glenwood.example", actor="cli")


def test_deactivating_twice_is_a_no_op_that_is_still_recorded(db_session: Session) -> None:
    """The person doing it meant to; the trail should show they did."""
    make(db_session)
    deactivate_user(db_session, email="sam@glenwood.example", actor="cli")
    deactivate_user(db_session, email="sam@glenwood.example", actor="cli")
    db_session.commit()
    rows = audit_rows(db_session, AuditAction.USER_DEACTIVATED)
    assert len(rows) == 2
    assert rows[1].before == {"active": False}


# --- authenticating ------------------------------------------------------------------------------


def test_the_right_password_authenticates(db_session: Session) -> None:
    user = make(db_session)
    found = authenticate(db_session, email="Sam@Glenwood.Example", password=PASSWORD)
    assert found is not None and found.id == user.id


def test_every_kind_of_failure_answers_the_same_way(db_session: Session) -> None:
    """None for all of them, so the page cannot be used to find out which accounts exist."""
    make(db_session)
    assert authenticate(db_session, email="sam@glenwood.example", password=OTHER) is None
    assert authenticate(db_session, email="nobody@glenwood.example", password=PASSWORD) is None
    deactivate_user(db_session, email="sam@glenwood.example", actor="cli")
    db_session.commit()
    assert authenticate(db_session, email="sam@glenwood.example", password=PASSWORD) is None


def test_a_hash_below_the_current_cost_is_upgraded_on_a_successful_sign_in(
    db_session: Session,
) -> None:
    user = make(db_session)
    from services.passwords import hash_password

    user.password_hash = hash_password(PASSWORD, iterations=1_000)
    db_session.flush()
    stale = user.password_hash

    assert authenticate(db_session, email=user.email, password=PASSWORD) is not None
    assert user.password_hash != stale
    assert verify_password(PASSWORD, user.password_hash)
    # a failed sign-in upgrades nothing
    upgraded = user.password_hash
    assert authenticate(db_session, email=user.email, password=OTHER) is None
    assert user.password_hash == upgraded


# --- listing -------------------------------------------------------------------------------------


def test_users_are_listed_active_first_then_by_name(db_session: Session) -> None:
    make(db_session, email="zoe@glenwood.example", name="Zoe Ames")
    make(db_session, email="sam@glenwood.example", name="Sam Reed")
    make(db_session, email="gone@glenwood.example", name="Al Gone")
    deactivate_user(db_session, email="gone@glenwood.example", actor="cli")
    db_session.commit()
    assert [user.name for user in list_users(db_session)] == ["Sam Reed", "Zoe Ames", "Al Gone"]


# --- the command line ----------------------------------------------------------------------------


@pytest.fixture
def cli_database(monkeypatch: pytest.MonkeyPatch, test_database_url: str, db_session: Session):  # type: ignore[no-untyped-def]
    """Point ``glenwood users`` at the test database, and read back through db_session."""
    from db import session as db_session_module

    monkeypatch.setenv("DATABASE_URL", test_database_url)
    db_session_module.get_engine.cache_clear()
    yield
    db_session_module.get_engine.cache_clear()


def test_the_cli_creates_and_deactivates_a_user(
    cli_database: None, db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "users",
            "create",
            "--name",
            "Sam Reed",
            "--email",
            "Sam@Glenwood.Example",
            "--password",
            PASSWORD,
            "--actor",
            "logan",
        ]
    )
    assert code == 0
    assert "created sam@glenwood.example" in capsys.readouterr().out
    user = user_by_email(db_session, "sam@glenwood.example")
    assert user is not None and user.active is True

    assert main(["users", "deactivate", "--email", "sam@glenwood.example"]) == 0
    db_session.expire_all()
    user = user_by_email(db_session, "sam@glenwood.example")
    assert user is not None and user.active is False
    assert "deactivated sam@glenwood.example" in capsys.readouterr().out


def test_the_cli_lists_users_and_says_so_when_there_are_none(
    cli_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["users", "list"]) == 0
    assert "no users yet" in capsys.readouterr().out
    main(["users", "create", "--name", "Sam", "--email", "s@g.example", "--password", PASSWORD])
    capsys.readouterr()
    assert main(["users", "list"]) == 0
    listed = capsys.readouterr().out
    assert "active" in listed and "s@g.example" in listed


def test_the_cli_reports_a_refusal_rather_than_tracing(
    cli_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["users", "create", "--name", "Sam", "--email", "s@g.example", "--password", PASSWORD])
    capsys.readouterr()
    assert (
        main(["users", "create", "--name", "Sam", "--email", "s@g.example", "--password", PASSWORD])
        == 2
    )
    assert "already exists" in capsys.readouterr().err

    assert (
        main(["users", "create", "--name", "Sam", "--email", "t@g.example", "--password", "x"]) == 2
    )
    assert "at least" in capsys.readouterr().err

    assert main(["users", "deactivate", "--email", "nobody@g.example"]) == 2
    assert "no user with email" in capsys.readouterr().err
