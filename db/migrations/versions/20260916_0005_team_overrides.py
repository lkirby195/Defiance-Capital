"""deals: team-supplied valuation and court search

Phase 2d: the enrichment adapters are Phase 3, so a deal that is ready to decide on today
has nothing behind its as-is value, its ARV or its court record. The team can now supply
all three by hand (SPEC §6):

* ``as_is_value_team`` / ``arv_team`` — used when no adapter value exists. An adapter value
  always wins; these columns are never overwritten, so a later reader can see what was
  entered by hand and what superseded it.
* ``court_records_status`` — NOT_CHECKED (the default state of every existing row, and not
  the same as clean), CLEAN, or FLAGS with one typed matter per entry in
  ``court_records_team``.
* ``court_records_as_of`` — the day the team searched. Required whenever the status is
  CLEAN or FLAGS, and enforced by a check constraint: the engine has no clock, so every
  SPEC §7.2 lookback is measured from this date.

Existing rows get NULL status and an empty matter list, which is exactly how they behaved
before: the screen raises COURT_RECORDS_NOT_CHECKED and sizes with no valuation.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)

court_records_status = postgresql.ENUM(
    "NOT_CHECKED", "CLEAN", "FLAGS", name="court_records_status", create_type=False
)


def upgrade() -> None:
    court_records_status.create(op.get_bind(), checkfirst=True)
    op.add_column("deals", sa.Column("as_is_value_team", MONEY, nullable=True))
    op.add_column("deals", sa.Column("arv_team", MONEY, nullable=True))
    op.add_column("deals", sa.Column("court_records_status", court_records_status, nullable=True))
    op.add_column("deals", sa.Column("court_records_as_of", sa.Date(), nullable=True))
    op.add_column(
        "deals",
        sa.Column(
            "court_records_team",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_deals_court_records_dated_when_searched",
        "deals",
        "court_records_status IS NULL "
        "OR court_records_status = 'NOT_CHECKED' "
        "OR court_records_as_of IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_deals_court_records_dated_when_searched", "deals", type_="check")
    for column in (
        "court_records_team",
        "court_records_as_of",
        "court_records_status",
        "arv_team",
        "as_is_value_team",
    ):
        op.drop_column("deals", column)
    court_records_status.drop(op.get_bind(), checkfirst=True)
