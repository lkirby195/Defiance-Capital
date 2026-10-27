"""deals: two loan structures, no analysis toggles, the broker default at 6%

10a-structures (SPEC §3, §8.1, engine 2.0.0). Three changes to ``deals`` and nothing else.

**The product enum has two values.** ``ACQUISITION`` ("100% at close") and
``ACQUISITION_REHAB`` ("Holdback"). Postgres cannot drop a value from an enum, so the type is
rebuilt and every row mapped across:

    NO_DRAW, WHOLETAIL               -> ACQUISITION
    SPLIT_DRAW, SPLIT_PRINCIPAL      -> ACQUISITION_REHAB

A ``SPLIT_PRINCIPAL`` deal keeps its two split columns as they are: its Principal Note is the
advance at closing and its Tranche A the rehab portion held back, and the two already add up
to the loan amount (``ck_deals_loan_split_set_together``, ``validate_loan_split``). It is now
priced as a holdback - interest on the whole commitment from close rather than on the drawn
balance - and a Tranche A above the contingency-adjusted budget, which used to lower the
commitment, now moves the excess to the advance at closing. A ``WHOLETAIL`` deal carries no
split. The check that a split sits only on a split product is rebuilt for the one value.

**The Flip and Rental toggles are gone** (SPEC §8.1): all three analyses run on every deal, so
``flip_analysis`` and ``rental_analysis`` are dropped. A version snapshot that names them is
restored without them (``services/versions.apply_snapshot`` puts back the columns the table
has).

**The broker's default is 6%** (``fees.broker_selling_pct``, was 4%). A deal whose
``broker_selling_pct`` is the 4% stand-in it was populated with - tagged in
``defaulted_fields`` - is moved to the new one, so it goes on carrying the default rather than
a 4% nobody chose. A 4% somebody typed is not tagged and stays.

What is not rewritten: the stored screens, underwrites, versions, intake payloads and audit
rows that name a retired product. They are the record of what ran; ``Product`` reads each
retired code as the structure it became (``schema.models.LEGACY_PRODUCTS``), the same mapping
as above, and ``UnderwriteResult`` still reads a run's toggles and ``profit_yield``.

**The downgrade is lossy and says so.** ``ACQUISITION`` goes back to ``NO_DRAW`` and
``ACQUISITION_REHAB`` to ``SPLIT_DRAW``: which deals had been ``WHOLETAIL`` or
``SPLIT_PRINCIPAL`` is not kept. The toggle columns come back empty (the default), and a
tagged 6% goes back to 4%.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPLIT_PRODUCTS_ONLY = "ck_deals_loan_split_only_on_split_products"

UPGRADE_MAP = {
    "NO_DRAW": "ACQUISITION",
    "WHOLETAIL": "ACQUISITION",
    "SPLIT_DRAW": "ACQUISITION_REHAB",
    "SPLIT_PRINCIPAL": "ACQUISITION_REHAB",
}
DOWNGRADE_MAP = {"ACQUISITION": "NO_DRAW", "ACQUISITION_REHAB": "SPLIT_DRAW"}
OLD_VALUES = ("NO_DRAW", "SPLIT_DRAW", "SPLIT_PRINCIPAL", "WHOLETAIL")
NEW_VALUES = ("ACQUISITION", "ACQUISITION_REHAB")

# The broker default before and after (``fees.broker_selling_pct``), as stored.
BROKER_BEFORE = "0.04"
BROKER_AFTER = "0.06"


def _retype_product(values: Sequence[str], mapping: dict[str, str], split_value: str) -> None:
    """Rebuild the ``product`` enum as ``values`` and map every row through ``mapping``."""
    op.drop_constraint(SPLIT_PRODUCTS_ONLY, "deals", type_="check")
    op.execute("ALTER TYPE product RENAME TO product_retired")
    labels = ", ".join(f"'{value}'" for value in values)
    op.execute(f"CREATE TYPE product AS ENUM ({labels})")
    cases = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
    op.execute(
        "ALTER TABLE deals ALTER COLUMN product TYPE product "
        f"USING (CASE product::text {cases} END)::product"
    )
    op.execute("DROP TYPE product_retired")
    op.create_check_constraint(
        SPLIT_PRODUCTS_ONLY,
        "deals",
        f"loan_purchase_portion IS NULL OR (product IS NOT NULL AND product = '{split_value}')",
    )


def _move_tagged_broker(old: str, new: str) -> None:
    """A tagged default broker percentage moves with the default; a chosen one stays."""
    op.execute(
        f"UPDATE deals SET broker_selling_pct = {new} "
        f"WHERE broker_selling_pct = {old} "
        "AND defaulted_fields @> '[\"broker_selling_pct\"]'::jsonb"
    )


def upgrade() -> None:
    _retype_product(NEW_VALUES, UPGRADE_MAP, "ACQUISITION_REHAB")
    op.drop_column("deals", "rental_analysis")
    op.drop_column("deals", "flip_analysis")
    _move_tagged_broker(BROKER_BEFORE, BROKER_AFTER)


def downgrade() -> None:
    _move_tagged_broker(BROKER_AFTER, BROKER_BEFORE)
    op.add_column("deals", sa.Column("flip_analysis", sa.Boolean(), nullable=True))
    op.add_column("deals", sa.Column("rental_analysis", sa.Boolean(), nullable=True))
    # The split check names SPLIT_PRINCIPAL too, as 0008 wrote it.
    _retype_product(OLD_VALUES, DOWNGRADE_MAP, "SPLIT_DRAW")
    op.drop_constraint(SPLIT_PRODUCTS_ONLY, "deals", type_="check")
    op.create_check_constraint(
        SPLIT_PRODUCTS_ONLY,
        "deals",
        "loan_purchase_portion IS NULL "
        "OR (product IS NOT NULL AND product IN ('SPLIT_DRAW', 'SPLIT_PRINCIPAL'))",
    )
