"""arbitrary payoff dates, and holding costs as a percentage of cost

Phase 5b. Two changes to ``deals``, and the same row deletion ``0010`` and ``0011`` made, for
the same reason.

``deals.term_stub_days`` is new. A term is no longer a whole number of months: a payoff date
that falls between two monthly anchors leaves a stub of days, the ledger prices it as a short
final period (SPEC §8.3), and this is where those days live. NULL means no stub, which is
every term entered as a number of months and every row that existed before this migration —
so the backfill is nothing at all, deliberately: an existing deal's payoff date does not move.

``deals.holding_costs_total_usd`` becomes ``deals.holding_costs_pct_of_cost``. Holding costs
are entered as a share of ``purchase_price + rehab_costs`` now (SPEC §8.1) and the dollar
figure is computed from it, so a deal whose price or rehab budget is corrected carries a carry
that moves with it. Each existing dollar figure is converted to the percentage it *was* of
that cost, rounded to the column's five decimal places, so the deal goes on carrying the same
carry to within a rounding; a deal whose price and rehab are both still unknown, or add to
nothing, has no cost to be a percentage of and lands on NULL, which is the config default.

**Every ``screens`` and ``underwrites`` row is deleted**, exactly as in ``0010`` and ``0011``.
Engine ``1.2.0`` changed the result shape: ``UnderwriteResult`` has gained ``term_stub_days``
and ``term_months_decimal``, ``DealEconomics`` has gained ``holding_costs_pct_of_cost`` and
``holding_costs_basis``, and ``LedgerEntry`` has gained ``stub_days``. Every result model
forbids extras and the queue list rebuilds each deal's latest run, so one row that cannot be
rebuilt takes down the page for every deal, not only its own. The deals keep their intake,
their overrides, their status and their whole audit trail, and re-running them is one button.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)
RATE = sa.Numeric(7, 5)

# The share of (price + rehab) each stored dollar figure was, to the column's own precision.
# A cost of nothing has no percentage, and a percentage above 1 is outside the column's range
# (RATE is 7,5) as well as outside anything a lender means by "holding costs" - both land on
# NULL, which is the config default and is what the readiness checklist will then report.
TO_PCT = """
    UPDATE deals
       SET holding_costs_pct_of_cost = ROUND(
               holding_costs_total_usd
               / (COALESCE(purchase_price, 0) + COALESCE(rehab_costs, 0)), 5)
     WHERE holding_costs_total_usd IS NOT NULL
       AND COALESCE(purchase_price, 0) + COALESCE(rehab_costs, 0) > 0
       AND holding_costs_total_usd
           <= COALESCE(purchase_price, 0) + COALESCE(rehab_costs, 0)
"""
TO_USD = """
    UPDATE deals
       SET holding_costs_total_usd = ROUND(
               holding_costs_pct_of_cost
               * (COALESCE(purchase_price, 0) + COALESCE(rehab_costs, 0)), 2)
     WHERE holding_costs_pct_of_cost IS NOT NULL
"""


def upgrade() -> None:
    # Nothing written before 1.2.0 can be rebuilt into the new result shapes; see the module
    # docstring. The deals themselves keep everything but the one converted column.
    op.execute("DELETE FROM underwrites")
    op.execute("DELETE FROM screens")

    op.add_column("deals", sa.Column("term_stub_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_deals_term_stub_needs_a_term", "deals", "term_stub_days IS NULL OR term_months IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_deals_term_runs_for_some_time",
        "deals",
        "term_months IS NULL OR term_months > 0 OR COALESCE(term_stub_days, 0) > 0",
    )

    op.add_column("deals", sa.Column("holding_costs_pct_of_cost", RATE, nullable=True))
    op.execute(TO_PCT)
    op.drop_column("deals", "holding_costs_total_usd")


def downgrade() -> None:
    op.add_column("deals", sa.Column("holding_costs_total_usd", MONEY, nullable=True))
    op.execute(TO_USD)
    op.drop_column("deals", "holding_costs_pct_of_cost")

    op.drop_constraint("ck_deals_term_runs_for_some_time", "deals", type_="check")
    op.drop_constraint("ck_deals_term_stub_needs_a_term", "deals", type_="check")
    # A term that ran past its last anchor cannot be said in whole months, so the stub is
    # given up rather than rounded: the deal's payoff date moves back to that anchor, which
    # is the only payoff date the schema before this migration could hold.
    op.drop_column("deals", "term_stub_days")
