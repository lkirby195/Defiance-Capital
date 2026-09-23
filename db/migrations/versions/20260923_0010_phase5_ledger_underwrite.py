"""the v0.3 underwrite: ledger inputs in, term-level model out

Phase 5. SPEC §8 is now a dated monthly cash-flow ledger with a true XIRR and three analyses
beside it (Flip, Rental, Take-Back), and engine ``1.0.0`` shares no result shape with the
0.8.1 that priced deals before it.

``deals`` gains the SPEC §8.1 inputs the ledger needs — the closing date, the interest rate,
the origination fee, the contingency, the lender's closing costs, the total holding cost, the
two analysis toggles, the loan purpose and the guarantor — and loses the three itemized opex
columns one ``holding_costs_total_usd`` replaces. Three columns are renamed to what SPEC §8.1
calls them, keeping their data: ``rehab_budget``, ``arv_team`` and ``market_rent_monthly``.

``properties`` gains the SPEC §8.1 Property Overview. None of it feeds any math; it is there
because a person reading a deal wants to know what the property is.

The term is no longer pinned to the bucket. ``term_bucket`` stays as the borrower's own
answer to "how long do you need the loan?" and still seeds ``term_months`` at intake, but the
team's term is what the deal is priced on and a deal repriced to 7 months on a 6-month ask is
a real thing, so ``ck_deals_term_months_matches_bucket`` goes.

``underwrites`` loses ``grid_lender`` and ``solved_rate`` — there is no grid and nothing is
solved for — and gains ``irr``.

**Every ``screens`` and ``underwrites`` row is deleted.** Not lightly: these tables are
append-only on purpose (CLAUDE.md, "Record everything"). But a stored underwrite from 0.8.1
has no ledger, no IRR and none of the three analyses, and there is nothing in it from which
any of them could be reconstructed; a stored screen names ``ARV_MISSING``, a flag code that no
longer exists, and carries a ``buy_closing`` its result model no longer has. Both would fail
to rebuild, and the queue list rebuilds every deal's latest run — so one orphaned row would
take down the page for every deal, not just its own. The deals keep their intake, their
overrides, their status and their whole audit trail, and re-running them is one button.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCHES_BUCKET = "ck_deals_term_months_matches_bucket"
BUCKET_MONTHS = (
    "CASE term_bucket WHEN '3' THEN 3 WHEN '6' THEN 6 WHEN '9' THEN 9 WHEN '12' THEN 12 END"
)

loan_purpose = postgresql.ENUM(
    "PURCHASE", "REFINANCE", "CASH_OUT", "CONSTRUCTION", name="loan_purpose", create_type=False
)

MONEY = sa.Numeric(14, 2)
RATE = sa.Numeric(7, 5)

# SPEC §8.1 Deal Economics and Overview, added to `deals`.
DEAL_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("guarantor_name", sa.String(200)),
    ("closing_date", sa.Date()),
    ("interest_rate", RATE),
    ("contingency_pct", RATE),
    ("closing_costs_usd", MONEY),
    ("holding_costs_total_usd", MONEY),
    ("origination_fee_pct", RATE),
    ("flip_analysis", sa.Boolean()),
    ("rental_analysis", sa.Boolean()),
)

# SPEC §8.1 Property Overview, added to `properties`.
PROPERTY_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("city", sa.String(100)),
    ("units", sa.Integer()),
    ("structures", sa.Integer()),
    ("sf", sa.Integer()),
    ("year_built", sa.Integer()),
    ("year_renovated", sa.Integer()),
    ("beds", sa.Integer()),
    ("baths", sa.Numeric(4, 1)),
    ("garage_spaces", sa.Integer()),
)

# SPEC §8.1 renames the three that kept their meaning; the data comes with them.
RENAMES: tuple[tuple[str, str], ...] = (
    ("rehab_budget", "rehab_costs"),
    ("arv_team", "estimated_sale_price_team"),
    ("market_rent_monthly", "monthly_rent"),
)

# One holding_costs_total_usd stands where these three itemized lines did (SPEC §8.1).
DROPPED_OPEX: tuple[str, ...] = (
    "actual_annual_taxes_usd",
    "actual_annual_insurance_usd",
    "actual_annual_utilities_usd",
)


def upgrade() -> None:
    # Nothing written before 1.0.0 can be rebuilt into the new result shapes; see the
    # module docstring. The deals themselves are untouched.
    op.execute("DELETE FROM underwrites")
    op.execute("DELETE FROM screens")

    op.drop_constraint(MATCHES_BUCKET, "deals", type_="check")
    loan_purpose.create(op.get_bind(), checkfirst=True)
    op.add_column("deals", sa.Column("loan_purpose", loan_purpose, nullable=True))
    for name, kind in DEAL_COLUMNS:
        op.add_column("deals", sa.Column(name, kind, nullable=True))
    for old, new in RENAMES:
        op.alter_column("deals", old, new_column_name=new)
    for name in DROPPED_OPEX:
        op.drop_column("deals", name)

    for name, kind in PROPERTY_COLUMNS:
        op.add_column("properties", sa.Column(name, kind, nullable=True))

    op.drop_column("underwrites", "grid_lender")
    op.drop_column("underwrites", "solved_rate")
    op.add_column("underwrites", sa.Column("irr", RATE, nullable=True))


def downgrade() -> None:
    op.drop_column("underwrites", "irr")
    op.add_column("underwrites", sa.Column("solved_rate", RATE, nullable=True))
    op.add_column(
        "underwrites",
        sa.Column(
            "grid_lender",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("underwrites", "grid_lender", server_default=None)

    for name, _ in reversed(PROPERTY_COLUMNS):
        op.drop_column("properties", name)

    for name in DROPPED_OPEX:
        op.add_column("deals", sa.Column(name, MONEY, nullable=True))
    for old, new in RENAMES:
        op.alter_column("deals", new, new_column_name=old)
    for name, _ in reversed(DEAL_COLUMNS):
        op.drop_column("deals", name)
    op.drop_column("deals", "loan_purpose")
    loan_purpose.drop(op.get_bind(), checkfirst=True)
    # A term that no longer matches its bucket cannot go back under the old constraint, and
    # the bucket is the number the pre-0.3 engine priced on, so it is what is restored.
    op.execute(
        f"UPDATE deals SET term_months = {BUCKET_MONTHS} "
        "WHERE term_bucket IS NOT NULL AND term_bucket <> '12_PLUS'"
    )
    op.execute("UPDATE deals SET term_months = NULL WHERE term_bucket IS NULL")
    op.create_check_constraint(
        MATCHES_BUCKET,
        "deals",
        "term_months IS NULL OR (term_bucket IS NOT NULL AND ("
        f"term_bucket = '12_PLUS' OR term_months = {BUCKET_MONTHS}))",
    )
