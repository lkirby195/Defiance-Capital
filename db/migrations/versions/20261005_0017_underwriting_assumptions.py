"""per-deal columns for the §8.4-§8.6 assumptions that only config carried

Phase 7c (SPEC §8.1, §8.4, §8.5, §8.6). Five numbers the three analyses read used to come
from the yaml alone: the broker's selling percentage on the flip, the rental's expense ratio
and takeout rate, and the take-back's legal costs and lost-interest months. Each becomes a
column on ``deals`` beside the §8.1 economics, with the same rule: populated with the config
value at intake and tagged ``DEFAULT`` (``services/defaults.py``), typed over on the deal
page's Underwriting Assumptions panel and then the team's own, and read by the engine when
present - config otherwise.

No row is rewritten here. An existing deal keeps NULL on all five, which the engine reads as
the config value, and is populated the first time it is opened
(``services.defaults.apply_defaults``) exactly as ``0015`` left the economics. No ``screens``
or ``underwrites`` row is deleted: engine ``1.6.0`` gives ``UnderwriteInputs`` five optional
fields and changes no result shape, so every stored row still rebuilds.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)
RATE = sa.Numeric(7, 5)


def upgrade() -> None:
    op.add_column("deals", sa.Column("broker_selling_pct", RATE, nullable=True))
    op.add_column("deals", sa.Column("rental_expenses_pct_of_rent", RATE, nullable=True))
    op.add_column("deals", sa.Column("rental_takeout_rate", RATE, nullable=True))
    op.add_column("deals", sa.Column("take_back_legal_costs_usd", MONEY, nullable=True))
    op.add_column("deals", sa.Column("take_back_lost_interest_months", sa.Integer, nullable=True))


def downgrade() -> None:
    # The code this goes back to reads the five from config alone, so a team number here is
    # lost with the column; the stored runs that used it keep it in their inputs JSON.
    # ``defaulted_fields`` is left naming columns that no longer exist: that code rewrites
    # the list whole on the next populate and reads it only by membership.
    op.drop_column("deals", "take_back_lost_interest_months")
    op.drop_column("deals", "take_back_legal_costs_usd")
    op.drop_column("deals", "rental_takeout_rate")
    op.drop_column("deals", "rental_expenses_pct_of_rent")
    op.drop_column("deals", "broker_selling_pct")
