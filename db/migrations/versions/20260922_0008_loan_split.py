"""deals: the purchase / rehab split of a split loan

Phase 4c. SPEC §8.2 used to derive the purchase portion (``commitment - rehab_adj``) and let
the team override it with a single number typed into an ``UnderwriteRequest``. It is now two
columns on the deal, entered together on the team-entry form and adding up to the loan
requested:

* ``loan_purchase_portion`` — the purchase advance; the Principal Note on SPLIT_PRINCIPAL.
* ``loan_rehab_portion`` — the rehab money; the holdback on SPLIT_DRAW, Tranche A on
  SPLIT_PRINCIPAL.

Two check constraints, both about what a split *is* rather than about whether one is there:
the halves are set together, and they exist only on a product that has a split. Presence is
deliberately not a constraint — a borrower-channel intake carries a loan amount and nothing
about its shape (SPEC §4.1, §4.2), so NULL on a SPLIT_DRAW deal is a real state. The
underwrite is what refuses it (SPEC §8.1).

Existing rows get NULL on both, which is the state they were in: nothing had a stored split,
and a deal that was priced with an override carried it on the request, not on the row.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)

SET_TOGETHER = "ck_deals_loan_split_set_together"
SPLIT_PRODUCTS_ONLY = "ck_deals_loan_split_only_on_split_products"


def upgrade() -> None:
    op.add_column("deals", sa.Column("loan_purchase_portion", MONEY, nullable=True))
    op.add_column("deals", sa.Column("loan_rehab_portion", MONEY, nullable=True))
    op.create_check_constraint(
        SET_TOGETHER,
        "deals",
        "(loan_purchase_portion IS NULL) = (loan_rehab_portion IS NULL)",
    )
    # `NULL IN (...)` is NULL and a CHECK passes on NULL, so the product is tested for
    # presence explicitly: without it, a deal with no product yet could carry a split.
    op.create_check_constraint(
        SPLIT_PRODUCTS_ONLY,
        "deals",
        "loan_purchase_portion IS NULL "
        "OR (product IS NOT NULL AND product IN ('SPLIT_DRAW', 'SPLIT_PRINCIPAL'))",
    )


def downgrade() -> None:
    op.drop_constraint(SPLIT_PRODUCTS_ONLY, "deals", type_="check")
    op.drop_constraint(SET_TOGETHER, "deals", type_="check")
    op.drop_column("deals", "loan_rehab_portion")
    op.drop_column("deals", "loan_purchase_portion")
