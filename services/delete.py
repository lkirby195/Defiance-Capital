"""Delete one deal and everything hanging off it.  # SPEC §4.6 (v0.25)

The trash can on a Home row and the deal page's Delete, behind their confirmation page
(``api/routes/queue.py``). Kill keeps a deal - it is marked DEAD and sits in its section for
good; Delete is for the deal that should never have been on the list: a test entry, a
duplicate, a submission somebody typed twice. What goes: the deal, its intake submissions,
its screens, underwrites and versions, its enrichment runs, documents and ``ma_sync`` rows -
and its borrower and property, each only when no other deal still points at it, the
borrower's entities with them when no other borrower holds one. A borrower with another
deal keeps their row, their profile and their entities.

**The trail stays.** The deal's ``audit_log`` rows are not deleted: they are the record of
who did what, and an audit log a delete could empty would not be one (CLAUDE.md, "Record
everything"). One more row is written, ``DEAL_DELETED``, whose ``before`` says what the deal
was - its address, its borrower, its status, how it came in - and what else went with it,
so "where did that deal go" has an answer with a name against it. Nothing here commits; the
caller owns the transaction, so the delete and its row land together or not at all.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from db.models import (
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
    Version,
    borrower_entities,
)
from schema.models import AuditAction
from services.audit import DEALS, record_audit
from services.runner import load_deal

# The deal's own rows, children before the deal. A version points at a screen and an
# underwrite, so it goes before them.
DEAL_CHILDREN: tuple[type, ...] = (
    Version,
    Underwrite,
    Screen,
    EnrichmentRun,
    Document,
    MASync,
    IntakeSubmission,
)


def _count_and_delete(session: Session, model: Any, deal_id: UUID) -> int:
    where = model.deal_id == deal_id
    count = int(session.scalar(select(func.count()).select_from(model).where(where)) or 0)
    if count:
        session.execute(delete(model).where(where))
    return count


def _orphaned(session: Session, column: Any, row_id: UUID | None) -> bool:
    """Whether no deal points at ``row_id`` through ``column`` any more."""
    if row_id is None:
        return False
    held = session.scalar(select(func.count()).select_from(Deal).where(column == row_id))
    return not held


def _delete_borrower(session: Session, borrower_id: UUID) -> list[str]:
    """The borrower row and every entity only they held; the entity names that went."""
    entity_ids = list(
        session.scalars(
            select(borrower_entities.c.entity_id).where(
                borrower_entities.c.borrower_id == borrower_id
            )
        )
    )
    session.execute(delete(borrower_entities).where(borrower_entities.c.borrower_id == borrower_id))
    session.execute(delete(Borrower).where(Borrower.id == borrower_id))
    gone: list[str] = []
    for entity_id in entity_ids:
        still_held = session.scalar(
            select(func.count())
            .select_from(borrower_entities)
            .where(borrower_entities.c.entity_id == entity_id)
        )
        if not still_held:
            name = session.scalar(select(Entity.name).where(Entity.id == entity_id))
            session.execute(delete(Entity).where(Entity.id == entity_id))
            gone.append(name or str(entity_id))
    return gone


def delete_deal(session: Session, deal_id: UUID, *, actor: str) -> dict[str, Any]:
    """Delete the deal and its rows; record one ``DEAL_DELETED`` row; return its ``before``.

    No status rule: a deal can be deleted from anywhere, a closed loan included, because the
    confirmation page is the guard and the audit row is the record. No commit.
    """
    deal = load_deal(session, deal_id)
    prop = deal.property
    borrower = deal.borrower
    before: dict[str, Any] = {
        "status": deal.status.value,
        "channel": deal.channel.value,
        "address": (prop.address_raw or prop.listing_url) if prop is not None else None,
        "borrower": borrower.name if borrower is not None else None,
        "loan_requested": str(deal.loan_requested) if deal.loan_requested is not None else None,
    }
    borrower_id, property_id = deal.borrower_id, deal.property_id

    removed: dict[str, int] = {}
    for model in DEAL_CHILDREN:
        table = model.__table__  # type: ignore[attr-defined]
        removed[str(table.name)] = _count_and_delete(session, model, deal.id)
    session.expunge(deal)
    session.execute(delete(Deal).where(Deal.id == deal_id))
    session.flush()

    if _orphaned(session, Deal.borrower_id, borrower_id):
        assert borrower_id is not None
        if borrower is not None:
            session.expunge(borrower)
        before["borrower_removed"] = True
        entities = _delete_borrower(session, borrower_id)
        if entities:
            before["entities_removed"] = entities
    if _orphaned(session, Deal.property_id, property_id):
        if prop is not None:
            session.expunge(prop)
        session.execute(delete(Property).where(Property.id == property_id))
        before["property_removed"] = True
    session.flush()

    before["rows_removed"] = {table: count for table, count in removed.items() if count}
    record_audit(
        session,
        actor=actor,
        action=AuditAction.DEAL_DELETED,
        table_name=DEALS,
        row_id=deal_id,
        before=before,
    )
    return before
