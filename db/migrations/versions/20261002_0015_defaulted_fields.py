"""which §8.1 economics on a deal hold a default rather than a person's number

Phase 6b (SPEC §8.1, §8.2, §9.2). Until now a §8.1 economic the team had not entered was
NULL on ``deals``, the engine read the config default in its place, and the readiness
checklist called it DEFAULT because it was NULL. Now every deal is populated with the
defaults at intake - the config rate, the four config fees and, on a split product, the
formula loan split - and ``deals.defaulted_fields`` names the columns whose value arrived
that way, so a stored value can still say "nobody chose this" (``services/defaults.py``).

No row is rewritten here. An existing deal keeps its NULLs, which the engine and the
checklist still read as the config default, and is populated the first time it is opened
(``services.defaults.apply_defaults``) - a team value already on the deal is never touched.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("defaulted_fields", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    # The code this goes back to reads NULL as "the config decides", so a value that was
    # populated as a default goes back to NULL and a value a person chose stays. The loan
    # split has no NULL-means-default reading there (the underwrite refuses without one), so
    # a defaulted split stays on the deal as if the team had entered it.
    op.execute(
        """
        UPDATE deals SET
            interest_rate = CASE WHEN defaulted_fields ? 'interest_rate'
                                 THEN NULL ELSE interest_rate END,
            contingency_pct = CASE WHEN defaulted_fields ? 'contingency_pct'
                                   THEN NULL ELSE contingency_pct END,
            closing_costs_usd = CASE WHEN defaulted_fields ? 'closing_costs_usd'
                                     THEN NULL ELSE closing_costs_usd END,
            holding_costs_pct_of_cost = CASE WHEN defaulted_fields ? 'holding_costs_pct_of_cost'
                                             THEN NULL ELSE holding_costs_pct_of_cost END,
            origination_fee_pct = CASE WHEN defaulted_fields ? 'origination_fee_pct'
                                       THEN NULL ELSE origination_fee_pct END
        WHERE defaulted_fields <> '[]'::jsonb
        """
    )
    op.drop_column("deals", "defaulted_fields")
