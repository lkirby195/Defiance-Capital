"""deals.thanks_token and the indicative terms shown to the borrower

Phase 8e (SPEC §4.2). A web submission is sent to ``/apply/thanks/<token>``: a random,
unguessable token stored on the deal with the moment it expires
(``web_intake.thanks_token_hours``). The page shows the borrower their indicative terms when
the automatic screen said Go or Conditional and at least one pricing row is quotable, and
the deal records what it showed - the rows as rendered and the language - and when, so the
team sees on the deal page exactly what the borrower saw.

Four nullable columns, no backfill: a deal stored before this has no token (its thank-you
page was the plain one) and nothing was shown to its borrower.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("thanks_token", sa.String(length=64), nullable=True))
    op.add_column(
        "deals",
        sa.Column("thanks_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("indicative_terms_shown_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column(
            "indicative_terms_shown", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_unique_constraint("deals_thanks_token_key", "deals", ["thanks_token"])


def downgrade() -> None:
    op.drop_constraint("deals_thanks_token_key", "deals", type_="unique")
    op.drop_column("deals", "indicative_terms_shown")
    op.drop_column("deals", "indicative_terms_shown_at")
    op.drop_column("deals", "thanks_token_expires_at")
    op.drop_column("deals", "thanks_token")
