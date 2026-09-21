"""Append an ``audit_log`` row for a write.  # SPEC §5, §11

Every service write takes an actor and records one row here. The table has existed since
migration 0001 and SPEC §5 says why: credit and court data are in this system, so a later
reader has to be able to ask who changed what and when.

An audit row is not a log line. It names the table and the row it is about, and carries the
before and after of what actually changed as JSON, so the trail can be read back per deal
without re-reading a file. ``before``/``after`` hold only the fields the write touched -
never a whole row, and never a password or a hash.

Nothing here commits. The audit row is written in the caller's transaction alongside the
change it describes, so a rolled-back change cannot leave an audit row claiming it happened.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from db.models import AuditLog
from schema.models import AuditAction

DEALS = "deals"
USERS = "users"


def record_audit(
    session: Session,
    *,
    actor: str,
    action: AuditAction,
    table_name: str,
    row_id: UUID | str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one row. Flushes so the id is available; no commit."""
    row = AuditLog(
        actor=actor,
        action=action.value,
        table_name=table_name,
        row_id=str(row_id),
        before=before,
        after=after,
    )
    session.add(row)
    session.flush()
    return row


def _for_row(table_name: str, row_id: UUID | str) -> Select[tuple[AuditLog]]:
    return select(AuditLog).where(AuditLog.table_name == table_name, AuditLog.row_id == str(row_id))


def deal_trail(session: Session, deal_id: UUID, limit: int | None = None) -> list[AuditLog]:
    """Everything recorded against one deal, newest first."""
    statement = _for_row(DEALS, deal_id).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def last_action_at(
    session: Session, table_name: str, row_id: UUID | str, *actions: AuditAction
) -> AuditLog | None:
    """The most recent row for ``row_id`` whose action is one of ``actions``, or None.

    The queue asks this to find when a deal last entered LOI_SENT or HANDED_OFF, so it can
    tell a Hard flag raised since from one that was already on the file (SPEC §12).
    """
    statement = (
        _for_row(table_name, row_id)
        .where(AuditLog.action.in_([action.value for action in actions]))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )
    return session.scalar(statement)
