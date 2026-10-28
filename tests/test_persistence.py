"""screens and underwrites round-trip: what the engine produced is what comes back.  # SPEC §5

The point of storing the whole result in JSONB is that a row rebuilds the exact
``ScreenResult`` / ``UnderwriteResult`` - every Decimal to the last place - so a memo
generated months later reads the numbers the team actually decided on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, Screen
from engine.version import ENGINE_VERSION
from schema.models import (
    AnalysisStatus,
    LegacyRentalAnalysis,
    LegacyTakeBackAnalysis,
    Product,
    RentalAnalysis,
    TermBucket,
    UnderwriteFlag,
    UnderwriteResult,
    Verdict,
)
from services import (
    DealNotFound,
    DealNotReady,
    UnderwriteRequest,
    latest_screen,
    latest_underwrite,
    run_screen,
    run_underwrite,
    screen_result,
    underwrite_result,
)
from tests.conftest import requires_db

pytestmark = requires_db

ACTOR = "tester@glenwood.example"
CONFIG = Config.load()
D = Decimal
MISSING_UUID = UUID("00000000-0000-0000-0000-000000000000")


def request(**overrides: Any) -> UnderwriteRequest:
    base: dict[str, Any] = {
        "estimated_sale_price": D("260000.00"),
        "verified_credit_score": 715,
        "verified_deals_36mo": 4,
        "monthly_rent": D("1800.00"),
        "holding_costs_pct_of_cost": D("0.03"),
    }
    base.update(overrides)
    return UnderwriteRequest(**base)


def test_run_screen_appends_a_row_and_returns_the_result(
    db_session: Session, stored_deal: Deal
) -> None:
    result = run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()

    row = db_session.scalar(select(Screen).where(Screen.deal_id == stored_deal.id))
    assert row is not None
    assert row.engine_version == ENGINE_VERSION == result.engine_version
    assert row.config_hash == CONFIG.config_hash == result.config_hash
    assert row.verdict is result.verdict
    assert row.reasons == result.reasons
    assert row.suggested_reply == result.suggested_reply
    assert row.inputs["deal"]["product"] == "ACQUISITION_REHAB"


def test_screen_row_rebuilds_the_exact_result(db_session: Session, stored_deal: Deal) -> None:
    result = run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()
    db_session.expire_all()

    row = latest_screen(db_session, stored_deal.id)
    assert row is not None
    rebuilt = screen_result(row)
    assert rebuilt == result
    assert isinstance(rebuilt.sizing.commitment, Decimal)
    assert rebuilt.sizing.total_cost == result.sizing.total_cost


def test_screens_are_append_only_and_ordered(db_session: Session, stored_deal: Deal) -> None:
    first = run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    second = run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()

    count = db_session.scalar(
        select(func.count()).select_from(Screen).where(Screen.deal_id == stored_deal.id)
    )
    assert count == 2
    assert first == second  # same deal, same engine, same config
    rows = list(
        db_session.scalars(
            select(Screen).where(Screen.deal_id == stored_deal.id).order_by(Screen.created_at)
        )
    )
    # clock_timestamp(), not now(): two runs inside one transaction still order (SPEC §5)
    assert rows[0].created_at < rows[1].created_at
    latest = latest_screen(db_session, stored_deal.id)
    assert latest is not None and latest.id == rows[1].id


def test_screen_without_a_valuation_flags_rather_than_guesses(
    db_session: Session, stored_deal: Deal
) -> None:
    """Enrichment is Phase 3, so a screen today runs with no valuation at all."""
    result = run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    codes = {flag.code.value for flag in result.flags}
    assert {"ESTIMATED_SALE_PRICE_MISSING", "COURT_RECORDS_NOT_CHECKED"} <= codes
    assert result.verdict is Verdict.CONDITIONAL


def test_run_underwrite_appends_a_row_and_round_trips(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()
    db_session.expire_all()

    row = latest_underwrite(db_session, deal_with_overrides.id)
    assert row is not None
    assert row.engine_version == ENGINE_VERSION
    assert row.config_hash == CONFIG.config_hash
    rebuilt = underwrite_result(row)
    assert rebuilt == result
    assert row.outputs["return_overview"]["entries"]
    assert rebuilt.return_overview == result.return_overview
    # irr is a NUMERIC(7,5) copy for querying; the JSONB keeps full precision
    irr = result.return_overview.irr
    assert irr is not None
    assert row.irr == irr.quantize(D("0.00001"))
    assert rebuilt.return_overview.irr == irr
    assert rebuilt.return_overview.irr != row.irr  # the column really is the lossy one


def test_underwrite_row_drops_nothing_the_result_carried(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    db_session.commit()
    row = latest_underwrite(db_session, deal_with_overrides.id)
    assert row is not None
    rebuilt = underwrite_result(row)
    assert rebuilt.economics == result.economics
    assert rebuilt.return_overview == result.return_overview
    assert rebuilt.analyses == result.analyses
    assert rebuilt.flip == result.flip
    assert rebuilt.rental == result.rental
    assert rebuilt.take_back == result.take_back
    assert rebuilt.flags == result.flags
    assert rebuilt.sizing == result.sizing
    assert row.inputs["term_months"] == result.term_months


def test_underwrite_takes_the_term_from_the_deal_unless_told_otherwise(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The column, seeded from the bucket at intake; a request still outranks it."""
    assert deal_with_overrides.term_months == 6  # the term on the deal, off the form
    from_deal = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert from_deal.term_months == 6
    stated = run_underwrite(
        db_session, deal_with_overrides.id, request(term_months=18), CONFIG, actor=ACTOR
    )
    assert stated.term_months == 18
    assert deal_with_overrides.term_months == 6, "a run does not write back to the deal"
    db_session.commit()


def test_underwrite_falls_back_to_the_deals_own_economics(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A request beats the deal; the deal beats config; config is the last word (SPEC §8.1)."""
    deal_with_overrides.origination_fee_pct = D("0.03000")
    db_session.flush()
    from_deal = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert from_deal.economics.origination_fee_pct == D("0.03000")
    # the holding cost comes off the request here, and the contingency off config:
    # 3% of the 155,000 price plus rehab is 4,650 over the hold (SPEC §8.1)
    assert from_deal.economics.holding_costs_pct_of_cost == D("0.03")
    assert from_deal.economics.holding_costs_basis == D("155000.00")
    assert from_deal.economics.holding_costs_total == D("4650.00")
    assert from_deal.economics.contingency_pct == CONFIG.fees.contingency_default_pct
    db_session.commit()


def test_underwrite_runs_all_three_analyses_with_no_toggles(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A sale price and a rent on the deal evaluate all three (SPEC §8.1, engine 2.0.0)."""
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert result.analyses is None
    assert result.flip.status is AnalysisStatus.EVALUATED
    assert result.rental.status is AnalysisStatus.EVALUATED
    assert result.sources_and_uses is not None
    db_session.commit()


def test_a_stored_result_from_before_2_0_0_still_loads(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The toggles, an OFF flip, ``profit_yield`` and a retired product on a stored row all
    read back (SPEC §8.7): a version written before engine 2.0.0 is still on the page."""
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    stored = result.model_dump(mode="json")
    stored["engine_version"] = "1.11.0"
    stored["analyses"] = {
        "flip_analysis": False,
        "rental_analysis": True,
        "flip_analysis_default": True,
        "rental_analysis_default": True,
    }
    stored["sizing"]["product"] = "WHOLETAIL"
    del stored["sources_and_uses"]
    # the two DSCR tests a run before engine 2.1.0 recorded (SPEC §8.5, §8.6 then)
    stored["rental"] = {
        "status": "EVALUATED",
        "expenses_pct": "0.35",
        "holding_costs_monthly": "516.67",
        "loan_amount": "120000.00",
        "takeout_rate": "0.065",
        "amortization_years": 30,
        "debt_service_monthly": "758.48",
        "dscr_floor": "1.20",
        "monthly_rent": "1800.00",
        "expenses": "630.00",
        "net_monthly_income": "653.33",
        "dscr": "0.861",
        "passed": False,
    }
    stored["take_back"] = {
        "status": "EVALUATED",
        "loan_amount": "120000.00",
        "interest_rate": "0.13",
        "lost_interest_months": 3,
        "lost_interest": "3900.00",
        "legal_costs": "5000.00",
        "total_cost": "128900.00",
        "amortization_years": 30,
        "debt_service_monthly": "1426.03",
        "dscr_floor": "1.00",
        "net_monthly_income": "653.33",
        "dscr": "0.458",
        "passed": False,
    }
    stored["flags"].append(
        {"code": "DSCR_BELOW_FLOOR", "severity": "SOFT", "message": "Rental DSCR 0.86x ..."}
    )
    flip = stored["flip"]
    for name in ("interest", "origination", "loan_amount", "borrower_equity", "cash_on_cash"):
        del flip[name]
    del flip["cash_on_cash_target"], flip["cash_on_cash_passed"], flip["project_yield"]
    flip.update(
        status="OFF",
        estimated_sale_price=None,
        broker_costs=None,
        net_profit=None,
        profit_yield=None,
    )
    old = UnderwriteResult.model_validate(stored)
    assert isinstance(old.rental, LegacyRentalAnalysis) and old.rental.dscr == D("0.861")
    assert isinstance(old.take_back, LegacyTakeBackAnalysis)
    assert old.take_back.total_cost == D("128900.00")
    assert old.flags[-1].code is UnderwriteFlag.DSCR_BELOW_FLOOR
    assert isinstance(result.rental, RentalAnalysis), "a 2.1.0 run reads as itself"
    assert old.analyses is not None and old.analyses.flip_analysis is False
    assert old.flip.status is AnalysisStatus.OFF and old.flip.project_yield is None
    assert old.sizing.product is Product.ACQUISITION
    assert old.sources_and_uses is None
    db_session.rollback()


def test_missing_deal_and_incomplete_deal_are_named_not_guessed(
    db_session: Session, stored_deal: Deal
) -> None:
    with pytest.raises(DealNotFound):
        run_screen(db_session, MISSING_UUID, CONFIG, actor=ACTOR)

    stored_deal.loan_requested = None
    stored_deal.credit_range_self_reported = None
    db_session.flush()
    with pytest.raises(DealNotReady) as caught:
        run_screen(db_session, stored_deal.id, CONFIG, actor=ACTOR)
    assert caught.value.missing == ["deal.loan_requested", "borrower.credit_range"]


def test_a_deal_with_no_term_cannot_be_underwritten_silently(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A deal with no term is refused, whatever bucket it carries (SPEC §8.1)."""
    deal_with_overrides.term_months = None
    deal_with_overrides.term_bucket = TermBucket.M12_PLUS
    db_session.flush()
    with pytest.raises(DealNotReady, match="deal.term_months"):
        run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)


def test_a_twelve_plus_deal_with_a_term_on_it_underwrites(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The other half: a number on the deal is all a 12+ bucket needs.  # SPEC §8.1"""
    deal_with_overrides.term_bucket = TermBucket.M12_PLUS
    deal_with_overrides.term_months = 18
    db_session.flush()
    result = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert result.term_months == 18
    db_session.commit()
