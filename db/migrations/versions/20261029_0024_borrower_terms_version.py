"""deals.borrower_terms_version: the version the thank-you page shows

10d-borrower (SPEC §4.2). The borrower can now change five of their own numbers from the
thank-you page while its link is live; each save is a new intake submission and a new
automatic run named ``Borrower edit n``, and the page then shows that run's indicative terms
rather than the arrival run's. This column is the number of the version the page reads -
set to the arrival version when a web deal is created and moved by each borrower edit -
so the page and the record of what it showed name the run they came from. Nullable, no
backfill: a deal without it reads version 1, as the page always did. Not an input, so a
version snapshot leaves it out and a restore leaves it be.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("borrower_terms_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "borrower_terms_version")
