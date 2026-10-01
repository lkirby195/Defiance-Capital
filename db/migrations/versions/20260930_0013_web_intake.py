"""the public borrower form: a WEB channel, and where a web deal came from

Phase 6 (SPEC §4.2). Three changes and no row is deleted.

The ``channel`` enum gains ``WEB``. It is shared by ``deals`` and ``intake_submissions``, and
Postgres adds a value to an enum in place - nothing is rewritten and nothing is locked for
longer than the catalog update. ``ADD VALUE`` inside a transaction is fine on PostgreSQL 12 and
later provided the new value is not *used* in the same transaction, and nothing here uses it:
the first ``WEB`` row is written by the form, after this has committed.

``deals.intake_source`` is the ``?src=`` slug on the link the borrower followed to the form,
and ``deals.referral_note`` is what they typed under "how did you hear about us?". Both are
NULL on every deal that exists today, which is every deal that came in some other way.

**The downgrade relabels rather than deletes.** Postgres cannot drop a value from an enum, so
the type is rebuilt without ``WEB`` - and before it is, every ``WEB`` row is relabelled
``TEAM``. That is the less wrong of the two choices: the deals are real and the borrower typed
them, and the code this downgrades to would refuse to load a row carrying a value its enum
does not have. The audit row that created each one still says ``WEB``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The enum as 0001 created it, which the downgrade puts back.
BEFORE: tuple[str, ...] = ("SMS", "LINK", "CONTRACT", "TEAM")
CHANNEL_COLUMNS: tuple[tuple[str, str], ...] = (("deals", "channel"), ("intake_submissions", "channel"))


def upgrade() -> None:
    op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'WEB'")
    op.add_column("deals", sa.Column("intake_source", sa.String(64), nullable=True))
    op.add_column("deals", sa.Column("referral_note", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "referral_note")
    op.drop_column("deals", "intake_source")
    for table, column in CHANNEL_COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = 'TEAM' WHERE {column} = 'WEB'")
    values = ", ".join(f"'{value}'" for value in BEFORE)
    op.execute("ALTER TYPE channel RENAME TO channel_with_web")
    op.execute(f"CREATE TYPE channel AS ENUM ({values})")
    for table, column in CHANNEL_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE channel USING {column}::text::channel"
        )
    op.execute("DROP TYPE channel_with_web")
