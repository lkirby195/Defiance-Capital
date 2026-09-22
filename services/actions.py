"""The team actions on the review queue.  # SPEC §4.6, §6.1

SPEC §4.6 makes two moves automatic (``services/lifecycle.py``); everything else on the
lifecycle is a person deciding, and this is where those decisions are applied. Each one takes
an actor, records an ``audit_log`` row carrying what moved, and does not commit - the caller
owns the transaction, so an action and its audit row land together or not at all.

The statuses each action runs from are named below rather than left implicit, because "the
button did nothing" is the worst possible answer: ``ActionNotAllowed`` says what the deal is
and what the action applies to.

The one asymmetry worth stating: the automatic Decline stops at UNDERWRITING (SPEC §4.6) so
that a re-screen cannot yank a deal out from under the person working it. A person declining
a deal by hand *is* that person, so the manual decline runs from every status still live.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Deal, Screen
from schema.models import SPLIT_PRODUCTS, AuditAction, ProductSource, Status
from services.audit import DEALS, jsonable, record_audit
from services.errors import ActionNotAllowed, ReasonRequired
from services.requests import TeamOverrides
from services.runner import load_deal

# A screened deal is the one a person pulls into review; the screen is Stage 1 and review is
# what happens to a deal that cleared it.
ADVANCE_TO_REVIEW_FROM: frozenset[Status] = frozenset({Status.SCREENED})
# Every status that is still live. A deal already closed out is not closed out again.
DECLINE_FROM: frozenset[Status] = frozenset(Status) - {Status.DECLINED, Status.DEAD}
# A deal can stop existing from anywhere, including after a decline: the borrower going
# silent on a deal GLENWOOD had already passed on is still worth recording as dead.
MARK_DEAD_FROM: frozenset[Status] = frozenset(Status) - {Status.DEAD}
# Only a declined deal is re-opened. DEAD is deliberately final.
REOPEN_FROM: frozenset[Status] = frozenset({Status.DECLINED})

# The fields the queue's override block owns, in the order the form shows them. ``product``
# is not here: it carries a source column and is handled on its own (SPEC §3).
OVERRIDE_FIELDS: tuple[str, ...] = (
    "as_is_value_team",
    "arv_team",
    "actual_annual_taxes_usd",
    "actual_annual_insurance_usd",
    "actual_annual_utilities_usd",
    "market_rent_monthly",
    "asset_type",
    "stated_exit",
    "court_records_status",
    "court_records_as_of",
    "court_records_team",
)


def _check(deal: Deal, action: str, allowed_from: frozenset[Status]) -> None:
    if deal.status not in allowed_from:
        raise ActionNotAllowed(deal.id, deal.status, action, set(allowed_from))


def _require_reason(action: str, reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise ReasonRequired(action)
    return text


def _move(
    session: Session,
    deal: Deal,
    *,
    to: Status,
    actor: str,
    action: AuditAction,
    reason: str | None = None,
) -> Deal:
    """Apply a status change and record it. Flushes; no commit."""
    before = deal.status
    deal.status = to
    session.flush()
    after: dict[str, Any] = {"status": to.value}
    if reason is not None:
        after["reason"] = reason
    record_audit(
        session,
        actor=actor,
        action=action,
        table_name=DEALS,
        row_id=deal.id,
        before={"status": before.value},
        after=after,
    )
    return deal


def advance_to_review(session: Session, deal_id: UUID, *, actor: str) -> Deal:
    """SCREENED -> IN_REVIEW: a person is taking the deal on.  # SPEC §4.6"""
    deal = load_deal(session, deal_id)
    _check(deal, "advanced to review", ADVANCE_TO_REVIEW_FROM)
    return _move(
        session,
        deal,
        to=Status.IN_REVIEW,
        actor=actor,
        action=AuditAction.ADVANCED_TO_REVIEW,
    )


def decline(session: Session, deal_id: UUID, *, actor: str, reason: str) -> Deal:
    """Close the deal out by hand, with the reason on the record.  # SPEC §4.6

    The reason is required and stored on the audit row: a declined deal the team cannot
    explain six months later is the thing this whole log exists to prevent. It is the team's
    own words, not a flag message - the engine's reasons are already on the ``screens`` row.
    """
    deal = load_deal(session, deal_id)
    _check(deal, "declined", DECLINE_FROM)
    return _move(
        session,
        deal,
        to=Status.DECLINED,
        actor=actor,
        action=AuditAction.DECLINED,
        reason=_require_reason("a decline", reason),
    )


def mark_dead(session: Session, deal_id: UUID, *, actor: str, reason: str | None = None) -> Deal:
    """Mark the deal dead: it stopped, for a reason that is not a credit decision.

    A reason is welcome and not required. A decline is GLENWOOD's judgement and has to be
    explainable; dead is usually the borrower going quiet, and forcing a sentence out of the
    team for that would only produce a column full of "no response".
    """
    deal = load_deal(session, deal_id)
    _check(deal, "marked dead", MARK_DEAD_FROM)
    text = (reason or "").strip() or None
    return _move(
        session,
        deal,
        to=Status.DEAD,
        actor=actor,
        action=AuditAction.MARKED_DEAD,
        reason=text,
    )


def reopen_status(session: Session, deal: Deal) -> Status:
    """Where a re-opened deal lands: back where the lifecycle says it was.

    SCREENED when the deal has a ``screens`` row, NEW when it has none - which happens when
    a person declined it by hand before Stage 1 ever ran. Not IN_REVIEW: taking a deal on is
    its own deliberate action, and a deal declined at screen was never in review to return
    to.
    """
    screened = session.scalar(select(Screen.id).where(Screen.deal_id == deal.id).limit(1))
    return Status.SCREENED if screened is not None else Status.NEW


def reopen(session: Session, deal_id: UUID, *, actor: str, reason: str) -> Deal:
    """Re-open a DECLINED deal, with the reason on the record.  # SPEC §4.6

    A decline is what stops a deal being priced (``check_underwritable``), so undoing one is
    exactly the write an auditor will want a name and a sentence against. The reason is
    required for that and for nothing else.
    """
    deal = load_deal(session, deal_id)
    _check(deal, "re-opened", REOPEN_FROM)
    return _move(
        session,
        deal,
        to=reopen_status(session, deal),
        actor=actor,
        action=AuditAction.REOPENED,
        reason=_require_reason("a re-open", reason),
    )


def add_note(session: Session, deal_id: UUID, *, actor: str, note: str) -> Deal:
    """Record a note against the deal. No status change.  # SPEC §5

    Notes live on ``audit_log`` rather than in a table of their own: a note is a person
    saying something about a deal at a moment, which is what this table already records, and
    a second store would mean two things to read to reconstruct what happened.
    """
    deal = load_deal(session, deal_id)
    text = _require_reason("a note", note)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.NOTE_ADDED,
        table_name=DEALS,
        row_id=deal.id,
        after={"note": text},
    )
    return deal


def _override_value(overrides: TeamOverrides, field: str) -> Any:
    """The submitted value in the form the ``deals`` column stores."""
    value = getattr(overrides, field)
    if field == "court_records_team":
        # exclude_none keeps a stored matter to the fields the team filled in, exactly as
        # db/repository.py writes them at intake.
        return [matter.model_dump(mode="json", exclude_none=True) for matter in value]
    return value


def save_overrides(
    session: Session, deal_id: UUID, overrides: TeamOverrides, *, actor: str
) -> Deal:
    """Apply the queue's override block to the deal.  # SPEC §6.1

    A replacement, not a patch: the form was rendered with the deal's current values, so what
    comes back is the state the team means the deal to be in and a blank means no value. Only
    the fields that actually moved reach the audit row, so a save that changed one number
    does not read as a save that changed eleven.

    Allowed from any status. It is data entry, not a decision - correcting a valuation on a
    declined deal before re-opening it is a real thing to want to do, and the audit row says
    who changed what either way.
    """
    deal = load_deal(session, deal_id)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in OVERRIDE_FIELDS:
        current = getattr(deal, field)
        submitted = _override_value(overrides, field)
        if current == submitted:
            continue
        before[field] = jsonable(current)
        after[field] = jsonable(submitted)
        setattr(deal, field, submitted)
    _apply_product(deal, overrides, before, after)
    if not after:
        return deal
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.OVERRIDES_SAVED,
        table_name=DEALS,
        row_id=deal.id,
        before=before,
        after=after,
    )
    return deal


def _apply_product(
    deal: Deal, overrides: TeamOverrides, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """Set the product only when the team actually chose a different one.  # SPEC §3

    A blank leaves the product alone, and a product equal to the one already on the deal
    leaves ``product_source`` alone: re-saving the block must not relabel a product the
    normalizer inferred as one a person entered.

    Moving off a split product takes the loan split with it (SPEC §8.2). A NO_DRAW or
    WHOLETAIL loan is one advance and has no purchase / rehab division, so leaving the old
    one behind would be a deal describing a shape it no longer has - and the database says
    so too (``ck_deals_loan_split_only_on_split_products``). Moving *onto* a split product
    leaves the split empty, which the underwrite then names (SPEC §8.1).
    """
    chosen = overrides.product
    if chosen is None or chosen is deal.product:
        return
    before["product"] = deal.product.value if deal.product is not None else None
    before["product_source"] = deal.product_source.value if deal.product_source else None
    deal.product = chosen
    deal.product_source = ProductSource.ENTERED
    after["product"] = chosen.value
    after["product_source"] = ProductSource.ENTERED.value
    if chosen in SPLIT_PRODUCTS:
        return
    for field in ("loan_purchase_portion", "loan_rehab_portion"):
        if getattr(deal, field) is None:
            continue
        before[field] = jsonable(getattr(deal, field))
        after[field] = None
        setattr(deal, field, None)
