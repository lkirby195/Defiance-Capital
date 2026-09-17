"""Write and read back ``screens`` and ``underwrites`` rows.  # SPEC §5

Append-only: every run writes a new row and nothing is updated in place, so a deal's
history is the rows themselves. Each row records ``engine_version`` and ``config_hash``
(CLAUDE.md, "Record everything"), and the engine result is stored whole in JSONB - split
only where the table already has a column for a piece of it - so a stored row rebuilds the
exact ``ScreenResult`` / ``UnderwriteResult`` that produced it.

Dumping in JSON mode writes every ``Decimal`` as its own digits (a string), not a float, so
the round trip is exact to the last place. ``underwrites.solved_rate`` is a NUMERIC(7,5)
copy of r* for querying and is deliberately lossy; the JSONB keeps the precise value.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Screen, Underwrite
from schema.models import (
    ScreenInputs,
    ScreenResult,
    UnderwriteInputs,
    UnderwriteResult,
)

RATE_PLACES = Decimal("0.00001")  # underwrites.solved_rate is NUMERIC(7,5)

# ScreenResult fields that have a column of their own; everything else goes to JSONB.
_SCREEN_COLUMNS = frozenset(
    {"engine_version", "config_hash", "verdict", "reasons", "suggested_reply"}
)


def _json(model: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def record_screen(
    session: Session, deal_id: UUID, inputs: ScreenInputs, result: ScreenResult
) -> Screen:
    """Append a ``screens`` row for one run. Flushes so the id is available; no commit."""
    row = Screen(
        deal_id=deal_id,
        engine_version=result.engine_version,
        config_hash=result.config_hash,
        inputs=_json(inputs),
        score_components=result.model_dump(mode="json", exclude=set(_SCREEN_COLUMNS)),
        verdict=result.verdict,
        reasons=list(result.reasons),
        suggested_reply=result.suggested_reply,
    )
    session.add(row)
    session.flush()
    return row


def screen_result(row: Screen) -> ScreenResult:
    """Rebuild the ``ScreenResult`` a row was written from."""
    return ScreenResult.model_validate(
        {
            **row.score_components,
            "engine_version": row.engine_version,
            "config_hash": row.config_hash,
            "verdict": row.verdict,
            "reasons": row.reasons,
            "suggested_reply": row.suggested_reply,
        }
    )


def record_underwrite(
    session: Session, deal_id: UUID, inputs: UnderwriteInputs, result: UnderwriteResult
) -> Underwrite:
    """Append an ``underwrites`` row for one run. Flushes so the id is available; no commit."""
    row = Underwrite(
        deal_id=deal_id,
        engine_version=result.engine_version,
        config_hash=result.config_hash,
        inputs=_json(inputs),
        outputs=result.model_dump(mode="json", exclude={"grid_lender"}),
        grid_lender=_json(result.grid_lender),
        solved_rate=result.solved_rate.quantize(RATE_PLACES),
    )
    session.add(row)
    session.flush()
    return row


def underwrite_result(row: Underwrite) -> UnderwriteResult:
    """Rebuild the ``UnderwriteResult`` a row was written from (grid included)."""
    return UnderwriteResult.model_validate({**row.outputs, "grid_lender": row.grid_lender})


def latest_screen(session: Session, deal_id: UUID) -> Screen | None:
    """The most recent ``screens`` row for a deal, or None if it has never been screened."""
    return session.scalar(
        select(Screen)
        .where(Screen.deal_id == deal_id)
        .order_by(Screen.created_at.desc(), Screen.id.desc())
        .limit(1)
    )


def latest_underwrite(session: Session, deal_id: UUID) -> Underwrite | None:
    """The most recent ``underwrites`` row for a deal, or None if it was never underwritten."""
    return session.scalar(
        select(Underwrite)
        .where(Underwrite.deal_id == deal_id)
        .order_by(Underwrite.created_at.desc(), Underwrite.id.desc())
        .limit(1)
    )
