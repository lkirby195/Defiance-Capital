"""one value ratio: the as-is value goes, the guarantor is the borrower, the phone is digits

Phase 5a. Four changes to ``deals`` and ``borrowers``, and the same row deletion migration
``0010`` made, for the same reason.

``deals.as_is_value_team`` goes. SPEC §7.4 now has one value ratio — ``LTV = commitment /
estimated_sale_price``, NOT_AVAILABLE when there is no price — so there is no longer anything
in the engine an as-is value feeds. It is dropped rather than left to rot: a column nobody
reads is a column somebody will read next year and believe.

``deals.guarantor_name`` goes too. The Overview asks for one name, and it is the guarantor's:
the field the form used to call "Borrower Name" is now "Guarantor Name" and lives where it
always did, on ``borrowers.name``. Two name columns, one of which was optional and neither of
which said which was which, is what that arrangement actually was.

``borrowers.phone`` becomes nullable, and every NANP number already in it is rewritten from
``+15551234567`` to ``5551234567``. The phone is optional on the team-entry form now
(SPEC §4.1) and is stored as digits (``schema/masks.py``), shown and typed as
``###-###-####``. It is still the match key when there is one; a borrower without one matches
nothing. The rewrite skips any row whose digits-only form is already taken, because the column
is unique and a collision is two rows that were always the same borrower — a merge, not a
migration.

**Every ``screens`` and ``underwrites`` row is deleted**, exactly as in ``0010`` and for the
same reason. Engine ``1.1.0`` changed the result shape, not just the numbers: ``SizingResult``
has lost ``ltv_basis`` and ``as_is_value_source``, ``MetricCheck`` has lost ``basis``, and
``LeverageMetric`` no longer has ``LTV_AS_IS`` or ``LTARV``. Every stored row carries at least
one of those, every result model forbids extras, and the queue list rebuilds each deal's
latest run — so one row that cannot be rebuilt takes down the page for every deal, not only
its own. The deals keep their intake, their overrides, their status and their whole audit
trail, and re-running them is one button.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)

# The only shape the old normalizer produced for a NANP number, and the one the new one
# stores. Anything else it passed through unchanged, and so does this.
E164_NANP = r"^\+1[0-9]{10}$"
TO_DIGITS = """
    UPDATE borrowers AS b
       SET phone = substr(b.phone, 3)
     WHERE b.phone ~ '{pattern}'
       AND NOT EXISTS (SELECT 1 FROM borrowers o WHERE o.phone = substr(b.phone, 3))
""".format(pattern=E164_NANP)
TO_E164 = """
    UPDATE borrowers AS b
       SET phone = '+1' || b.phone
     WHERE b.phone ~ '^[0-9]{{10}}$'
       AND NOT EXISTS (SELECT 1 FROM borrowers o WHERE o.phone = '+1' || b.phone)
"""


def upgrade() -> None:
    # Nothing written before 1.1.0 can be rebuilt into the new result shapes; see the module
    # docstring. The deals themselves are untouched.
    op.execute("DELETE FROM underwrites")
    op.execute("DELETE FROM screens")

    op.drop_column("deals", "as_is_value_team")
    op.drop_column("deals", "guarantor_name")

    op.alter_column("borrowers", "phone", existing_type=sa.String(32), nullable=True)
    op.execute(TO_DIGITS)


def downgrade() -> None:
    # A row that was created without a phone has no number to put back, so it is given up
    # rather than invented: the column was NOT NULL before this migration and cannot be
    # again while those rows exist.
    op.execute("DELETE FROM deals WHERE borrower_id IN (SELECT id FROM borrowers WHERE phone IS NULL)")
    op.execute("DELETE FROM borrowers WHERE phone IS NULL")
    op.execute(TO_E164)
    op.alter_column("borrowers", "phone", existing_type=sa.String(32), nullable=False)

    op.add_column("deals", sa.Column("guarantor_name", sa.String(200), nullable=True))
    op.add_column("deals", sa.Column("as_is_value_team", MONEY, nullable=True))
