"""users: the people the audit log names

Phase 4. ``audit_log`` records who changed what and has done since migration 0001, but there
was nobody to be: every write came from a script. The review queue is the first thing a
person signs in to, so this is the table behind ``audit_log.actor``.

Deliberately small. There is no self-signup and no password reset in v1: a user is created
from the command line (``glenwood users create``) and deactivated the same way, so the table
carries only what a sign-in needs and what an auditor needs to resolve an actor.

* ``email`` — the sign-in name, unique, stored lower-cased by ``services/users.py``.
* ``password_hash`` — carries its own algorithm and parameters (``services/passwords.py``),
  so a later change of algorithm is a value change and not a migration.
* ``active`` — deactivating is not deleting. ``audit_log.actor`` names people who may have
  left, and a row whose actor resolves to nobody would be worse than useless.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    # The deal page reads one row's trail newest-first, and the queue asks when a deal last
    # entered a status (SPEC §12 pinning), so time is part of that lookup.
    op.create_index(
        "ix_audit_log_table_row_created", "audit_log", ["table_name", "row_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_table_row_created", table_name="audit_log")
    op.drop_table("users")
