"""Failed sign-ins: the row each one leaves, and the lockout they add up to.  # SPEC §11 (v0.26)

A failed sign-in is an ``audit_log`` row like everything else that happens here
(``SIGN_IN_FAILED``). Nobody is signed in, so the actor is ``anonymous``; the row is about the
account the email names - its id, or ``unknown`` when the email has no account - and carries
the normalized email and the client address (``api.ratelimit.client_address``). Never the
password, not even a wrong one: a wrong password is very often somebody's right one for
another site, or this one with a typo. The email is cut at ``EMAIL_LENGTH`` wherever a
failure is recorded, matched or logged (``recorded_email``), so a post with ten thousand
characters in the box costs a row of the usual size, and still counts.

The rows are the count. ``sign_in.lockout_failures`` of them for one email, or from one
address, inside the last ``sign_in.lockout_minutes`` (SPEC §10) lock the sign-in, and
``lockout`` says so - and for how long - before the route verifies anything, so a locked
attempt costs two queries and not a PBKDF2. Either count is enough on its own: the email
alone would let a guesser spread over many addresses keep going, and the address alone would
let one address try every email. An attempt refused while locked is not recorded (the route
writes it to the process log instead), so the lock lifts ``lockout_minutes`` after the
failures stop rather than lasting for as long as somebody keeps knocking.

The window is measured on the database's clock (``now()``), the clock that stamped the rows,
so an app server whose clock drifts cannot widen or shrink it. Nothing here commits: the
route owns the transaction, and commits the failure row itself.

In the database rather than in memory like ``api/ratelimit``: a lockout that a deploy or a
second process resets is one an attacker waits out in a minute, and the rows are wanted
anyway - SPEC §11 logs access, and a failed attempt at it is access worth logging.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import AuditLog
from schema.models import AuditAction
from services.audit import USERS, record_audit
from services.users import normalize_email, user_by_email

ANONYMOUS = "anonymous"
# ``row_id`` when the email matches no account; the email itself is in ``after``.
UNKNOWN_ACCOUNT = "unknown"
# The longest address SMTP carries (RFC 5321), and ``users.email``'s width: no account's
# email is ever cut, and anything longer is somebody filling the box.
EMAIL_LENGTH = 254


def recorded_email(email: str) -> str:
    """The email as a failed sign-in is recorded, matched and logged: normalized, then cut at
    ``EMAIL_LENGTH``."""
    return normalize_email(email)[:EMAIL_LENGTH]


@dataclass(frozen=True)
class Lockout:
    """A locked sign-in: how long until it lifts, and which count locked it."""

    seconds: int  # until the lock lifts, rounded up; at least 1
    by_email: bool
    by_address: bool

    @property
    def minutes(self) -> int:
        """The wait as the page says it, rounded up, so it never reads "0 minutes"."""
        return max(1, math.ceil(self.seconds / 60))


def record_failed_sign_in(session: Session, *, email: str, address: str) -> None:
    """One ``SIGN_IN_FAILED`` row for an attempt that did not get in. Flushes; no commit.

    The account is looked up here rather than handed in, so the route needs to know nothing
    about why ``authenticate`` said no - and every failure, whatever its reason, costs the
    same lookup. The lookup takes the whole email, so a long one that starts with somebody's
    address is not put down to their account; the row takes ``recorded_email``.
    """
    user = user_by_email(session, email)
    record_audit(
        session,
        actor=ANONYMOUS,
        action=AuditAction.SIGN_IN_FAILED,
        table_name=USERS,
        row_id=user.id if user is not None else UNKNOWN_ACCOUNT,
        after={"email": recorded_email(email), "ip": address},
    )


def _lifts_in(
    session: Session, matches: ColumnElement[bool], failures: int, minutes: int
) -> timedelta | None:
    """How long until fewer than ``failures`` matching rows are inside the window; None
    when there already are.

    Newest first, the ``failures``-th row is the one whose ageing out takes the count below
    the line, so the lock lifts when it does: its time plus the window.
    """
    window = timedelta(minutes=minutes)
    statement = (
        select(AuditLog.created_at + window - func.now())
        .where(
            AuditLog.table_name == USERS,
            AuditLog.action == AuditAction.SIGN_IN_FAILED.value,
            AuditLog.created_at > func.now() - window,
            matches,
        )
        .order_by(AuditLog.created_at.desc())
        .offset(failures - 1)
        .limit(1)
    )
    wait = session.scalar(statement)
    return wait if isinstance(wait, timedelta) else None


def lockout(
    session: Session, *, email: str, address: str, config: Config | None = None
) -> Lockout | None:
    """The lock on a sign-in for this email from this address, or None when it may be tried.

    Locked when either count has reached ``sign_in.lockout_failures`` inside the window; the
    wait is the longer of the two.
    """
    settings = (config or get_config()).sign_in
    failures, minutes = settings.lockout_failures, settings.lockout_minutes
    by_email = _lifts_in(
        session, AuditLog.after.op("->>")("email") == recorded_email(email), failures, minutes
    )
    by_address = _lifts_in(session, AuditLog.after.op("->>")("ip") == address, failures, minutes)
    waits = [wait for wait in (by_email, by_address) if wait is not None]
    if not waits:
        return None
    return Lockout(
        seconds=max(1, math.ceil(max(waits).total_seconds())),
        by_email=by_email is not None,
        by_address=by_address is not None,
    )
