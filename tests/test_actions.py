"""The review-queue team actions and the audit row each one writes.  # SPEC §4.6, §5, §11

Two halves. The service half exercises every action, every status it runs from and every
status it refuses, and asserts the ``audit_log`` row - because an action that moved a deal
and recorded nothing is the failure this table exists to prevent. The route half posts the
same actions as a browser does and checks the actor that lands on the row is the person who
was signed in, not a constant.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import AuditLog, Deal, User
from schema.models import (
    AuditAction,
    CourtRecordsStatus,
    Product,
    ProductSource,
    Status,
    TeamCourtRecord,
)
from services import (
    DEALS,
    ActionNotAllowed,
    ReasonRequired,
    TeamOverrides,
    add_note,
    advance_to_review,
    deal_trail,
    decline,
    mark_dead,
    reopen,
    reopen_status,
    run_screen,
    save_overrides,
)
from services.actions import OVERRIDE_FIELDS
from tests.conftest import ACTOR, requires_db

pytestmark = requires_db

CONFIG = Config.load()


def rows(session: Session, deal: Deal, action: AuditAction | None = None) -> list[AuditLog]:
    statement = select(AuditLog).where(
        AuditLog.table_name == DEALS, AuditLog.row_id == str(deal.id)
    )
    if action is not None:
        statement = statement.where(AuditLog.action == action.value)
    return list(session.scalars(statement.order_by(AuditLog.created_at, AuditLog.id)))


def at(session: Session, deal: Deal, status: Status) -> Deal:
    deal.status = status
    session.flush()
    return deal


# --- advance to review ---------------------------------------------------------------------------


def test_advancing_a_screened_deal_moves_it_and_records_who(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    advance_to_review(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()

    assert deal_with_overrides.status is Status.IN_REVIEW
    recorded = rows(db_session, deal_with_overrides, AuditAction.ADVANCED_TO_REVIEW)
    assert len(recorded) == 1
    assert recorded[0].actor == ACTOR
    assert recorded[0].before == {"status": "SCREENED"}
    assert recorded[0].after == {"status": "IN_REVIEW"}


@pytest.mark.parametrize(
    "current", [Status.NEW, Status.NEEDS_INFO, Status.IN_REVIEW, Status.DECLINED, Status.DEAD]
)
def test_advancing_from_anywhere_else_is_refused_and_writes_nothing(
    db_session: Session, deal_with_overrides: Deal, current: Status
) -> None:
    at(db_session, deal_with_overrides, current)
    with pytest.raises(ActionNotAllowed) as caught:
        advance_to_review(db_session, deal_with_overrides.id, actor=ACTOR)
    assert current.value in str(caught.value)
    assert "SCREENED" in str(caught.value)
    assert deal_with_overrides.status is current
    assert rows(db_session, deal_with_overrides, AuditAction.ADVANCED_TO_REVIEW) == []


# --- decline -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current",
    [Status.NEW, Status.SCREENED, Status.IN_REVIEW, Status.UNDERWRITING, Status.LOI_SENT],
)
def test_a_person_can_decline_a_deal_at_any_live_stage(
    db_session: Session, deal_with_overrides: Deal, current: Status
) -> None:
    """The automatic Decline stops at UNDERWRITING; a person declining it is that person."""
    at(db_session, deal_with_overrides, current)
    decline(db_session, deal_with_overrides.id, actor=ACTOR, reason="Borrower withdrew the entity")
    db_session.commit()

    assert deal_with_overrides.status is Status.DECLINED
    recorded = rows(db_session, deal_with_overrides, AuditAction.DECLINED)
    assert len(recorded) == 1
    assert recorded[0].before == {"status": current.value}
    assert recorded[0].after == {
        "status": "DECLINED",
        "reason": "Borrower withdrew the entity",
    }


def test_a_decline_needs_a_reason(db_session: Session, deal_with_overrides: Deal) -> None:
    for blank in ("", "   "):
        with pytest.raises(ReasonRequired):
            decline(db_session, deal_with_overrides.id, actor=ACTOR, reason=blank)
    assert deal_with_overrides.status is not Status.DECLINED
    assert rows(db_session, deal_with_overrides, AuditAction.DECLINED) == []


@pytest.mark.parametrize("closed", [Status.DECLINED, Status.DEAD])
def test_a_closed_deal_is_not_declined_again(
    db_session: Session, deal_with_overrides: Deal, closed: Status
) -> None:
    at(db_session, deal_with_overrides, closed)
    with pytest.raises(ActionNotAllowed):
        decline(db_session, deal_with_overrides.id, actor=ACTOR, reason="again")


# --- mark dead -----------------------------------------------------------------------------------


def test_marking_dead_takes_a_reason_but_does_not_demand_one(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A decline is GLENWOOD's judgement and must be explainable; dead usually is not."""
    at(db_session, deal_with_overrides, Status.IN_REVIEW)
    mark_dead(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.DEAD
    recorded = rows(db_session, deal_with_overrides, AuditAction.MARKED_DEAD)
    assert recorded[0].after == {"status": "DEAD"}


def test_a_declined_deal_can_still_be_marked_dead(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.DECLINED)
    mark_dead(db_session, deal_with_overrides.id, actor=ACTOR, reason="Sold to someone else")
    db_session.commit()
    assert deal_with_overrides.status is Status.DEAD
    assert rows(db_session, deal_with_overrides, AuditAction.MARKED_DEAD)[0].after == {
        "status": "DEAD",
        "reason": "Sold to someone else",
    }


def test_a_dead_deal_is_final(db_session: Session, deal_with_overrides: Deal) -> None:
    at(db_session, deal_with_overrides, Status.DEAD)
    with pytest.raises(ActionNotAllowed):
        mark_dead(db_session, deal_with_overrides.id, actor=ACTOR)
    with pytest.raises(ActionNotAllowed):
        reopen(db_session, deal_with_overrides.id, actor=ACTOR, reason="changed our minds")


# --- reopen --------------------------------------------------------------------------------------


def test_reopening_a_screened_deal_puts_it_back_at_screened(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    decline(db_session, deal_with_overrides.id, actor=ACTOR, reason="leverage")
    db_session.commit()
    assert deal_with_overrides.status is Status.DECLINED

    reopen(db_session, deal_with_overrides.id, actor=ACTOR, reason="New ARV from the appraiser")
    db_session.commit()
    assert deal_with_overrides.status is Status.SCREENED
    recorded = rows(db_session, deal_with_overrides, AuditAction.REOPENED)
    assert recorded[0].before == {"status": "DECLINED"}
    assert recorded[0].after == {"status": "SCREENED", "reason": "New ARV from the appraiser"}


def test_reopening_a_deal_that_was_never_screened_puts_it_back_at_new(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """Declined by hand before Stage 1 ever ran, so there is no screen to return to."""
    decline(db_session, deal_with_overrides.id, actor=ACTOR, reason="wrong state")
    db_session.commit()
    assert reopen_status(db_session, deal_with_overrides) is Status.NEW

    reopen(db_session, deal_with_overrides.id, actor=ACTOR, reason="it is in Tulsa after all")
    db_session.commit()
    assert deal_with_overrides.status is Status.NEW


def test_a_reopen_needs_a_reason(db_session: Session, deal_with_overrides: Deal) -> None:
    """Undoing the thing that stops a deal being priced is the write an auditor asks about."""
    at(db_session, deal_with_overrides, Status.DECLINED)
    for blank in ("", "  \n "):
        with pytest.raises(ReasonRequired) as caught:
            reopen(db_session, deal_with_overrides.id, actor=ACTOR, reason=blank)
        assert "records a reason" in str(caught.value)
    assert deal_with_overrides.status is Status.DECLINED
    assert rows(db_session, deal_with_overrides, AuditAction.REOPENED) == []


@pytest.mark.parametrize("current", [Status.NEW, Status.SCREENED, Status.UNDERWRITING])
def test_only_a_declined_deal_is_reopened(
    db_session: Session, deal_with_overrides: Deal, current: Status
) -> None:
    at(db_session, deal_with_overrides, current)
    with pytest.raises(ActionNotAllowed) as caught:
        reopen(db_session, deal_with_overrides.id, actor=ACTOR, reason="why not")
    assert "DECLINED" in str(caught.value)


# --- notes ---------------------------------------------------------------------------------------


def test_a_note_is_recorded_against_the_deal_and_changes_nothing_else(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    before = deal_with_overrides.status
    add_note(db_session, deal_with_overrides.id, actor=ACTOR, note="  Called; roof is newer  ")
    db_session.commit()
    assert deal_with_overrides.status is before
    recorded = rows(db_session, deal_with_overrides, AuditAction.NOTE_ADDED)
    assert len(recorded) == 1
    assert recorded[0].after == {"note": "Called; roof is newer"}
    assert recorded[0].before is None


def test_an_empty_note_is_refused(db_session: Session, deal_with_overrides: Deal) -> None:
    with pytest.raises(ReasonRequired):
        add_note(db_session, deal_with_overrides.id, actor=ACTOR, note="   ")
    assert rows(db_session, deal_with_overrides, AuditAction.NOTE_ADDED) == []


# --- team overrides ------------------------------------------------------------------------------


def current_block(deal: Deal, **changes: object) -> TeamOverrides:
    """The deal's override block exactly as the page renders it, with any changes applied.

    Keyed off ``OVERRIDE_FIELDS`` so a field added to the block is carried here too: a save
    is a replacement, and a test that quietly drops a field would be testing a clear rather
    than an edit.
    """
    values: dict[str, object] = {}
    for field in OVERRIDE_FIELDS:
        value = getattr(deal, field)
        if field == "court_records_team":
            value = [TeamCourtRecord.model_validate(matter) for matter in value]
        values[field] = value
    values["term_months"] = deal.term_months
    return TeamOverrides(**{**values, **changes})  # type: ignore[arg-type]


def test_saving_overrides_replaces_the_block_and_records_only_what_moved(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    before_sale_price = deal_with_overrides.estimated_sale_price_team
    save_overrides(
        db_session,
        deal_with_overrides.id,
        current_block(
            deal_with_overrides,
            monthly_rent=Decimal("2600.00"),
            holding_costs_pct_of_cost=Decimal("0.03"),
        ),
        actor=ACTOR,
    )
    db_session.commit()

    assert deal_with_overrides.monthly_rent == Decimal("2600.00")
    assert deal_with_overrides.estimated_sale_price_team == before_sale_price
    recorded = rows(db_session, deal_with_overrides, AuditAction.OVERRIDES_SAVED)
    assert len(recorded) == 1
    assert set(recorded[0].after or {}) == {"monthly_rent", "holding_costs_pct_of_cost"}
    assert "estimated_sale_price_team" not in (recorded[0].after or {})


def test_a_blank_clears_a_value(db_session: Session, deal_with_overrides: Deal) -> None:
    """The form is rendered with the current values in it, so a blank is a deliberate empty."""
    assert deal_with_overrides.estimated_sale_price_team is not None
    save_overrides(db_session, deal_with_overrides.id, TeamOverrides(), actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.estimated_sale_price_team is None
    assert deal_with_overrides.court_records_status is None
    assert deal_with_overrides.court_records_team == []


def test_a_save_that_changes_nothing_records_nothing(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    current = current_block(deal_with_overrides)
    save_overrides(db_session, deal_with_overrides.id, current, actor=ACTOR)
    db_session.commit()
    assert rows(db_session, deal_with_overrides, AuditAction.OVERRIDES_SAVED) == []


def test_resaving_the_same_product_does_not_relabel_an_inferred_one(
    db_session: Session, stored_deal: Deal
) -> None:
    """``product_source`` says who decided; a round trip through the form must not lie.

    ``stored_deal`` is the complete team entry, whose product the normalizer inferred from
    the rehab costs - which is the state this rule exists to protect.
    """
    assert stored_deal.product_source is ProductSource.INFERRED
    inferred = stored_deal.product
    save_overrides(db_session, stored_deal.id, TeamOverrides(product=inferred), actor=ACTOR)
    db_session.commit()
    assert stored_deal.product_source is ProductSource.INFERRED

    save_overrides(
        db_session, stored_deal.id, TeamOverrides(product=Product.WHOLETAIL), actor=ACTOR
    )
    db_session.commit()
    assert stored_deal.product is Product.WHOLETAIL
    assert stored_deal.product_source is ProductSource.ENTERED


def test_a_blank_product_leaves_the_inferred_one_alone(
    db_session: Session, stored_deal: Deal
) -> None:
    inferred = stored_deal.product
    save_overrides(db_session, stored_deal.id, TeamOverrides(), actor=ACTOR)
    db_session.commit()
    assert stored_deal.product is inferred
    assert stored_deal.product_source is ProductSource.INFERRED


def test_an_incoherent_court_block_is_refused_at_the_model() -> None:
    """The same check ``DealInfo`` makes, so the queue answers with a message, not a 500."""
    with pytest.raises(ValueError, match="FLAGS needs at least one matter"):
        TeamOverrides(
            court_records_status=CourtRecordsStatus.FLAGS,
            court_records_as_of=date(2026, 9, 16),
        )


# --- through the routes ---------------------------------------------------------------------------


def post(client: TestClient, deal: Deal, action: str, **data: Any) -> Any:
    return client.post(f"/queue/deals/{deal.id}/{action}", data=data, follow_redirects=False)


def test_each_action_posts_and_records_the_signed_in_person(
    client: TestClient, db_session: Session, deal_with_overrides: Deal, queue_user: User
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    db_session.commit()

    assert post(client, deal_with_overrides, "advance").status_code == 303
    assert post(client, deal_with_overrides, "note", note="spoke to the broker").status_code == 303
    assert post(client, deal_with_overrides, "decline", reason="leverage").status_code == 303
    assert post(client, deal_with_overrides, "reopen", reason="new ARV").status_code == 303
    assert post(client, deal_with_overrides, "dead", reason="went quiet").status_code == 303

    db_session.expire_all()
    trail = deal_trail(db_session, deal_with_overrides.id)
    actions = [row.action for row in trail]
    assert actions[:5] == [
        AuditAction.MARKED_DEAD.value,
        AuditAction.REOPENED.value,
        AuditAction.DECLINED.value,
        AuditAction.NOTE_ADDED.value,
        AuditAction.ADVANCED_TO_REVIEW.value,
    ]
    assert {row.actor for row in trail[:5]} == {queue_user.email}
    assert db_session.get(Deal, deal_with_overrides.id).status is Status.DEAD  # type: ignore[union-attr]


def test_an_action_the_status_rules_out_comes_back_as_the_page_saying_why(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.NEW)
    db_session.commit()
    response = post(client, deal_with_overrides, "advance")
    assert response.status_code == 409
    assert "cannot be advanced to review" in response.text
    assert "SCREENED" in response.text


def test_a_reopen_without_a_reason_comes_back_as_the_page_saying_so(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.DECLINED)
    db_session.commit()
    response = post(client, deal_with_overrides, "reopen", reason="   ")
    assert response.status_code == 409
    assert "records a reason" in response.text

    db_session.expire_all()
    assert db_session.get(Deal, deal_with_overrides.id).status is Status.DECLINED  # type: ignore[union-attr]
    assert rows(db_session, deal_with_overrides, AuditAction.REOPENED) == []


def test_the_override_form_posts_and_saves(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/overrides",
        data={
            "estimated_sale_price_team": "$295,000",
            "monthly_rent": "2,400",
            "holding_costs_pct_of_cost": "1.5%",
            "court_records_status": "CLEAN",
            "court_records_as_of": "2026-09-16",
            "matter_code": ["", "", ""],
            "matter_occurred_on": ["", "", ""],
            "matter_amount_usd": ["", "", ""],
            "matter_lien_kind": ["", "", ""],
            "matter_senior": ["", "", ""],
            "matter_resolved_at_close": ["", "", ""],
            "matter_description": ["", "", ""],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None
    # the masks come off at the boundary (api/masks.py): the deal carries the numbers
    assert deal.estimated_sale_price_team == Decimal("295000")
    assert deal.monthly_rent == Decimal("2400")
    assert deal.holding_costs_pct_of_cost == Decimal("0.015")
    assert deal.court_records_status is CourtRecordsStatus.CLEAN
    assert deal.court_records_team == []


def test_a_court_matter_typed_into_the_form_reaches_the_deal(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/overrides",
        data={
            "court_records_status": "FLAGS",
            "court_records_as_of": "2026-09-16",
            "matter_code": ["OPEN_TAX_LIEN", "SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS", ""],
            "matter_occurred_on": ["", "", ""],
            "matter_amount_usd": ["3200.00", "", ""],
            "matter_lien_kind": ["", "LIEN", ""],
            "matter_senior": ["", "true", ""],
            "matter_resolved_at_close": ["", "false", ""],
            "matter_description": ["", "second position behind us", ""],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None
    assert [matter["code"] for matter in deal.court_records_team] == [
        "OPEN_TAX_LIEN",
        "SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS",
    ]
    lien = deal.court_records_team[1]
    assert lien["senior"] is True and lien["resolved_at_close"] is False


def test_a_bad_override_comes_back_with_the_values_still_in_the_form(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/overrides",
        data={
            "monthly_rent": "$2,400",
            "estimated_sale_price_team": "not a number",
            "matter_code": [""],
            "matter_occurred_on": [""],
            "matter_amount_usd": [""],
            "matter_lien_kind": [""],
            "matter_senior": [""],
            "matter_resolved_at_close": [""],
            "matter_description": [""],
        },
    )
    assert response.status_code == 422
    assert "estimated_sale_price_team" in response.text
    assert 'value="$2,400"' in response.text  # re-masked, as the box had it
