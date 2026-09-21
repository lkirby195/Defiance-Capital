"""deals: the two SPEC §8.1 team inputs with nowhere else to live

Phase 4. The review queue runs an underwrite from a button, so every SPEC §8.1 input it
needs has to be somewhere on the deal by then. Taxes and insurance already were (migration
0002). These two were not:

* ``actual_annual_utilities_usd`` — annual utilities, into holding costs and the REO carry.
* ``market_rent_monthly`` — the monthly rent the DSCR takeout is computed on.

Neither has a config default and neither has an adapter behind it, so there is nothing to
fall back to and a zero would not be neutral: it would understate the carry and fabricate a
DSCR shortfall. The team enters them once here, an ``UnderwriteRequest`` still outranks
them, and a deal carrying neither is named as not ready rather than priced on a guess.

Existing rows get NULL, which is exactly the state they were in: the numbers had to be typed
into the request on every run.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    op.add_column("deals", sa.Column("actual_annual_utilities_usd", MONEY, nullable=True))
    op.add_column("deals", sa.Column("market_rent_monthly", MONEY, nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "market_rent_monthly")
    op.drop_column("deals", "actual_annual_utilities_usd")
