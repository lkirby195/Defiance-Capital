"""borrowers: the investing profile

10c-intake (SPEC §4.1, §5). Five nullable enum columns on ``borrowers``, no backfill: the
"About your investing" group both intake forms ask - full-time or not, years investing,
projects in progress right now, what they mostly do, who does the work. They describe the
person rather than the deal, so they live on the borrower's row and a repeat borrower's
latest answers are what every deal of theirs reads. Nothing in the engine reads them; they
are shown on the deal page, the borrower's page, and filtered on Home.

Each column has its own Postgres enum type, persisting the stored values (``0_1``,
``FIRST_PROJECT``) as every enum column here does. ``strategy`` is typed
``investing_strategy`` so the type name says what kind of strategy it is.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

work_status = postgresql.ENUM(
    "FULL_TIME", "PART_TIME", "FIRST_PROJECT", name="work_status", create_type=False
)
years_investing = postgresql.ENUM(
    "0_1", "2_5", "6_10", "10_PLUS", name="years_investing", create_type=False
)
projects_in_progress = postgresql.ENUM(
    "0", "1", "2_3", "4_PLUS", name="projects_in_progress", create_type=False
)
investing_strategy = postgresql.ENUM(
    "FLIPS", "RENTALS", "MIX", name="investing_strategy", create_type=False
)
work_crew = postgresql.ENUM("OWN_CREW", "GC", "MYSELF", name="work_crew", create_type=False)

# (column, type), in the order the forms ask the questions.
COLUMNS: tuple[tuple[str, postgresql.ENUM], ...] = (
    ("work_status", work_status),
    ("years_investing", years_investing),
    ("projects_in_progress", projects_in_progress),
    ("strategy", investing_strategy),
    ("work_crew", work_crew),
)


def upgrade() -> None:
    bind = op.get_bind()
    for column, kind in COLUMNS:
        kind.create(bind, checkfirst=True)
        op.add_column("borrowers", sa.Column(column, kind, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for column, kind in reversed(COLUMNS):
        op.drop_column("borrowers", column)
        kind.drop(bind, checkfirst=True)
