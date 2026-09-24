"""Status transitions the engine runs drive.  # SPEC §4.5

Two moves are automatic and everything else on the lifecycle is a team action, so the tests
are in two halves: the pure rules, and the same rules seen through ``run_screen`` /
``run_underwrite`` against a real database.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, Screen, Underwrite
from schema.models import Severity, Status, Verdict
from services import (
    DealNotReady,
    DealNotUnderwritable,
    UnderwriteRequest,
    latest_screen,
    run_screen,
    run_underwrite,
    screen_result,
    status_after_screen,
    status_after_underwrite,
)
from tests.conftest import TEAM_ENTRY_WITH_OVERRIDES, requires_db, store_deal

ACTOR = "tester@glenwood.example"
CONFIG = Config.load()
D = Decimal


def request(**overrides: Any) -> UnderwriteRequest:
    base: dict[str, Any] = {
        "monthly_rent": D("2400.00"),
        "holding_costs_pct_of_cost": D("0.03"),
    }
    base.update(overrides)
    return UnderwriteRequest(**base)


# --- the rules themselves ------------------------------------------------------------------------


def test_a_clean_screen_moves_a_new_deal_and_nothing_else() -> None:
    assert status_after_screen(Status.NEW, Verdict.GO) is Status.SCREENED
    assert status_after_screen(Status.NEW, Verdict.CONDITIONAL) is Status.SCREENED


@pytest.mark.parametrize(
    "current",
    [s for s in Status if s is not Status.NEW],
    ids=[s.value for s in Status if s is not Status.NEW],
)
def test_a_clean_rescreen_never_drags_a_deal_backwards(current: Status) -> None:
    """Re-screening a deal the team has moved on leaves its status alone.  # SPEC §4.6"""
    for verdict in (Verdict.GO, Verdict.CONDITIONAL):
        assert status_after_screen(current, verdict) is current


@pytest.mark.parametrize(
    "current",
    [Status.NEW, Status.SCREENED, Status.IN_REVIEW],
    ids=["new", "screened", "in_review"],
)
def test_a_decline_closes_a_deal_nobody_is_pricing_yet(current: Status) -> None:
    """A re-screen that turns up a Hard flag on a deal in review is worth acting on."""
    assert status_after_screen(current, Verdict.DECLINE) is Status.DECLINED


@pytest.mark.parametrize(
    "current",
    [Status.UNDERWRITING, Status.LOI_SENT, Status.HANDED_OFF, Status.DECLINED, Status.DEAD],
    ids=["underwriting", "loi_sent", "handed_off", "declined", "dead"],
)
def test_a_decline_leaves_a_deal_someone_is_working_alone(current: Status) -> None:
    """From UNDERWRITING on, the Hard flags are recorded and a person decides.  # SPEC §4.6"""
    assert status_after_screen(current, Verdict.DECLINE) is current


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
    result = run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    db_session.commit()
    assert result.verdict is Verdict.GO
    assert deal_with_overrides.status is Status.SCREENED


@requires_db
def test_screen_declines_the_deal_when_the_verdict_does(
    db_session: Session, declining_deal: Deal
) -> None:
    """The sale price it carries puts the commitment at 88.6% of it, past the 75% cap."""
    result = run_screen(db_session, declining_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()
    assert result.verdict is Verdict.DECLINE
    assert declining_deal.status is Status.DECLINED


@requires_db
def test_underwrite_moves_a_screened_deal_to_underwriting(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    assert deal_with_overrides.status is Status.SCREENED
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.UNDERWRITING


@requires_db
def test_underwrite_moves_an_in_review_deal_to_underwriting(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.status = Status.IN_REVIEW
    db_session.flush()
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
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
        run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
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
    db_session: Session, declining_deal: Deal
) -> None:
    """The two rules meet: a Decline closes the deal, and a closed deal is not priced."""
    run_screen(db_session, declining_deal.id, CONFIG, actor=ACTOR)
    assert declining_deal.status is Status.DECLINED
    with pytest.raises(DealNotUnderwritable):
        run_underwrite(
            db_session,
            declining_deal.id,
            request(estimated_sale_price=D("295000")),
            CONFIG,
            actor=ACTOR,
        )


@requires_db
def test_the_status_change_rides_the_caller_s_transaction(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """Services never commit: a rollback takes the status back with the row."""
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    assert deal_with_overrides.status is Status.SCREENED
    db_session.rollback()
    reloaded = db_session.get(Deal, deal_with_overrides.id)
    assert reloaded is not None
    assert reloaded.status is Status.NEW


# --- a Decline on a deal already in flight (SPEC §4.6) -------------------------------------------


@requires_db
@pytest.mark.parametrize(
    "current", [Status.SCREENED, Status.IN_REVIEW], ids=["screened", "in_review"]
)
def test_a_rescreen_that_declines_closes_a_deal_in_review(
    db_session: Session, declining_deal: Deal, current: Status
) -> None:
    declining_deal.status = current
    db_session.flush()
    result = run_screen(db_session, declining_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()
    assert result.verdict is Verdict.DECLINE
    assert declining_deal.status is Status.DECLINED


@requires_db
@pytest.mark.parametrize(
    "current",
    [Status.UNDERWRITING, Status.LOI_SENT, Status.HANDED_OFF],
    ids=["underwriting", "loi_sent", "handed_off"],
)
def test_a_decline_on_a_deal_being_worked_records_the_flags_and_leaves_the_status(
    db_session: Session, declining_deal: Deal, current: Status
) -> None:
    """A person owns the deal from here on; the Hard flags are theirs to read and act on."""
    declining_deal.status = current
    db_session.flush()
    result = run_screen(db_session, declining_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()

    assert result.verdict is Verdict.DECLINE
    assert declining_deal.status is current
    hard = [f for f in result.flags if f.severity is Severity.HARD]
    assert hard, "the decline had no hard flag to record"
    # recorded, not just returned: the row carries the same flags and verdict
    row = latest_screen(db_session, declining_deal.id)
    assert row is not None
    assert row.verdict is Verdict.DECLINE
    stored = screen_result(row)
    assert [f.code for f in stored.flags if f.severity is Severity.HARD] == [f.code for f in hard]


# --- the underwrite screens an unscreened deal first (SPEC §8) -----------------------------------


@requires_db
def test_an_underwrite_on_a_new_deal_screens_it_first_and_proceeds(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    assert deal_with_overrides.status is Status.NEW
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()

    # the screen really ran: its row is there, with its own verdict
    row = latest_screen(db_session, deal_with_overrides.id)
    assert row is not None and row.verdict is Verdict.GO
    assert result.term_months == 6
    assert deal_with_overrides.status is Status.UNDERWRITING


@requires_db
def test_an_underwrite_on_a_new_deal_stops_when_that_screen_declines(
    db_session: Session, declining_deal: Deal
) -> None:
    assert declining_deal.status is Status.NEW
    with pytest.raises(DealNotUnderwritable) as caught:
        run_underwrite(
            db_session,
            declining_deal.id,
            request(estimated_sale_price=D("295000.00")),
            CONFIG,
            actor=ACTOR,
        )
    assert "the screen it just ran declined it" in str(caught.value)
    assert caught.value.status is Status.DECLINED

    # the screen that declined it is recorded, and nothing was priced
    assert declining_deal.status is Status.DECLINED
    row = latest_screen(db_session, declining_deal.id)
    assert row is not None and row.verdict is Verdict.DECLINE
    assert (
        db_session.scalars(select(Underwrite).where(Underwrite.deal_id == declining_deal.id)).all()
        == []
    )


@requires_db
def test_an_underwrite_on_a_screened_deal_does_not_screen_it_again(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    before = db_session.scalar(
        select(func.count()).select_from(Screen).where(Screen.deal_id == deal_with_overrides.id)
    )
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()
    after = db_session.scalar(
        select(func.count()).select_from(Screen).where(Screen.deal_id == deal_with_overrides.id)
    )
    assert before == after == 1
    assert deal_with_overrides.status is Status.UNDERWRITING


@requires_db
def test_the_fixture_states_the_statuses_its_deal_ends_in(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The Go fixture reaches the two statuses SPEC §4.6 says it should."""
    fixture = json.loads(TEAM_ENTRY_WITH_OVERRIDES.read_text(encoding="utf-8"))
    assert fixture["expected"]["verdict"] == "GO"

    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    assert deal_with_overrides.status is Status.SCREENED

    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.UNDERWRITING


# --- an intake the team is still chasing (SPEC §4.1, §4.6) ---------------------------------------


@requires_db
def test_an_incomplete_intake_is_not_priced_and_names_what_is_missing(
    db_session: Session, team_entry_with_overrides: dict[str, Any]
) -> None:
    """A NEEDS_INFO deal raises DealNotReady carrying ``missing_fields``.  # SPEC §4.1

    The deal below keeps every value the engine itself needs, so nothing deeper in the
    assembly would have objected: without this check it would have been priced, and the
    ``underwrites`` row would have landed on a deal still sitting in NEEDS_INFO.
    """
    payload = dict(team_entry_with_overrides)
    payload.pop("credit_range")
    deal = store_deal(db_session, payload)
    assert deal.status is Status.NEEDS_INFO
    assert deal.missing_fields == ["borrower.credit_range"]

    with pytest.raises(DealNotReady) as caught:
        run_underwrite(db_session, deal.id, request(), CONFIG, actor=ACTOR)
    assert caught.value.missing == ["borrower.credit_range"]
    assert "borrower.credit_range" in str(caught.value)

    rows = db_session.scalars(select(Underwrite).where(Underwrite.deal_id == deal.id)).all()
    assert rows == []
    assert deal.status is Status.NEEDS_INFO


@requires_db
def test_completing_the_intake_lets_the_same_deal_be_priced(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The refusal is about the status, not the deal: complete, it prices."""
    run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert deal_with_overrides.status is Status.UNDERWRITING
