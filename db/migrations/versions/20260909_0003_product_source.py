"""deals.product_source: whether the product was entered by the team or inferred

Phase 2a review decision: when the team does not supply a product the normalizer
infers NO_DRAW (rehab_budget == 0) or SPLIT_DRAW, and the deal records which. The
check constraint keeps ``product`` and ``product_source`` set together. Existing rows
have neither (Phase 1 never wrote ``product``), so no backfill is needed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

product_source = postgresql.ENUM("ENTERED", "INFERRED", name="product_source", create_type=False)


def upgrade() -> None:
    product_source.create(op.get_bind(), checkfirst=True)
    op.add_column("deals", sa.Column("product_source", product_source, nullable=True))
    op.create_check_constraint(
        "ck_deals_product_and_source_together",
        "deals",
        "(product IS NULL) = (product_source IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_deals_product_and_source_together", "deals", type_="check")
    op.drop_column("deals", "product_source")
    product_source.drop(op.get_bind(), checkfirst=True)
