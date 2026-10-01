"""the borrower's own estimates get columns of their own

Phase 6a (SPEC §4.2, §6.1). ``deals.estimated_sale_price_borrower`` and
``deals.monthly_rent_borrower`` hold what a borrower typed on the public form; they are third
in line after an adapter and the team, flagged ``BORROWER_SOURCED_VALUES`` when the engine
runs on one, and never overwritten by a team entry.

Until this migration the public form wrote the borrower's two numbers into the team's
columns, so every stored ``WEB`` deal carries them there, labelled as the team's. Each is
moved across - and the team column cleared - **unless** a person has saved the override block
or re-applied the intake on that deal since, which is the only way a team number could have
reached the team column on a web deal; those rows are left alone, because the number there
is now the team's and the borrower's original is gone. The ``INTAKE_CREATED`` audit row still
carries nothing but the channel, so this is the best reconstruction available, and it is a
reconstruction: the docstring says so, and so does the PR.

No ``screens`` or ``underwrites`` row is deleted. Engine ``1.3.0`` adds a value to an enum
and a code to another, and gives ``UnderwriteInputs`` one optional field; every stored row
still rebuilds into the models that read it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)

# A web deal nobody on the team has written to since it arrived: no override save and no
# intake edit on its audit trail. The channel is compared as text on purpose: ``0013`` adds
# WEB to the enum, and when the two run in one transaction (a fresh database upgraded to
# head) Postgres refuses to *use* the new value before it is committed. Text is fine.
UNTOUCHED_WEB_DEALS = """
    channel::text = 'WEB'
    AND NOT EXISTS (
        SELECT 1 FROM audit_log
         WHERE audit_log.table_name = 'deals'
           AND audit_log.row_id = deals.id::text
           AND audit_log.action IN ('OVERRIDES_SAVED', 'INTAKE_EDITED')
    )
"""


def upgrade() -> None:
    op.add_column("deals", sa.Column("estimated_sale_price_borrower", MONEY, nullable=True))
    op.add_column("deals", sa.Column("monthly_rent_borrower", MONEY, nullable=True))
    op.execute(
        "UPDATE deals SET estimated_sale_price_borrower = estimated_sale_price_team, "
        "estimated_sale_price_team = NULL, "
        "monthly_rent_borrower = monthly_rent, monthly_rent = NULL "
        f"WHERE {UNTOUCHED_WEB_DEALS}"
    )


def downgrade() -> None:
    # The code this goes back to reads one column per value, so a borrower number with no
    # team number beside it goes back to where the public form used to put it. One with a
    # team number beside it is lost: the team's replaces it, which is what the engine ran on.
    op.execute(
        "UPDATE deals SET estimated_sale_price_team = "
        "COALESCE(estimated_sale_price_team, estimated_sale_price_borrower), "
        "monthly_rent = COALESCE(monthly_rent, monthly_rent_borrower) "
        "WHERE channel::text = 'WEB'"
    )
    op.drop_column("deals", "monthly_rent_borrower")
    op.drop_column("deals", "estimated_sale_price_borrower")
