"""deals.rental_exit_value and deals.as_is_value

10a-scenarios (SPEC §8.5, §8.6, engine 2.1.0). Two nullable money columns on ``deals``, no
backfill:

``rental_exit_value``
    The value the Rental analysis's takeout lends against. NULL means the estimated sale
    price in force stands in - the default is read where it is needed rather than copied
    onto the row, so a sale price edited later moves it (``services/defaults.py``). A number
    is one the team typed on the Underwriting Assumptions panel.

``as_is_value``
    The team's as-is valuation, optional; the Take-Back analysis sets its haircut price
    beside it when there is one. It was dropped with the as-is LTV in ``0011`` and comes
    back as a plain input: nothing in the screen reads it.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("rental_exit_value", sa.Numeric(14, 2), nullable=True))
    op.add_column("deals", sa.Column("as_is_value", sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "as_is_value")
    op.drop_column("deals", "rental_exit_value")
