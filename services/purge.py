"""Delete every deal and everything hanging off it, and leave the users.  # README

The one-time cleanup ``glenwood deals purge --all --confirm`` runs through here: every deal,
borrower, entity, property, submission, screen, underwrite, enrichment run, document,
``ma_sync`` row and audit row goes, and the ``users`` table does not. It exists for wiping
the test deals out of a database before the real ones arrive, and for nothing else - which
is why the command demands two flags and the README calls it what it is.

What it leaves behind, apart from the users, is one audit row (``DEALS_PURGED``) carrying the
count of every table it emptied. Every service write records one (CLAUDE.md, "Record
everything"), and a purge that erased the record of itself would be the one write in the
system nobody could ask about later. Nothing here commits; the caller owns the transaction,
so the purge and its row land together or not at all.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from db.models import (
    AuditLog,
    Borrower,
    Deal,
    Document,
    EnrichmentRun,
    Entity,
    IntakeSubmission,
    MASync,
    Property,
    Screen,
    Underwrite,
    borrower_entities,
)
from schema.models import AuditAction
from services.audit import DEALS, record_audit

# What ``row_id`` says on the one row the purge leaves: every deal, not one of them.
EVERY_ROW = "*"

# Children before parents, so no foreign key is left pointing at nothing mid-way. The
# borrower-entity link table goes before the two it links.
PURGED_TABLES: tuple[type, ...] = (
    AuditLog,
    MASync,
    Document,
    EnrichmentRun,
    Underwrite,
    Screen,
    IntakeSubmission,
    Deal,
    Property,
    Entity,
    Borrower,
)


def counts(session: Session) -> dict[str, int]:
    """How many rows each purged table holds, by table name."""
    out: dict[str, int] = {}
    for model in PURGED_TABLES:
        table = model.__table__  # type: ignore[attr-defined]
        out[str(table.name)] = int(session.scalar(select(func.count()).select_from(table)) or 0)
    out[str(borrower_entities.name)] = int(
        session.scalar(select(func.count()).select_from(borrower_entities)) or 0
    )
    return out


def purge_deals(session: Session, *, actor: str) -> dict[str, int]:
    """Empty every deal table, record one audit row, return what was deleted. No commit."""
    deleted = counts(session)
    session.execute(delete(borrower_entities))
    for model in PURGED_TABLES:
        session.execute(delete(model))
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.DEALS_PURGED,
        table_name=DEALS,
        row_id=EVERY_ROW,
        after={"deleted": deleted},
    )
    return deleted
