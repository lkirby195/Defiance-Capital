"""audit_log.created_at from clock_timestamp()

``audit_log.created_at`` moves from ``now()`` to ``clock_timestamp()``, as ``0004`` did for
the screens and underwrites. ``now()`` is the transaction start time, so every audit row one
request wrote - an edit, the market-data pull it set off and the run after it - carried the
same timestamp and the trail had no order between them but the row id, a random UUID.
``clock_timestamp()`` reads the wall clock as each row is inserted, so the rows order as they
were written. Existing rows keep the values they were written with: a row from before this
migration still shares its transaction's ``now()`` with the intake submission beside it,
which is how ``services/history.py`` finds the actor of a submission whose audit row names
no ``submission_id``.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("audit_log", "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    op.alter_column("audit_log", "created_at", server_default=sa.text("now()"))
