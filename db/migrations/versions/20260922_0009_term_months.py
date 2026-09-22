"""deals: the term in months, beside the bucket it came from

Phase 4d. The term was derived on every read: ``services/assemble.py`` looked the bucket up
in a dict, and a ``12_PLUS`` deal — which names no number — could only be priced by typing one
into an ``UnderwriteRequest``, which the review queue's Run underwrite button does not send.
So a ``12_PLUS`` deal could not be underwritten from the queue at all, and the readiness
checklist had nothing to point at.

``term_months`` is now a column. For every bucket that names a number it is that number,
derived by the normalizer and shown read-only; for ``12_PLUS`` it is the team's own, typed on
the team-entry form or the deal page's override block. One check constraint says exactly
that, with the mapping spelled out rather than cast so the rule a reader checks is the rule
the database enforces.

Existing rows are backfilled from their bucket, which is the number they were already being
priced on — so nothing that could be underwritten before this migration stops being able to.
A ``12_PLUS`` row is left NULL, because there never was a number to recover: that is the state
the change exists to make visible.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCHES_BUCKET = "ck_deals_term_months_matches_bucket"

BUCKET_MONTHS = (
    "CASE term_bucket WHEN '3' THEN 3 WHEN '6' THEN 6 WHEN '9' THEN 9 WHEN '12' THEN 12 END"
)


def upgrade() -> None:
    op.add_column("deals", sa.Column("term_months", sa.Integer(), nullable=True))
    # The number these rows were already priced on; 12_PLUS had none and stays NULL.
    op.execute(
        f"UPDATE deals SET term_months = {BUCKET_MONTHS} "
        "WHERE term_bucket IS NOT NULL AND term_bucket <> '12_PLUS'"
    )
    op.create_check_constraint(
        MATCHES_BUCKET,
        "deals",
        "term_months IS NULL OR (term_bucket IS NOT NULL AND ("
        f"term_bucket = '12_PLUS' OR term_months = {BUCKET_MONTHS}))",
    )


def downgrade() -> None:
    op.drop_constraint(MATCHES_BUCKET, "deals", type_="check")
    op.drop_column("deals", "term_months")
