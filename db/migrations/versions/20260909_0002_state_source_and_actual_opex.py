"""properties.state_source; deals.actual_annual_taxes_usd / actual_annual_insurance_usd

Phase 1 review decisions: record whether the property state was entered or inferred,
and let the team supply actual annual taxes and insurance (USD) that override the
%-of-value opex defaults in config (SPEC §8.6). Existing property rows are backfilled
INFERRED because nobody has confirmed them.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

state_source = postgresql.ENUM("ENTERED", "INFERRED", name="state_source", create_type=False)

MONEY = sa.Numeric(14, 2)  # SPEC §5


def upgrade() -> None:
    state_source.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "properties",
        sa.Column("state_source", state_source, nullable=False, server_default="INFERRED"),
    )
    # Backfill only: new rows must state their source explicitly.
    op.alter_column("properties", "state_source", server_default=None)
    op.add_column("deals", sa.Column("actual_annual_taxes_usd", MONEY))
    op.add_column("deals", sa.Column("actual_annual_insurance_usd", MONEY))


def downgrade() -> None:
    op.drop_column("deals", "actual_annual_insurance_usd")
    op.drop_column("deals", "actual_annual_taxes_usd")
    op.drop_column("properties", "state_source")
    state_source.drop(op.get_bind(), checkfirst=True)
