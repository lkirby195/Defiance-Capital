"""Team-supplied valuation and court search.  # SPEC §6, §7.2, §8.1

The enrichment adapters are Phase 3. Until they exist the team is the source, and these
tests pin the three things that matter: the entries are typed and validated the same way the
engine would test them, an adapter value wins when there is one, and a deal the team has
valued and searched reaches Go where the same deal without those entries does not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.main import app
from config.config import Config
from db.models import Deal
from db.repository import transient_deal
from db.session import get_session
from engine.screen import screen
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import (
    Channel,
    CourtFlag,
    CourtRecordInputs,
    CourtRecordsStatus,
    LienKind,
    Severity,
    Status,
    TeamCourtRecord,
    ValueSource,
    Verdict,
)
from services import (
    DealNotReady,
    UnderwriteRequest,
    latest_screen,
    run_screen,
    run_underwrite,
    screen_result,
)
from services.assemble import court_records, resolve_valuation, screen_inputs
from services.enrichment import AdapterValues
from tests.conftest import requires_db, store_deal

CONFIG = Config.load()
D = Decimal
FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals/go_team_overrides_tulsa.json"
)
SEARCHED_ON = date(2026, 9, 16)


def team_entry(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["team_entry"]
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def deal_from(**overrides: Any) -> Any:
    payload = team_entry(**overrides)
    record = normalize(parse_team_form(TeamEntryForm(**payload)), Channel.TEAM, raw_payload=payload)
    return transient_deal(record)


# --- the entries are typed (SPEC §7.2) -----------------------------------------------------------


def test_a_matter_must_carry_what_its_code_is_tested_on() -> None:
    with pytest.raises(ValidationError, match="needs occurred_on"):
        TeamCourtRecord(code=CourtFlag.BANKRUPTCY_IN_LOOKBACK)
    with pytest.raises(ValidationError, match="needs amount_usd"):
        TeamCourtRecord(code=CourtFlag.OPEN_TAX_LIEN)
    with pytest.raises(ValidationError, match="needs amount_usd"):
        TeamCourtRecord(code=CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD)
    with pytest.raises(ValidationError, match="senior and resolved_at_close"):
        TeamCourtRecord(code=CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS, senior=True)
    # the two codes with nothing to test carry nothing
    assert TeamCourtRecord(code=CourtFlag.ACTIVE_FORECLOSURE_AS_OWNER).amount_usd is None
    assert TeamCourtRecord(code=CourtFlag.LANDLORD_TENANT_AS_LANDLORD).occurred_on is None


def test_lien_facts_belong_only_to_a_lien() -> None:
    with pytest.raises(ValidationError, match="apply only to"):
        TeamCourtRecord(code=CourtFlag.OPEN_TAX_LIEN, amount_usd=D("900"), senior=True)


def test_a_search_with_findings_must_say_what_and_when() -> None:
    with pytest.raises(ValidationError, match="needs court_records_as_of"):
        TeamEntryForm(**team_entry(court_records_as_of=None))
    with pytest.raises(ValidationError, match="FLAGS needs at least one matter"):
        TeamEntryForm(**team_entry(court_records_status=CourtRecordsStatus.FLAGS))
    with pytest.raises(ValidationError, match="CLEAN cannot carry a matter"):
        TeamEntryForm(
            **team_entry(
                court_records_team=[{"code": "ACTIVE_FORECLOSURE_AS_OWNER"}],
            )
        )
    with pytest.raises(ValidationError, match="NOT_CHECKED and unset carry neither"):
        TeamEntryForm(**team_entry(court_records_status=CourtRecordsStatus.NOT_CHECKED))


# --- the entries reach the engine as its own flag inputs (SPEC §7.2) -----------------------------


def test_a_clean_search_is_a_dated_empty_record_not_an_absent_one() -> None:
    records = court_records(deal_from())
    assert records is not None
    assert records.source is ValueSource.TEAM
    assert records.as_of == SEARCHED_ON
    assert records.bankruptcy_filing_dates == []
    assert records.subject_property_liens == []


def test_not_checked_and_unset_both_reach_the_engine_as_nothing() -> None:
    not_checked = deal_from(
        court_records_status=CourtRecordsStatus.NOT_CHECKED, court_records_as_of=None
    )
    assert court_records(not_checked) is None
    unset = deal_from(court_records_status=None, court_records_as_of=None)
    assert court_records(unset) is None


def test_findings_spread_across_the_fields_the_screen_tests() -> None:
    deal = deal_from(
        court_records_status=CourtRecordsStatus.FLAGS,
        court_records_team=[
            {"code": "BANKRUPTCY_IN_LOOKBACK", "occurred_on": "2024-02-11"},
            {"code": "UNSATISFIED_JUDGMENT_OVER_THRESHOLD", "amount_usd": "6000.00"},
            {"code": "UNSATISFIED_JUDGMENT_OVER_THRESHOLD", "amount_usd": "7000.00"},
            {"code": "OPEN_TAX_LIEN", "amount_usd": "1200.00"},
            {"code": "ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT", "amount_usd": "30000.00"},
            {
                "code": "SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK",
                "occurred_on": "2025-06-01",
            },
            {"code": "ACTIVE_FORECLOSURE_AS_OWNER"},
            {"code": "LANDLORD_TENANT_AS_LANDLORD"},
            {"code": "LANDLORD_TENANT_AS_LANDLORD"},
            {
                "code": "SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS",
                "lien_kind": "LIS_PENDENS",
                "senior": True,
                "resolved_at_close": False,
                "amount_usd": "4500.00",
            },
        ],
    )
    records = court_records(deal)
    assert records is not None
    assert records.bankruptcy_filing_dates == [date(2024, 2, 11)]
    assert records.unsatisfied_judgments_usd == [D("6000.00"), D("7000.00")]
    assert records.open_tax_liens_usd == [D("1200.00")]
    assert records.active_civil_litigation_as_defendant_usd == [D("30000.00")]
    assert records.satisfied_judgment_or_released_lien_dates == [date(2025, 6, 1)]
    assert records.active_foreclosure_as_owner is True
    assert records.landlord_tenant_matters_as_landlord == 2
    assert len(records.subject_property_liens) == 1
    lien = records.subject_property_liens[0]
    assert lien.kind is LienKind.LIS_PENDENS and lien.senior and not lien.resolved_at_close


def test_the_config_thresholds_still_decide_a_team_entered_matter() -> None:
    """The team records the matter and its amount; the yaml decides whether it is a flag."""
    under = deal_from(
        court_records_status=CourtRecordsStatus.FLAGS,
        court_records_team=[
            {"code": "UNSATISFIED_JUDGMENT_OVER_THRESHOLD", "amount_usd": "4000.00"}
        ],
    )
    result = screen(screen_inputs(under), CONFIG)
    assert "UNSATISFIED_JUDGMENT_OVER_THRESHOLD" not in {f.code.value for f in result.flags}
    assert result.verdict is Verdict.GO  # 4,000 is under the 10,000 aggregate threshold

    over = deal_from(
        court_records_status=CourtRecordsStatus.FLAGS,
        court_records_team=[
            {"code": "UNSATISFIED_JUDGMENT_OVER_THRESHOLD", "amount_usd": "12000.00"}
        ],
    )
    flagged = screen(screen_inputs(over), CONFIG)
    flag = next(f for f in flagged.flags if f.code is CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD)
    assert flag.severity is Severity.HARD  # the severity is config, not the team's say-so
    assert flagged.verdict is Verdict.DECLINE


# --- precedence: an adapter value wins, the team value is kept (SPEC §6) -------------------------


def test_the_team_valuation_is_used_when_nothing_else_has_one() -> None:
    deal = deal_from()
    valuation = resolve_valuation(deal)
    assert valuation.as_is_value == D("250000.00")
    assert valuation.as_is_value_source is ValueSource.TEAM
    assert valuation.arv == D("295000.00")
    assert valuation.arv_source is ValueSource.TEAM


def test_an_adapter_value_wins_and_the_team_value_is_retained() -> None:
    deal = deal_from()
    adapters = AdapterValues(as_is_value=D("262500.00"), arv=D("310000.00"))
    valuation = resolve_valuation(deal, adapters)
    assert valuation.as_is_value == D("262500.00")
    assert valuation.as_is_value_source is ValueSource.ADAPTER
    assert valuation.arv_source is ValueSource.ADAPTER
    # retained for audit: resolution reads the deal, it never writes back to it
    assert deal.as_is_value_team == D("250000.00")
    assert deal.arv_team == D("295000.00")


def test_an_adapter_wins_one_half_of_the_valuation_without_touching_the_other() -> None:
    valuation = resolve_valuation(deal_from(), AdapterValues(arv=D("310000.00")))
    assert (valuation.as_is_value, valuation.as_is_value_source) == (
        D("250000.00"),
        ValueSource.TEAM,
    )
    assert (valuation.arv, valuation.arv_source) == (D("310000.00"), ValueSource.ADAPTER)


def test_an_underwrite_request_outranks_the_deal_but_not_an_adapter() -> None:
    deal = deal_from()
    request = UnderwriteRequest(
        as_is_value=D("255000.00"),
        market_rent_monthly=D("2400.00"),
        annual_utilities_usd=D("840.00"),
    )
    # the team typed a newer number when they advanced the deal
    from_request = resolve_valuation(deal, request=request)
    assert from_request.as_is_value == D("255000.00")
    assert from_request.as_is_value_source is ValueSource.TEAM
    # an adapter still beats it
    with_adapter = resolve_valuation(
        deal, AdapterValues(as_is_value=D("262500.00")), request=request
    )
    assert with_adapter.as_is_value == D("262500.00")
    assert with_adapter.as_is_value_source is ValueSource.ADAPTER


def test_an_adapter_court_record_wins_over_the_team_search() -> None:
    deal = deal_from(
        court_records_status=CourtRecordsStatus.FLAGS,
        court_records_team=[{"code": "ACTIVE_FORECLOSURE_AS_OWNER"}],
    )
    pulled = court_records(deal)
    assert pulled is not None and pulled.active_foreclosure_as_owner is True
    from_adapter = court_records(
        deal, AdapterValues(court_records=CourtRecordInputs(as_of=SEARCHED_ON))
    )
    assert from_adapter is not None
    assert from_adapter.source is ValueSource.ADAPTER
    assert from_adapter.active_foreclosure_as_owner is False
    # the team's own entry is still on the deal
    assert deal.court_records_status is CourtRecordsStatus.FLAGS
    assert deal.court_records_team == [{"code": "ACTIVE_FORECLOSURE_AS_OWNER"}]


# --- what the overrides are for: a Go before any adapter exists (SPEC §7.5) ----------------------


def test_the_same_deal_is_declined_without_the_overrides_and_go_with_them() -> None:
    """The whole point of the overrides: this deal is undecidable without them.

    With no as-is value the LTV falls back to the purchase price (SPEC §7.4), where a
    185,000 loan on a 185,000 house is 100% and blows through the cap and its band. The
    team's own 250,000 valuation turns the same deal into a 74.0% LTV and a Go.
    """
    bare = deal_from(
        as_is_value_team=None,
        arv_team=None,
        court_records_status=None,
        court_records_as_of=None,
    )
    without = screen(screen_inputs(bare), CONFIG)
    assert without.verdict is Verdict.DECLINE
    assert {f.code.value for f in without.flags} >= {
        "AS_IS_VALUE_MISSING",
        "ARV_MISSING",
        "COURT_RECORDS_NOT_CHECKED",
        "LTV_AS_IS_OVER_CAP",
    }
    assert without.sizing.as_is_value_source is None
    assert without.components.court_records_source is None

    with_overrides = screen(screen_inputs(deal_from()), CONFIG)
    assert with_overrides.verdict is Verdict.GO
    # the only flag left says where the numbers came from, and INFO never moves a verdict
    assert [f.code.value for f in with_overrides.flags] == ["TEAM_SOURCED_VALUES"]
    assert with_overrides.flags[0].severity is Severity.INFO
    assert with_overrides.sizing.as_is_value_source is ValueSource.TEAM
    assert with_overrides.sizing.arv_source is ValueSource.TEAM
    assert with_overrides.components.court_records_source is ValueSource.TEAM


def test_a_provenance_stamp_travels_onto_the_stored_result() -> None:
    """The verdict alone does not say a Go rested on hand-entered numbers; the result does."""
    result = screen(screen_inputs(deal_from()), CONFIG)
    again = type(result).model_validate_json(result.model_dump_json())
    assert again.sizing.as_is_value_source is ValueSource.TEAM
    assert again.components.court_records_source is ValueSource.TEAM


# --- through the database and the API (SPEC §5) --------------------------------------------------


@requires_db
def test_the_overrides_survive_a_round_trip_through_the_deals_row(
    db_session: Session, team_entry_with_overrides: dict[str, Any]
) -> None:
    payload = {
        **team_entry_with_overrides,
        "court_records_status": "FLAGS",
        "court_records_team": [
            {"code": "LANDLORD_TENANT_AS_LANDLORD", "description": "eviction, 2025"},
            {
                "code": "SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS",
                "lien_kind": "LIEN",
                "senior": False,
                "resolved_at_close": True,
                "amount_usd": "3200.00",
            },
        ],
    }
    deal = store_deal(db_session, payload)
    db_session.expire_all()

    reloaded = db_session.get(Deal, deal.id)
    assert reloaded is not None
    assert reloaded.as_is_value_team == D("250000.00")
    assert reloaded.arv_team == D("295000.00")
    assert reloaded.court_records_status is CourtRecordsStatus.FLAGS
    assert reloaded.court_records_as_of == SEARCHED_ON
    matters = [TeamCourtRecord.model_validate(entry) for entry in reloaded.court_records_team]
    assert [m.code for m in matters] == [
        CourtFlag.LANDLORD_TENANT_AS_LANDLORD,
        CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS,
    ]
    assert matters[1].amount_usd == D("3200.00")
    assert matters[1].resolved_at_close is True


@requires_db
def test_a_stored_screen_records_that_the_numbers_were_the_team_s(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG)
    db_session.commit()
    row = latest_screen(db_session, deal_with_overrides.id)
    assert row is not None
    stored = screen_result(row)
    assert stored.verdict is Verdict.GO
    assert stored.sizing.as_is_value_source is ValueSource.TEAM
    assert stored.components.court_records_source is ValueSource.TEAM
    assert row.inputs["deal"]["as_is_value_source"] == "TEAM"
    assert row.inputs["court_records"]["source"] == "TEAM"


@requires_db
def test_an_underwrite_falls_back_to_the_team_valuation_on_the_deal(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The request names no valuation at all; SPEC §8.1 still needs both halves."""
    result = run_underwrite(
        db_session,
        deal_with_overrides.id,
        UnderwriteRequest(market_rent_monthly=D("2400.00"), annual_utilities_usd=D("840.00")),
        CONFIG,
    )
    db_session.commit()
    assert result.sizing.as_is_value_source is ValueSource.TEAM
    assert result.sizing.arv_source is ValueSource.TEAM
    assert result.downside.recovery_basis == D("295000.00")  # min(250,000 + 46,200, 295,000)


@requires_db
def test_an_underwrite_with_no_valuation_anywhere_is_named_not_guessed(
    db_session: Session, stored_deal: Deal
) -> None:
    """Past the screen gate, a deal with no valuation from any source is named, not guessed."""
    stored_deal.status = Status.SCREENED  # the screen gate is a separate test
    db_session.flush()
    with pytest.raises(DealNotReady) as caught:
        run_underwrite(
            db_session,
            stored_deal.id,
            UnderwriteRequest(market_rent_monthly=D("1500.00"), annual_utilities_usd=D("600.00")),
            CONFIG,
        )
    assert caught.value.missing == [
        "as_is_value (no adapter value, none on the request, none on the deal)",
        "arv (no adapter value, none on the request, none on the deal)",
    ]


@requires_db
def test_the_intake_endpoint_refuses_an_incoherent_court_block(
    db_session: Session, team_entry_with_overrides: dict[str, Any]
) -> None:
    """A 422 from the form, not a 500 from half-way through assembly."""

    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        response = client.post(
            "/intake/team",
            json={**team_entry_with_overrides, "court_records_status": "FLAGS"},
        )
    app.dependency_overrides.clear()
    assert response.status_code == 422
    assert "FLAGS needs at least one matter" in response.text
