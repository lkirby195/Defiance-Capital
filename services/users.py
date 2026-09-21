"""Create, deactivate, find and authenticate the people who use the review queue.  # SPEC §11

The only writers of ``users``. There is no self-signup and no password reset in v1: a user is
created from the command line and deactivated the same way, and both writes take an actor and
record an ``audit_log`` row like every other service write.

``email`` is the sign-in name. It is lower-cased and stripped on the way in and on the way to
every lookup, so ``Sam@Glenwood.com`` and ``sam@glenwood.com`` are one account rather than two
and a sign-in cannot fail on a capital letter.

Nothing here returns, logs or audits a password or a hash. An ``audit_log`` row for a user
write names the user and what changed about them; the credential never appears in it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import User
from schema.models import AuditAction
from services.audit import USERS, record_audit
from services.errors import UserExists, UserNotFound
from services.passwords import hash_password, needs_rehash, verify_password


def normalize_email(email: str) -> str:
    """The stored and looked-up form of a sign-in name: stripped and lower-cased."""
    return email.strip().lower()


def user_by_email(session: Session, email: str) -> User | None:
    """The user with that email, active or not; None when there is none."""
    return session.scalar(select(User).where(User.email == normalize_email(email)))


def get_user(session: Session, user_id: UUID) -> User | None:
    """The user with that id, active or not; None when there is none."""
    return session.get(User, user_id)


def list_users(session: Session) -> list[User]:
    """Every user, active first, then by name."""
    return list(session.scalars(select(User).order_by(User.active.desc(), func.lower(User.name))))


def create_user(session: Session, *, name: str, email: str, password: str, actor: str) -> User:
    """Add a user. Raises ``UserExists`` or ``WeakPassword``; flushes, no commit.

    The hash is computed before the row is built, so a password that fails the length floor
    costs nothing and leaves nothing behind.
    """
    address = normalize_email(email)
    if user_by_email(session, address) is not None:
        raise UserExists(address)
    user = User(name=name.strip(), email=address, password_hash=hash_password(password))
    session.add(user)
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.USER_CREATED,
        table_name=USERS,
        row_id=user.id,
        after={"name": user.name, "email": user.email, "active": user.active},
    )
    return user


def deactivate_user(session: Session, *, email: str, actor: str) -> User:
    """Turn a user's sign-in off. Raises ``UserNotFound``; flushes, no commit.

    Not a delete: ``audit_log.actor`` names people who have left, and the row has to keep
    resolving to someone. ``active`` is checked on every request, so the user's live session
    stops working at once rather than at its next expiry.

    Deactivating an already-inactive user is a no-op that still records a row - the person
    doing it meant to, and the trail should show they did.
    """
    user = user_by_email(session, email)
    if user is None:
        raise UserNotFound(normalize_email(email))
    was_active = user.active
    user.active = False
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.USER_DEACTIVATED,
        table_name=USERS,
        row_id=user.id,
        before={"active": was_active},
        after={"active": False},
    )
    return user


def authenticate(session: Session, *, email: str, password: str) -> User | None:
    """The user when the password is right and the account is active; None otherwise.

    One answer for every kind of failure - no such user, wrong password, deactivated - so a
    sign-in page cannot be used to find out which accounts exist. The password is still
    verified against a deactivated user's hash before the active check, so the two paths cost
    the same.

    A hash below the current cost is upgraded here, in the one place a plaintext password is
    legitimately in hand. The caller owns the transaction, so the rehash lands with the
    ``SIGNED_IN`` row the route writes.
    """
    user = user_by_email(session, email)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        session.flush()
    return user
