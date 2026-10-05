"""The team actions on the review queue.  # SPEC §4.6, §6.1

SPEC §4.6 makes two moves automatic (``services/lifecycle.py``); everything else on the
lifecycle is a person deciding, and this is where those decisions are applied. Each one takes
an actor, records an ``audit_log`` row carrying what moved, and does not commit - the caller
owns the transaction, so an action and its audit row land together or not at all.

The statuses each action runs from are named below rather than left implicit, because "the
button did nothing" is the worst possible answer: ``ActionNotAllowed`` says what the deal is
and what the action applies to.

The three controls on every Home row are here too (SPEC §4.6). **Progress** advances a deal
to the next status - a screened deal into review, exactly as before - and, on a paused deal,
returns it to the status it was paused from. **Pause** sets a live deal aside: nothing
automatic runs on it until Progress brings it back, and the audit row that paused it is
where its prior status is kept, so there is no second column to keep in step with the first.
**Kill** marks a deal dead, with the confirmation page in front of it (``api/routes/queue``).

The one asymmetry worth stating: the automatic Decline stops at UNDERWRITING (SPEC §4.6) so
that a re-screen cannot yank a deal out from under the person working it. A person declining
a deal by hand *is* that person, so the manual decline runs from every status still live.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, Screen
from schema.models import (
    SPLIT_PRODUCTS,
    AuditAction,
    ProductSource,
    Status,
    validate_loan_split,
)
from services.audit import DEALS, jsonable, last_action_at, record_audit
from services.defaults import populate
from services.errors import ActionNotAllowed, ReasonRequired
from services.requests import TeamOverrides
from services.runner import load_deal

# The two closed statuses: a deal that is over, one way or the other.
CLOSED: frozenset[Status] = frozenset({Status.DECLINED, Status.DEAD})
# A screened deal is the one a person pulls into review; the screen is Stage 1 and review is
# what happens to a deal that cleared it.
ADVANCE_TO_REVIEW_FROM: frozenset[Status] = frozenset({Status.SCREENED})
# Progress is the advance above, plus the way back off a pause.
PROGRESS_FROM: frozenset[Status] = ADVANCE_TO_REVIEW_FROM | {Status.PAUSED}
# A live deal can be set aside; a paused or closed one already is.
PAUSE_FROM: frozenset[Status] = frozenset(Status) - CLOSED - {Status.PAUSED}
# Only a paused deal is resumed.
RESUME_FROM: frozenset[Status] = frozenset({Status.PAUSED})
# Every status that is still live. A deal already closed out is not closed out again.
DECLINE_FROM: frozenset[Status] = frozenset(Status) - CLOSED
# A deal can stop existing from anywhere, including after a decline: the borrower going
# silent on a deal GLENWOOD had already passed on is still worth recording as dead.
MARK_DEAD_FROM: frozenset[Status] = frozenset(Status) - {Status.DEAD}
# Only a declined deal is re-opened. DEAD is deliberately final.
REOPEN_FROM: frozenset[Status] = frozenset({Status.DECLINED})
# The two columns of the loan split (SPEC §8.2), applied after the product.
SPLIT_COLUMNS: tuple[str, str] = ("loan_purchase_portion", "loan_rehab_portion")

# The fields the queue's override block owns, in the order the form shows them. ``product``
# is not here: it carries a source column and is handled on its own (SPEC §3). Nor is the
# loan split: it is applied after the product, against the product the deal ends up with.
OVERRIDE_FIELDS: tuple[str, ...] = (
    "loan_purpose",
    "closing_date",
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
    "estimated_sale_price_team",
    "monthly_rent",
    "flip_analysis",
    "rental_analysis",
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


def pause(session: Session, deal_id: UUID, *, actor: str) -> Deal:
    """Set a live deal aside.  # SPEC §4.6

    The status it had is on the audit row's ``before``, which is what ``resume`` reads; no
    column on the deal says it, so there is nothing to fall out of step. A paused deal keeps
    its runs and its intake and is not run on automatically until it is brought back.
    """
    deal = load_deal(session, deal_id)
    _check(deal, "paused", PAUSE_FROM)
    return _move(session, deal, to=Status.PAUSED, actor=actor, action=AuditAction.PAUSED)


def paused_from(session: Session, deal: Deal) -> Status:
    """Where a paused deal goes back to: the status on the row that paused it.  # SPEC §4.6

    A paused deal with no such row got there some other way (a script, a migration); it
    lands where a re-opened deal does - SCREENED if it has ever been screened, else NEW -
    rather than being stuck.
    """
    row = last_action_at(session, DEALS, deal.id, AuditAction.PAUSED)
    before = (row.before or {}).get("status") if row is not None else None
    if isinstance(before, str) and before in Status.__members__.values():
        prior = Status(before)
        if prior is not Status.PAUSED:
            return prior
    return reopen_status(session, deal)


def resume(session: Session, deal_id: UUID, *, actor: str) -> Deal:
    """PAUSED -> the status it was paused from.  # SPEC §4.6"""
    deal = load_deal(session, deal_id)
    _check(deal, "resumed", RESUME_FROM)
    return _move(
        session,
        deal,
        to=paused_from(session, deal),
        actor=actor,
        action=AuditAction.RESUMED,
    )


def progress(session: Session, deal_id: UUID, *, actor: str) -> Deal:
    """The Progress control: the next status, or the prior one off a pause.  # SPEC §4.6

    One button for a person, two moves underneath: a paused deal is resumed, anything else
    is advanced to review. The refusal names ``PROGRESS_FROM``, so a deal that is neither
    screened nor paused is told both things it could have been.
    """
    deal = load_deal(session, deal_id)
    if deal.status is Status.PAUSED:
        return resume(session, deal_id, actor=actor)
    _check(deal, "progressed", PROGRESS_FROM)
    return advance_to_review(session, deal_id, actor=actor)


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
    """Kill the deal: it stopped, for a reason that is not a credit decision.  # SPEC §4.6

    A reason is welcome and not required. A decline is GLENWOOD's judgement and has to be
    explainable; dead is usually the borrower going quiet, and forcing a sentence out of the
    team for that would only produce a column full of "no response". The confirmation in
    front of this is the page's (``api/routes/queue.py``), not the service's: a script that
    calls this has already decided.
    """
    deal = load_deal(session, deal_id)
    _check(deal, "killed", MARK_DEAD_FROM)
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
    session: Session,
    deal_id: UUID,
    overrides: TeamOverrides,
    *,
    actor: str,
    config: Config | None = None,
) -> Deal:
    """Apply the queue's override block to the deal.  # SPEC §6.1

    A replacement, not a patch: the form was rendered with the deal's current values, so what
    comes back is the state the team means the deal to be in and a blank means no value -
    and for the economics that have one, the default, which is written back onto the deal
    and tagged (``services/defaults.py``) in the same save and the same audit row. Only the
    fields that actually moved reach the audit row, so a save that changed one number does
    not read as a save that changed eleven.

    Allowed from any status. It is data entry, not a decision - correcting a valuation on a
    declined deal before re-opening it is a real thing to want to do, and the audit row says
    who changed what either way.

    The borrower's own estimates (SPEC §4.2) are not in the block and are not touched by it:
    a team sale price or rent replaces the borrower's as the value in force
    (``services/assemble.py``), and the borrower's figure stays in its own column for the
    audit trail to show what was claimed.
    """
    deal = load_deal(session, deal_id)
    before, after = apply_overrides(deal, overrides, config)
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


def apply_overrides(
    deal: Deal, overrides: TeamOverrides, config: Config | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Set the block on a deal row; return the before and after of what moved.

    Pure: no session, no audit row, no commit. ``save_overrides`` wraps it with all three,
    and the CLI applies a fixture's ``team_overrides`` block to an unpersisted row through it
    (``cli/fixtures.py``), so a web deal the team then corrected can be run with no database
    behind it, through the same code the queue runs.

    A blank on a defaulted economic, or on the split of a split product, ends as the default
    rather than as nothing: ``populate`` fills every blank the block left and relabels the
    rest, and what it wrote lands in ``after`` beside the team's own changes.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    original = {field: getattr(deal, field) for field in (*OVERRIDE_FIELDS, *SPLIT_COLUMNS)}
    for field in OVERRIDE_FIELDS:
        current = getattr(deal, field)
        submitted = _override_value(overrides, field)
        if current == submitted:
            continue
        before[field] = jsonable(current)
        after[field] = jsonable(submitted)
        setattr(deal, field, submitted)
    _apply_product(deal, overrides, before, after)
    _apply_split(deal, overrides, before, after)
    _apply_term(deal, overrides, before, after)
    for field, value in populate(deal, config).items():
        after[field] = value
    # A box left blank that the default filled back in has not moved; it leaves the row.
    for field in list(after):
        if field in original and getattr(deal, field) == original[field]:
            del after[field]
            before.pop(field, None)
    return before, after


def _apply_split(
    deal: Deal, overrides: TeamOverrides, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """Set the loan split, against the product the deal now has.  # SPEC §8.2

    After ``_apply_product``, so a split typed beside a move onto a split product is kept
    and one typed beside a move off one is refused rather than stored. The two halves have
    to add up to the loan amount, which the block does not carry and the deal does, so the
    check runs here. Both blank clears the split, which ``populate`` then fills with the
    §8.2 formula on a split product - the default the page tagged it with.
    """
    submitted = (overrides.loan_purchase_portion, overrides.loan_rehab_portion)
    if deal.product not in SPLIT_PRODUCTS:
        # The block's split boxes are rendered for a split product; a save that moves the
        # deal off one posts them too, and a single-note loan carries no split (SPEC §8.2).
        # ``_apply_product`` has already cleared the old one.
        return
    validate_loan_split(deal.product, deal.loan_requested, *submitted)
    for field, value in zip(SPLIT_COLUMNS, submitted, strict=True):
        if getattr(deal, field) == value:
            continue
        before[field] = jsonable(getattr(deal, field))
        after[field] = jsonable(value)
        setattr(deal, field, value)


def _apply_term(
    deal: Deal, overrides: TeamOverrides, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """Set the term the deal is priced on.  # SPEC §8.1

    The block takes a term in months and nothing else: the payoff date is derived - the end
    of the month that is the closing month plus the term - and is shown read-only beside the
    box. A term typed in months clears any stub the deal carried, because a term said in
    months has none; nothing in the queue writes a stub today (``schema/dates.py``). The
    bucket does not come into it: it is the borrower's answer to "how long do you need the
    loan?", it seeded this at intake, and a deal repriced to 7 months on a 6-month ask is a
    real thing rather than a row to reject.

    This block is the only place in the queue a team member can set one: the Run Analysis
    button posts no form of its own.
    """
    months = overrides.term_months
    for field, value in (("term_months", months), ("term_stub_days", None)):
        if getattr(deal, field) == value:
            continue
        before[field] = getattr(deal, field)
        after[field] = value
        setattr(deal, field, value)


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
