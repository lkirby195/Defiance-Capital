"""deals.asset_type; drop the orphaned underwrites.grid_borrower

Phase 2b review decisions:

* ``deals.asset_type`` (SFR / UNITS_2_4 / UNITS_5_PLUS / OTHER) is captured at intake and
  drives the SPEC §3 exit inference together with the term. Nullable: existing rows predate
  the field and the inference treats an unknown asset type as "no resale rule fires".
* ``underwrites.grid_borrower`` was orphaned when Phase 2b settled on a single lender grid
  (SPEC §8.5). No row has ever been written to ``underwrites``, so the drop loses nothing;
  the downgrade restores the column as NOT NULL with an empty-object default.
* ``screens.created_at`` and ``underwrites.created_at`` move from ``now()`` to
  ``clock_timestamp()``. ``now()`` is the transaction start time, so two runs recorded in
  one transaction shared a timestamp and "the latest screen" had no answer. Existing rows
  keep the values they were written with.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

asset_type = postgresql.ENUM(
    "SFR", "UNITS_2_4", "UNITS_5_PLUS", "OTHER", name="asset_type", create_type=False
)


def upgrade() -> None:
    asset_type.create(op.get_bind(), checkfirst=True)
    op.add_column("deals", sa.Column("asset_type", asset_type, nullable=True))
    op.drop_column("underwrites", "grid_borrower")
    for table in ("screens", "underwrites"):
        op.alter_column(table, "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table in ("screens", "underwrites"):
        op.alter_column(table, "created_at", server_default=sa.text("now()"))
    op.add_column(
        "underwrites",
        sa.Column(
            "grid_borrower",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("underwrites", "grid_borrower", server_default=None)
    op.drop_column("deals", "asset_type")
    asset_type.drop(op.get_bind(), checkfirst=True)
