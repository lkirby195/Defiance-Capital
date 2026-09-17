"""Status transitions the engine runs drive.  # SPEC §4.5

Two moves are automatic and everything else on the lifecycle is a team action, so the tests
are in two halves: the pure rules, and the same rules seen through ``run_screen`` /
``run_underwrite`` against a real database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, Underwrite
from schema.models import Status, Verdict
from services import (
    DealNotUnderwritable,
    UnderwriteRequest,
    run_screen,
    run_underwrite,
    status_after_screen,
    status_after_underwrite,
)
from tests.conftest import requires_db

CONFIG = Config.load()
D = Decimal


def request(**overrides: Any) -> UnderwriteRequest:
    base: dict[str, Any] = {
        "market_rent_monthly": D("2400.00"),
        "annual_utilities_usd": D("840.00"),
    }
    base.update(overrides)
    return UnderwriteRequest(**base)


# --- the rules themselves ------------------------------------------------------------------------


def test_a_screen_moves_a_new_deal_and_nothing_else() -> None:
    assert status_after_screen(Status.NEW, Verdict.GO) is Status.SCREENED
    assert status_after_screen(Status.NEW, Verdict.CONDITIONAL) is Status.SCREENED
    assert status_after_screen(Status.NEW, Verdict.DECLINE) is Status.DECLINED


@pytest.mark.parametrize(
    "current",
    [s for s in Status if s is not Status.NEW],
    ids=[s.value for s in Status if s is not Status.NEW],
)
def test_a_rescreen_never_drags_a_deal_backwards(current: Status) -> None:
    """Re-screening a deal the team has moved on leaves its status alone.  # SPEC §4.5"""
    for verdict in Verdict:
        assert status_after_screen(current, verdict) is current


def test_an_underwrite_moves_a_screened_or_in_review_deal() -> None:
    assert status_after_underwrite(Status.SCREENED) is Status.UNDERWRITING
    assert status_after_underwrite(Status.IN_REVIEW) is Status.UNDERWRITING
    # already underwriting is a no-op, and a re-run late in the pipeline changes nothing
    assert status_after_underwrite(Status.UNDERWRITING) is Status.UNDERWRITING
    assert status_after_underwrite(Status.LOI_SENT) is Status.LOI_SENT
    assert status_after_underwrite(Status.NEW) is Status.NEW


# --- the rules through the services (SPEC §7, §8) ------------------------------------------------


@requires_db
def test_screen_moves_the_deal_to_screened(db_session: Session, deal_with_overrides: Deal) -> None:
    assert deal_with_overrides.status is Status.NEW
    result = run_screen(db_session, deal_with_overrides.id, CONFIG)
    db_session.commit()
    assert result.verdict is Verdict.GO
    assert deal_with_overrides.status is Status.SCREENED


@requires_db
def test_screen_declines_the_deal_when_the_verdict_does(
    db_session: Session, stored_deal: Deal
) -> None:
    """No valuation behind it, so its LTV is on the purchase price and lands over the cap."""
    result = run_screen(db_session, stored_deal.id, CONFIG)
    db_session.commit()
    assert result.verdict is Verdict.DECLINE
    assert stored_deal.status is Status.DECLINED


@requires_db
def test_underwrite_moves_a_screened_deal_to_underwriting(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG)
    assert deal_with_overrides.status is Status.SCREENED
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG)
    db_session.commit()
    assert deal_with_overrides.status is Status.UNDERWRITING


@requires_db
def test_underwrite_moves_an_in_review_deal_to_underwriting(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.status = Status.IN_REVIEW
    db_session.flush()
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG)
    db_session.commit()
    assert deal_with_overrides.status is Status.UNDERWRITING


@requires_db
@pytest.mark.parametrize("closed", [Status.DECLINED, Status.DEAD], ids=["declined", "dead"])
def test_a_closed_deal_is_refused_and_nothing_is_written(
    db_session: Session, deal_with_overrides: Deal, closed: Status
) -> None:
    deal_with_overrides.status = closed
    db_session.flush()
    with pytest.raises(DealNotUnderwritable) as caught:
        run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG)
    assert caught.value.status is closed
    assert closed.value in str(caught.value)
    assert "re-opens it" in str(caught.value)
    # refused before any work: no row, and the status is untouched
    rows = db_session.scalars(
        select(Underwrite).where(Underwrite.deal_id == deal_with_overrides.id)
    ).all()
    assert rows == []
    assert deal_with_overrides.status is closed


@requires_db
def test_a_declined_screen_then_blocks_the_underwrite(
    db_session: Session, stored_deal: Deal
) -> None:
    """The two rules meet: a Decline closes the deal, and a closed deal is not priced."""
    run_screen(db_session, stored_deal.id, CONFIG)
    assert stored_deal.status is Status.DECLINED
    with pytest.raises(DealNotUnderwritable):
        run_underwrite(
            db_session, stored_deal.id, request(as_is_value=D("250000"), arv=D("295000")), CONFIG
        )


@requires_db
def test_the_status_change_rides_the_caller_s_transaction(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """Services never commit: a rollback takes the status back with the row."""
    run_screen(db_session, deal_with_overrides.id, CONFIG)
    assert deal_with_overrides.status is Status.SCREENED
    db_session.rollback()
    reloaded = db_session.get(Deal, deal_with_overrides.id)
    assert reloaded is not None
    assert reloaded.status is Status.NEW
