"""PAUSED on the status enum; drop the asset type and the stated exit; delete the stored underwrites

Phase 7 (SPEC §4.6, §8.1, §8.3). Three changes.

``deal_status`` gains ``PAUSED``, between ``HANDED_OFF`` and ``DECLINED``: a deal a person has
set aside, excluded from the automatic runs and returned to its prior status by Progress.
``ADD VALUE`` inside a transaction is fine on PostgreSQL 12 and later provided the new value
is not *used* in the same transaction, and nothing here uses it.

``deals.asset_type`` and ``deals.stated_exit`` go, with their enum types. The §3 exit inference
they fed is gone from the engine (``1.4.0``): the Flip analysis is on by default when there is
a sale price and the Rental analysis when there is a rent, and nothing reads either column.

**Every ``underwrites`` row is deleted.** Engine ``1.4.0`` anchors the ledger to month ends,
so a stored ledger no longer says what the engine would say about the same deal, and its
result shape has changed besides - ``exit`` is ``analyses`` and the stored inputs carry the
two dropped columns - so a row written before it cannot be rebuilt into a model that forbids
extras, and the Home page rebuilds nothing but the deal page would fail on it. The deals
keep their intake, their screens, their status and their whole audit trail; re-pricing one is
a button, and every team edit re-prices on its own (SPEC §4.6).

**The downgrade relabels rather than deletes.** Postgres cannot drop a value from an enum, so
the type is rebuilt without ``PAUSED`` - and before it is, every paused deal is put back to
the status its ``PAUSED`` audit row says it came from, or ``NEW`` when there is no such row.
The two dropped columns come back empty. The underwrites do not come back.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The status enum as 0001 created it, which the downgrade puts back.
STATUSES_BEFORE: tuple[str, ...] = (
    "NEW",
    "NEEDS_INFO",
    "SCREENED",
    "IN_REVIEW",
    "UNDERWRITING",
    "LOI_SENT",
    "HANDED_OFF",
    "DECLINED",
    "DEAD",
)

asset_type = postgresql.ENUM(
    "SFR", "UNITS_2_4", "UNITS_5_PLUS", "OTHER", name="asset_type", create_type=False
)
stated_exit = postgresql.ENUM(
    "FLIP", "HOLD", "WHOLETAIL", "UNKNOWN", name="stated_exit", create_type=False
)


def upgrade() -> None:
    op.execute("ALTER TYPE deal_status ADD VALUE IF NOT EXISTS 'PAUSED' BEFORE 'DECLINED'")
    op.drop_column("deals", "stated_exit")
    op.drop_column("deals", "asset_type")
    stated_exit.drop(op.get_bind(), checkfirst=True)
    asset_type.drop(op.get_bind(), checkfirst=True)
    op.execute("DELETE FROM underwrites")


def downgrade() -> None:
    # A paused deal goes back where its PAUSED audit row says it came from; a paused deal
    # with no such row - there should be none - lands at NEW rather than nowhere.
    op.execute(
        """
        UPDATE deals SET status = COALESCE(
            (
                SELECT (audit_log.before ->> 'status')::deal_status
                FROM audit_log
                WHERE audit_log.table_name = 'deals'
                  AND audit_log.row_id = deals.id::text
                  AND audit_log.action = 'PAUSED'
                  AND audit_log.before ->> 'status' <> 'PAUSED'
                ORDER BY audit_log.created_at DESC, audit_log.id DESC
                LIMIT 1
            ),
            'NEW'::deal_status
        )
        WHERE status = 'PAUSED'
        """
    )
    values = ", ".join(f"'{value}'" for value in STATUSES_BEFORE)
    op.execute("ALTER TYPE deal_status RENAME TO deal_status_with_paused")
    op.execute(f"CREATE TYPE deal_status AS ENUM ({values})")
    op.execute(
        "ALTER TABLE deals ALTER COLUMN status TYPE deal_status USING status::text::deal_status"
    )
    op.execute("DROP TYPE deal_status_with_paused")
    asset_type.create(op.get_bind(), checkfirst=True)
    stated_exit.create(op.get_bind(), checkfirst=True)
    op.add_column("deals", sa.Column("asset_type", asset_type, nullable=True))
    op.add_column("deals", sa.Column("stated_exit", stated_exit, nullable=True))
