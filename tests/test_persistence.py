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
from schema.models import AssetType, ExitSource, StatedExit, Verdict
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

CONFIG = Config.load()
D = Decimal
MISSING_UUID = UUID("00000000-0000-0000-0000-000000000000")


def request(**overrides: Any) -> UnderwriteRequest:
    base: dict[str, Any] = {
        "as_is_value": D("230000.00"),
        "arv": D("260000.00"),
        "verified_credit_score": 715,
        "verified_deals_36mo": 4,
        "market_rent_monthly": D("1800.00"),
        "annual_utilities_usd": D("720.00"),
    }
    base.update(overrides)
    return UnderwriteRequest(**base)


def test_run_screen_appends_a_row_and_returns_the_result(
    db_session: Session, stored_deal: Deal
) -> None:
    result = run_screen(db_session, stored_deal.id, CONFIG)
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
    result = run_screen(db_session, stored_deal.id, CONFIG)
    db_session.commit()
    db_session.expire_all()

    row = latest_screen(db_session, stored_deal.id)
    assert row is not None
    rebuilt = screen_result(row)
    assert rebuilt == result
    assert isinstance(rebuilt.sizing.commitment, Decimal)
    assert rebuilt.sizing.total_cost == result.sizing.total_cost


def test_screens_are_append_only_and_ordered(db_session: Session, stored_deal: Deal) -> None:
    first = run_screen(db_session, stored_deal.id, CONFIG)
    second = run_screen(db_session, stored_deal.id, CONFIG)
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
    """Enrichment is Phase 3, so a screen today runs with no as-is value and no ARV."""
    result = run_screen(db_session, stored_deal.id, CONFIG)
    codes = {flag.code.value for flag in result.flags}
    assert {"AS_IS_VALUE_MISSING", "ARV_MISSING", "COURT_RECORDS_NOT_CHECKED"} <= codes
    assert result.verdict is not Verdict.GO


def test_run_underwrite_appends_a_row_and_round_trips(
    db_session: Session, stored_deal: Deal
) -> None:
    result = run_underwrite(db_session, stored_deal.id, request(), CONFIG)
    db_session.commit()
    db_session.expire_all()

    row = latest_underwrite(db_session, stored_deal.id)
    assert row is not None
    assert row.engine_version == ENGINE_VERSION
    assert row.config_hash == CONFIG.config_hash
    rebuilt = underwrite_result(row)
    assert rebuilt == result
    assert row.grid_lender["rates"]
    assert rebuilt.grid_lender == result.grid_lender
    # solved_rate is a NUMERIC(7,5) copy for querying; the JSONB keeps full precision
    assert row.solved_rate == result.solved_rate.quantize(D("0.00001"))
    assert rebuilt.solved_rate == result.solved_rate
    assert rebuilt.solved_rate != row.solved_rate  # the column really is the lossy one


def test_underwrite_row_drops_nothing_the_result_carried(
    db_session: Session, stored_deal: Deal
) -> None:
    result = run_underwrite(db_session, stored_deal.id, request(), CONFIG)
    db_session.commit()
    row = latest_underwrite(db_session, stored_deal.id)
    assert row is not None
    rebuilt = underwrite_result(row)
    assert rebuilt.borrower_at_solve == result.borrower_at_solve
    assert rebuilt.exit == result.exit
    assert rebuilt.downside == result.downside
    assert rebuilt.flags == result.flags
    assert rebuilt.sizing == result.sizing
    assert row.inputs["term_months"] == result.term_months


def test_underwrite_takes_the_term_from_the_bucket_unless_told_otherwise(
    db_session: Session, stored_deal: Deal
) -> None:
    from_bucket = run_underwrite(db_session, stored_deal.id, request(), CONFIG)
    assert from_bucket.term_months == 9  # the deal's term_bucket is "9"
    stated = run_underwrite(db_session, stored_deal.id, request(term_months=18), CONFIG)
    assert stated.term_months == 18
    db_session.commit()


def test_underwrite_falls_back_to_the_deals_own_opex_actuals(
    db_session: Session, stored_deal: Deal
) -> None:
    stored_deal.actual_annual_taxes_usd = D("2650.00")
    db_session.flush()
    result = run_underwrite(db_session, stored_deal.id, request(), CONFIG)
    assert result.exit.annual_taxes == D("2650.00")
    assert result.exit.annual_taxes_source.value == "ACTUAL"
    # nothing supplied for insurance, so the config default on the as-is value stands
    assert result.exit.annual_insurance_source.value == "DEFAULT"
    assert result.exit.annual_insurance == D("230000.00") * D("0.005")
    db_session.commit()


def test_underwrite_uses_the_deals_asset_type_and_stated_exit(
    db_session: Session, stored_deal: Deal
) -> None:
    assert stored_deal.asset_type is AssetType.SFR
    stated = run_underwrite(db_session, stored_deal.id, request(), CONFIG)
    assert stated.exit.type is StatedExit.FLIP  # the team stated it at intake
    assert stated.exit.exit_source is ExitSource.STATED

    stored_deal.stated_exit = None
    db_session.flush()
    inferred = run_underwrite(db_session, stored_deal.id, request(term_months=12), CONFIG)
    assert inferred.exit.type is StatedExit.HOLD  # 12 months infers a hold (SPEC §3)
    assert inferred.exit.exit_source is ExitSource.INFERRED
    db_session.commit()


def test_missing_deal_and_incomplete_deal_are_named_not_guessed(
    db_session: Session, stored_deal: Deal
) -> None:
    with pytest.raises(DealNotFound):
        run_screen(db_session, MISSING_UUID, CONFIG)

    stored_deal.loan_requested = None
    stored_deal.credit_range_self_reported = None
    db_session.flush()
    with pytest.raises(DealNotReady) as caught:
        run_screen(db_session, stored_deal.id, CONFIG)
    assert caught.value.missing == ["deal.loan_requested", "borrower.credit_range"]


def test_a_deal_with_no_term_bucket_cannot_be_underwritten_silently(
    db_session: Session, stored_deal: Deal
) -> None:
    stored_deal.term_bucket = None
    db_session.flush()
    with pytest.raises(DealNotReady, match="term_months"):
        run_underwrite(db_session, stored_deal.id, request(), CONFIG)
