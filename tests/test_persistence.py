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
from schema.models import AssetType, ExitSource, StatedExit, TermBucket, Verdict
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
    assert row.inputs["deal"]["product"] == "SPLIT_DRAW"


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
    assert rebuilt.exit == result.exit
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


def test_underwrite_uses_the_deals_asset_type_and_stated_exit(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    assert deal_with_overrides.asset_type is AssetType.SFR
    assert deal_with_overrides.stated_exit is None
    inferred = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    # 6 months on an SFR resells, and the product is WHOLETAIL, so the exit is too (SPEC §3)
    assert inferred.exit.type is StatedExit.WHOLETAIL
    assert inferred.exit.exit_source is ExitSource.INFERRED

    longer = run_underwrite(
        db_session, deal_with_overrides.id, request(term_months=12), CONFIG, actor=ACTOR
    )
    assert longer.exit.type is StatedExit.HOLD  # 12 months infers a hold instead
    assert longer.exit.exit_source is ExitSource.INFERRED

    deal_with_overrides.stated_exit = StatedExit.HOLD
    db_session.flush()
    stated = run_underwrite(db_session, deal_with_overrides.id, request(), CONFIG, actor=ACTOR)
    assert stated.exit.type is StatedExit.HOLD  # a stated exit wins over the term
    assert stated.exit.exit_source is ExitSource.STATED
    db_session.commit()


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
    """A 12+ bucket names no months, so a deal on one carries a term or is refused."""
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
