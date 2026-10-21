"""versions.human_selected: whether a person chose the version

Phase 8c prep (SPEC §5). An automatic run - on arrival, after Edit Intake, after a court
search save - becomes the deal's Working version unless the Working version is one a person
chose: named it when they ran it, or set it with Mark as Working. This column is what says
so, on every version.

Existing rows are backfilled from what is on record. A version whose name is neither the
default ``v<number>`` nor ``Auto v<number>`` was named by a person; a version a
``VERSION_MARKED_WORKING`` audit row names was marked by one. Every other version - the
backfilled legacy ones included - is not.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "versions",
        sa.Column("human_selected", sa.Boolean(), server_default="false", nullable=False),
    )
    op.execute(
        """
        UPDATE versions SET human_selected = true
        WHERE name <> 'v' || number::text AND name <> 'Auto v' || number::text
        """
    )
    op.execute(
        """
        UPDATE versions SET human_selected = true
        FROM audit_log
        WHERE audit_log.action = 'VERSION_MARKED_WORKING'
          AND audit_log.table_name = 'deals'
          AND audit_log.row_id = versions.deal_id::text
          AND (audit_log.after ->> 'version')::int = versions.number
        """
    )


def downgrade() -> None:
    op.drop_column("versions", "human_selected")
