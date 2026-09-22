"""Store an ``IntakeRecord``, and re-apply an edited one.  # SPEC §4.2, §4.5, §5, §11

``db/repository.py`` knows how to turn a record into rows. This is the seam that gives that
write an actor, so a deal that appears in the queue can be traced to the person who typed it
in - the same rule every other service write follows.

It exists as its own thin function rather than as an argument to the repository because the
repository is also used by the CLI, which has no database and no actor: a fixture run builds
the same rows in memory and must not need a name to do it.

``update_intake`` is the second write: the team got the borrower back on the phone and now
has the rehab budget. It re-applies the whole intake rather than patching a field, because
that is what the form posts and because ``missing_fields`` and the status are computed from
the record as a whole (``intake/normalize.py``). Nothing a reader would want back is
overwritten - every submission is kept on ``intake_submissions``, immutable, and the audit
row names the columns that moved.

**A re-screen is not automatic.** An edited intake is new information, and whether it is
worth re-running Stage 1 on is a person's call; ``screen_is_stale`` is how the deal page
tells them there is one to make.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Deal, IntakeSubmission
from db.repository import (
    create_deal_from_intake,
    find_or_create_borrower,
    find_or_create_property,
    intake_columns,
)
from schema.models import AuditAction, IntakeRecord, Status
from services.audit import DEALS, jsonable, record_audit
from services.errors import ActionNotAllowed
from services.persistence import latest_screen
from services.runner import load_deal

# Every status where the intake is still a live description of a deal somebody is working.
# A DECLINED or DEAD deal is closed out (re-open it first); LOI_SENT and HANDED_OFF are past
# the point where terms went out on these numbers, and editing them under a sent LOI would
# leave the file disagreeing with the document.
EDIT_INTAKE_FROM: frozenset[Status] = frozenset(Status) - {
    Status.DECLINED,
    Status.DEAD,
    Status.LOI_SENT,
    Status.HANDED_OFF,
}


def create_deal(session: Session, record: IntakeRecord, *, actor: str) -> Deal:
    """Create the deal and its immutable submission row, with an audit row. No commit."""
    deal = create_deal_from_intake(session, record)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.INTAKE_CREATED,
        table_name=DEALS,
        row_id=deal.id,
        after={
            "channel": deal.channel.value,
            "status": deal.status.value,
            "missing_fields": list(deal.missing_fields),
        },
    )
    return deal


def update_intake(session: Session, deal_id: UUID, record: IntakeRecord, *, actor: str) -> Deal:
    """Re-apply an edited intake to an existing deal. Flushes; no commit.  # SPEC §4.1, §4.5

    The deal keeps its id, its history and its runs; what changes is what the intake says
    about it. The borrower and property links are re-derived, because the phone is the
    borrower match key and the address is the property one (SPEC §5) - correcting either on
    the form and leaving the deal pointed at the old row would show the team the value they
    just replaced.

    The status moves in one direction only: ``NEEDS_INFO`` to ``NEW`` once the minimum viable
    intake is complete. Nothing drags a screened deal backwards to be re-screened as if it
    were new; a re-screen is a button, and ``screen_is_stale`` is what says to press it.
    """
    deal = load_deal(session, deal_id)
    if deal.status not in EDIT_INTAKE_FROM:
        raise ActionNotAllowed(deal.id, deal.status, "edited", set(EDIT_INTAKE_FROM))

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for column, value in intake_columns(record).items():
        if getattr(deal, column) == value:
            continue
        before[column] = jsonable(getattr(deal, column))
        after[column] = jsonable(value)
        setattr(deal, column, value)

    _relink(session, deal, record, before, after)
    _apply_completeness(deal, record, before, after)

    deal.submissions.append(
        IntakeSubmission(channel=record.channel, raw_payload=record.raw_payload)
    )
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.INTAKE_EDITED,
        table_name=DEALS,
        row_id=deal.id,
        before=before,
        after=after,
    )
    return deal


def _relink(
    session: Session,
    deal: Deal,
    record: IntakeRecord,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Point the deal at the borrower and property its edited intake describes.

    The borrower's own name and email are corrected in place when the edit supplies a
    different one: the row is the person behind the phone number, and a typed name is the
    only thing that ever set it. A new entity is linked; an old one is not unlinked, because
    the link is shared with the borrower's other deals and is not this form's to remove.
    """
    borrower = find_or_create_borrower(session, record.borrower)
    if borrower is not None:
        for attribute in ("name", "email"):
            value = getattr(record.borrower, attribute)
            if value and value != getattr(borrower, attribute):
                before[f"borrower.{attribute}"] = jsonable(getattr(borrower, attribute))
                after[f"borrower.{attribute}"] = jsonable(value)
                setattr(borrower, attribute, value)
        if borrower is not deal.borrower:
            before["borrower_id"] = jsonable(deal.borrower_id)
            after["borrower_id"] = jsonable(borrower.id)
            deal.borrower = borrower

    prop = find_or_create_property(session, record.property)
    if prop is not None and prop is not deal.property:
        before["property_id"] = jsonable(deal.property_id)
        after["property_id"] = jsonable(prop.id)
        deal.property = prop


def _apply_completeness(
    deal: Deal,
    record: IntakeRecord,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Update ``missing_fields``, and leave ``NEEDS_INFO`` when nothing is outstanding."""
    missing = list(record.missing_fields)
    if list(deal.missing_fields) != missing:
        before["missing_fields"] = list(deal.missing_fields)
        after["missing_fields"] = missing
        deal.missing_fields = missing
    if deal.status is Status.NEEDS_INFO and not missing:
        before["status"] = deal.status.value
        after["status"] = Status.NEW.value
        deal.status = Status.NEW


def latest_submission(session: Session, deal_id: UUID) -> IntakeSubmission | None:
    """The most recent ``intake_submissions`` row for a deal, or None if it has none."""
    return session.scalar(
        select(IntakeSubmission)
        .where(IntakeSubmission.deal_id == deal_id)
        .order_by(IntakeSubmission.received_at.desc(), IntakeSubmission.id.desc())
        .limit(1)
    )


def screen_is_stale(session: Session, deal_id: UUID) -> bool:
    """True when the intake changed after the last screen ran.  # SPEC §7

    A deal that has never been screened is not stale - there is nothing to be out of date.
    Neither is one whose only submission is the one it was created from.
    """
    screen = latest_screen(session, deal_id)
    if screen is None:
        return False
    submission = latest_submission(session, deal_id)
    return submission is not None and submission.received_at > screen.created_at
