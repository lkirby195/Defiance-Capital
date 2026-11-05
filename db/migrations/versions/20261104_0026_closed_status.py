"""CLOSED on the status enum

v0.25 (SPEC §4.6, §9.1). ``deal_status`` gains ``CLOSED``, between ``HANDED_OFF`` and
``PAUSED``: the loan closed and funded, the end of the road Progress walks a deal along -
Under LOI (``LOI_SENT``) and then Closed - and the fourth of Home's six sections.
``ADD VALUE`` inside a transaction is fine on PostgreSQL 12 and later provided the new value
is not *used* in the same transaction, and nothing here uses it.

No table changes. ``audit_log.action`` is a string, so ``LOAN_CLOSED`` and ``DEAL_DELETED``
need nothing here.

**The downgrade relabels rather than deletes.** Postgres cannot drop a value from an enum, so
the type is rebuilt without ``CLOSED`` - and before it is, every closed deal is put back
Under LOI, the status Progress closed it from.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The status enum as 0016 left it, which the downgrade puts back.
STATUSES_BEFORE: tuple[str, ...] = (
    "NEW",
    "NEEDS_INFO",
    "SCREENED",
    "IN_REVIEW",
    "UNDERWRITING",
    "LOI_SENT",
    "HANDED_OFF",
    "PAUSED",
    "DECLINED",
    "DEAD",
)


def upgrade() -> None:
    op.execute("ALTER TYPE deal_status ADD VALUE IF NOT EXISTS 'CLOSED' BEFORE 'PAUSED'")


def downgrade() -> None:
    op.execute("UPDATE deals SET status = 'LOI_SENT' WHERE status = 'CLOSED'")
    values = ", ".join(f"'{value}'" for value in STATUSES_BEFORE)
    op.execute("ALTER TYPE deal_status RENAME TO deal_status_with_closed")
    op.execute(f"CREATE TYPE deal_status AS ENUM ({values})")
    op.execute(
        "ALTER TABLE deals ALTER COLUMN status TYPE deal_status USING status::text::deal_status"
    )
    op.execute("DROP TYPE deal_status_with_closed")
