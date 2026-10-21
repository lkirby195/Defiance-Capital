"""versions: one analysis run, its inputs whole, and which one is Working

Phase 8b (SPEC §5, §9). A version is one analysis run - the screen and the underwrite Run
Analysis produces together - with a snapshot of every input it read, a name, the person or
``system`` it ran for, and whether it is the deal's **Working** version. Exactly one version
per deal is Working: the partial unique index ``uq_versions_one_working_per_deal`` refuses a
second, so the rule holds in the database and not only in ``services/versions.py``.

**Existing runs are backfilled**, so a deal analyzed before this migration opens on its
latest result rather than on nothing, and Home keeps its verdicts. Each ``screens`` row
becomes a version together with the first ``underwrites`` row written after it and before
the next screen - the pair one Run Analysis wrote - and an underwrite left over (a JSON
underwrite run on its own) becomes a version of its own beside the screen before it. They
are numbered in the order they ran, named ``v<number>``, and the latest is Working. The
actor is read off the run's ``UNDERWRITE_RUN`` audit row, else its ``SCREEN_RUN`` row, and
``unknown`` where there is neither. What cannot be backfilled is the snapshot: the runs
recorded their engine inputs and not the deal they came from, so a backfilled version
carries ``{"legacy": true}`` and the intake submission that was in force when it ran, which
is enough for the History to place it and not enough to restore it - the page says so rather
than offering a restore that would be a guess.

Downgrading drops the table: the runs themselves stay, and so does every audit row; the
names and the Working choice go with it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
UNKNOWN_ACTOR = "unknown"

INSERT = sa.text(
    """
    INSERT INTO versions (id, deal_id, number, name, created_at, actor, engine_version,
                          config_hash, inputs_snapshot, screen_id, underwrite_id, is_working)
    VALUES (:id, :deal_id, :number, :name, :created_at, :actor, :engine_version,
            :config_hash, CAST(:snapshot AS JSONB), :screen_id, :underwrite_id, :is_working)
    """
)


def upgrade() -> None:
    op.create_table(
        "versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "deal_id", sa.Uuid(), sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("inputs_snapshot", JSONB, nullable=False),
        sa.Column("screen_id", sa.Uuid(), sa.ForeignKey("screens.id", ondelete="SET NULL")),
        sa.Column(
            "underwrite_id", sa.Uuid(), sa.ForeignKey("underwrites.id", ondelete="SET NULL")
        ),
        sa.Column("is_working", sa.Boolean(), server_default="false", nullable=False),
        sa.UniqueConstraint("deal_id", "number", name="uq_versions_deal_number"),
    )
    op.create_index(op.f("ix_versions_deal_id"), "versions", ["deal_id"])
    op.create_index(
        "uq_versions_one_working_per_deal",
        "versions",
        ["deal_id"],
        unique=True,
        postgresql_where=sa.text("is_working"),
    )
    backfill(op.get_bind())


def downgrade() -> None:
    op.drop_index("uq_versions_one_working_per_deal", table_name="versions")
    op.drop_index(op.f("ix_versions_deal_id"), table_name="versions")
    op.drop_table("versions")


def _run_actors(bind: sa.Connection) -> dict[str, str]:
    """Who each run was for, by the screen or underwrite id its audit row names."""
    found: dict[str, str] = {}
    rows = bind.execute(
        sa.text(
            "SELECT actor, after FROM audit_log WHERE table_name = 'deals' "
            "AND action IN ('SCREEN_RUN', 'UNDERWRITE_RUN') ORDER BY created_at"
        )
    )
    for actor, after in rows:
        if not isinstance(after, dict):
            continue
        for key in ("screen_id", "underwrite_id"):
            if isinstance(after.get(key), str):
                found[after[key]] = actor
    return found


def _pairs(screens: list[Any], underwrites: list[Any]) -> list[tuple[Any | None, Any | None]]:
    """One (screen, underwrite) pair per version, in the order the runs happened."""
    pairs: list[tuple[Any | None, Any | None]] = []
    taken: set[Any] = set()
    for index, screen in enumerate(screens):
        until = screens[index + 1].created_at if index + 1 < len(screens) else None
        partner = next(
            (
                row
                for row in underwrites
                if row.id not in taken
                and row.created_at >= screen.created_at
                and (until is None or row.created_at < until)
            ),
            None,
        )
        if partner is not None:
            taken.add(partner.id)
        pairs.append((screen, partner))
    for row in underwrites:
        if row.id in taken:
            continue
        before = [screen for screen in screens if screen.created_at <= row.created_at]
        pairs.append((before[-1] if before else None, row))
    pairs.sort(key=lambda pair: _when(pair))
    return pairs


def _when(pair: tuple[Any | None, Any | None]) -> datetime:
    """When a pair's version was recorded: once its last row was written, as a live one is."""
    return max(row.created_at for row in pair if row is not None)  # type: ignore[no-any-return]


def backfill(bind: sa.Connection) -> int:
    """A legacy version per stored run pair, the latest of each deal Working; the count."""
    actors = _run_actors(bind)
    screens: dict[Any, list[Any]] = defaultdict(list)
    for row in bind.execute(
        sa.text(
            "SELECT id, deal_id, engine_version, config_hash, created_at FROM screens "
            "ORDER BY created_at, id"
        )
    ):
        screens[row.deal_id].append(row)
    underwrites: dict[Any, list[Any]] = defaultdict(list)
    for row in bind.execute(
        sa.text(
            "SELECT id, deal_id, engine_version, config_hash, created_at FROM underwrites "
            "ORDER BY created_at, id"
        )
    ):
        underwrites[row.deal_id].append(row)
    submissions: dict[Any, list[Any]] = defaultdict(list)
    for row in bind.execute(
        sa.text(
            "SELECT id, deal_id, received_at FROM intake_submissions "
            "WHERE deal_id IS NOT NULL ORDER BY received_at, id"
        )
    ):
        submissions[row.deal_id].append(row)

    written = 0
    for deal_id in sorted(set(screens) | set(underwrites), key=str):
        pairs = _pairs(screens[deal_id], underwrites[deal_id])
        for number, (screen, underwrite) in enumerate(pairs, start=1):
            at = _when((screen, underwrite))
            in_force = [row for row in submissions[deal_id] if row.received_at <= at]
            source = underwrite if underwrite is not None else screen
            # The underwrite's actor first: on a Run Analysis pair it is the screen's too, and
            # on a leftover underwrite it is who ran it rather than who ran the screen before.
            actor = (actors.get(str(underwrite.id)) if underwrite is not None else None) or (
                actors.get(str(screen.id)) if screen is not None else None
            )
            bind.execute(
                INSERT,
                {
                    "id": uuid.uuid4(),
                    "deal_id": deal_id,
                    "number": number,
                    "name": f"v{number}",
                    "created_at": at,
                    "actor": actor or UNKNOWN_ACTOR,
                    "engine_version": source.engine_version,
                    "config_hash": source.config_hash,
                    "snapshot": json.dumps(
                        {
                            "legacy": True,
                            "intake": (
                                {"submission_id": str(in_force[-1].id)} if in_force else None
                            ),
                        }
                    ),
                    "screen_id": screen.id if screen is not None else None,
                    "underwrite_id": underwrite.id if underwrite is not None else None,
                    "is_working": number == len(pairs),
                },
            )
            written += 1
    return written
