"""Store an ``IntakeRecord``, re-apply an edited one, restore an earlier one.  # SPEC §4.2, §4.5, §5

``db/repository.py`` knows how to turn a record into rows. This is the seam that gives that
write an actor, so a deal that appears in the queue can be traced to the person who typed it
in - the same rule every other service write follows.

It exists as its own thin function rather than as an argument to the repository because the
repository is also used by the CLI, which has no database and no actor: a fixture run builds
the same rows in memory and must not need a name to do it.

``update_intake`` is the second write: the team got the borrower back on the phone and now
has the rehab budget. It re-applies the intake form's own columns rather than patching a
field, because that is what the form posts and because ``missing_fields`` and the status are
computed from the record as a whole (``intake/normalize.py``). Only the columns the form asks
for are touched (``db/repository.form_columns``): the §8.1 economics, the valuation, the
rent, the toggles and the court search are edited on the deal page and have no box on the
form, so an edit that could not have known them leaves them exactly as they were. Nothing a
reader would want back is overwritten - every submission is kept on ``intake_submissions``,
immutable, and the audit row names the columns that moved.

``restore_intake`` is the third, and it is the second with an older payload: an intake
version the deal already carries is read back through the parser that wrote it and applied
as a **new** submission, so the versions table keeps growing and the one restored is still
there to restore again. Nothing is rewound.

All three writes populate the deal's SPEC §8.1 defaults (``services/defaults.py``) before
the audit row is written, so a deal is never stored with a blank the engine would have read
a config number into: the rate, the four fees, the closing date and the loan split on a
split product are on the row, tagged as the stand-ins they are.

None of them runs the engine. The routes do that, through ``services/autorun.py``, once
the intake is stored: a deal that arrives complete is screened and priced on the way in,
and an edited or restored one is re-run (SPEC §4.6). ``screen_is_stale`` still says when a
stored screen is older than the intake - which, with the re-run, is only when the run could
not go ahead.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, IntakeSubmission
from db.repository import (
    create_deal_from_intake,
    find_or_create_borrower,
    find_or_create_property,
    form_columns,
)
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from intake.parsers.web_form import WebApplyForm, parse_web_form
from schema.models import SPLIT_PRODUCTS, AuditAction, Channel, IntakeRecord, Status
from services.audit import DEALS, jsonable, record_audit
from services.defaults import populate
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

# The key on the public form's stored payload that is not a form field: the language the
# borrower filled it in (``api/apply_form.intake_record``).
LANGUAGE_KEY = "language"


def create_deal(
    session: Session, record: IntakeRecord, *, actor: str, config: Config | None = None
) -> Deal:
    """Create the deal and its immutable submission row, with an audit row. No commit.

    The deal is populated with its SPEC §8.1 defaults on the way in, whatever the channel,
    and the audit row names what was written as ``defaults`` beside the intake's own facts
    and the submission it was created from.
    """
    deal = create_deal_from_intake(session, record)
    defaults = populate(deal, config)
    session.flush()
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
            "submission_id": str(deal.submissions[-1].id),
            **({"intake_source": deal.intake_source} if deal.intake_source else {}),
            **({"defaults": defaults} if defaults else {}),
        },
    )
    return deal


def update_intake(
    session: Session,
    deal_id: UUID,
    record: IntakeRecord,
    *,
    actor: str,
    config: Config | None = None,
) -> Deal:
    """Re-apply an edited intake to an existing deal. Flushes; no commit.  # SPEC §4.1, §4.5

    The deal keeps its id, its history and its runs; what changes is what the intake says
    about it. The borrower and property links are re-derived, because the phone is the
    borrower match key and the address is the property one (SPEC §5) - correcting either on
    the form and leaving the deal pointed at the old row would show the team the value they
    just replaced.

    The status moves in one direction only: ``NEEDS_INFO`` to ``NEW`` once the minimum viable
    intake is complete. Nothing drags a screened deal backwards to be re-screened as if it
    were new; the re-run the route makes afterwards is what brings the verdict up to date.
    """
    deal = load_deal(session, deal_id)
    if deal.status not in EDIT_INTAKE_FROM:
        raise ActionNotAllowed(deal.id, deal.status, "edited", set(EDIT_INTAKE_FROM))
    return _apply_record(
        session, deal, record, actor=actor, action=AuditAction.INTAKE_EDITED, config=config
    )


def restore_intake(
    session: Session,
    deal_id: UUID,
    submission_id: UUID,
    *,
    actor: str,
    config: Config | None = None,
) -> Deal:
    """Re-apply an earlier intake version as a new submission. Flushes; no commit.  # SPEC §5

    The version is read back through the parser for the channel it arrived on and applied
    exactly as an edit would be: the form's columns and nothing else, a new
    ``intake_submissions`` row carrying the same payload, an audit row naming the version it
    came from. Refused on the statuses an edit is refused on, and on a version that does not
    belong to this deal or that no parser reads (``ValueError``, with the reason).
    """
    deal = load_deal(session, deal_id)
    if deal.status not in EDIT_INTAKE_FROM:
        raise ActionNotAllowed(deal.id, deal.status, "restored", set(EDIT_INTAKE_FROM))
    submission = session.get(IntakeSubmission, submission_id)
    if submission is None or submission.deal_id != deal.id:
        raise ValueError("that intake version does not belong to this deal")
    record = record_from_submission(submission, deal)
    return _apply_record(
        session,
        deal,
        record,
        actor=actor,
        action=AuditAction.INTAKE_RESTORED,
        config=config,
        restored_from=submission,
    )


def record_from_submission(submission: IntakeSubmission, deal: Deal) -> IntakeRecord:
    """The ``IntakeRecord`` a stored submission's payload normalizes to, today.  # SPEC §4.2, §4.5

    Through the same parser and normalizer the channel used when the version arrived, so
    the restored deal is what that intake would make of it now - the inferred product, the
    seeded term, the recomputed ``missing_fields`` - rather than a copy of columns as they
    were. The public form's provenance stays the deal's own: ``intake_source`` is on the
    deal rather than in the payload, and the referral note is in the payload.
    """
    payload = submission.raw_payload
    if not isinstance(payload, dict):
        raise ValueError("that intake version has no form payload to read back")
    if submission.channel is Channel.TEAM:
        team = TeamEntryForm.model_validate(payload)
        return normalize(parse_team_form(team), Channel.TEAM, raw_payload=payload)
    if submission.channel is Channel.WEB:
        web = WebApplyForm.model_validate(
            {key: value for key, value in payload.items() if key != LANGUAGE_KEY}
        )
        return normalize(
            parse_web_form(web),
            Channel.WEB,
            raw_payload=payload,
            intake_source=deal.intake_source,
            referral_note=web.referral_note,
        )
    raise ValueError(
        f"an intake version from the {submission.channel.value} channel cannot be restored; "
        "no parser reads it back yet"
    )


def _apply_record(
    session: Session,
    deal: Deal,
    record: IntakeRecord,
    *,
    actor: str,
    action: AuditAction,
    config: Config | None,
    restored_from: IntakeSubmission | None = None,
) -> Deal:
    """Write a record's form columns onto a deal, append the submission, record who did it."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    columns = form_columns(record)
    # ...plus the split, which the edit may reset and populate may fill back in.
    watched = (*columns, "loan_purchase_portion", "loan_rehab_portion")
    original = {column: getattr(deal, column) for column in watched}
    for column, value in columns.items():
        if getattr(deal, column) == value:
            continue
        before[column] = jsonable(getattr(deal, column))
        after[column] = jsonable(value)
        setattr(deal, column, value)

    _relink(session, deal, record, before, after)
    _reconcile_split(deal, before, after)
    _apply_completeness(deal, record, before, after)
    # A box the form left blank on a defaulted economic is the default again, not nothing -
    # and a column that ends where it started is not a change, so it leaves the audit row.
    for column, value in populate(deal, config).items():
        after[column] = value
    for column in list(after):
        if column in original and getattr(deal, column) == original[column]:
            del after[column]
            before.pop(column, None)

    submission = IntakeSubmission(channel=record.channel, raw_payload=record.raw_payload)
    deal.submissions.append(submission)
    session.flush()
    after["submission_id"] = str(submission.id)
    if restored_from is not None:
        after["restored_from"] = str(restored_from.id)
    record_audit(
        session,
        actor=actor,
        action=action,
        table_name=DEALS,
        row_id=deal.id,
        before=before,
        after=after,
    )
    return deal


def _reconcile_split(deal: Deal, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Clear a loan split the edit has made incoherent, so the default can stand in.  # SPEC §8.2

    The split has no box on the form, so an edit cannot restate it - but an edit can change
    the loan amount it divides, or the rehab costs the product is inferred from. A split that
    no longer adds up to the loan amount, or sits on a product that has no split, is cleared
    here and ``populate`` fills the §8.2 formula split back in on a split product, tagged as
    the default it is; the audit row carries both halves of that.
    """
    purchase, rehab = deal.loan_purchase_portion, deal.loan_rehab_portion
    if purchase is None and rehab is None:
        return
    coherent = (
        deal.product in SPLIT_PRODUCTS
        and purchase is not None
        and rehab is not None
        and purchase + rehab == deal.loan_requested
    )
    if coherent:
        return
    for column in ("loan_purchase_portion", "loan_rehab_portion"):
        before.setdefault(column, jsonable(getattr(deal, column)))
        after[column] = None
        setattr(deal, column, None)


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
