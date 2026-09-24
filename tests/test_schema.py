"""IntakeRecord and enum tests.  # SPEC §4.5"""

from __future__ import annotations

import json
from datetime import UTC, date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from schema.dates import Term
from schema.generate import INTAKE_JSON, render
from schema.models import (
    Channel,
    CourtFlag,
    DealInfo,
    ExperienceBucket,
    ExperienceTier,
    IntakeRecord,
    Product,
    ProductSource,
    PropertyInfo,
    ScreenFlag,
    State,
    StatedExit,
    StateSource,
    Status,
    TermBucket,
    Tranche,
    Verdict,
)


def test_committed_intake_json_matches_model() -> None:
    assert INTAKE_JSON.read_text(encoding="utf-8") == render(), (
        "schema/intake.json is stale: run `uv run python -m schema.generate`"
    )


def test_enum_values_match_spec() -> None:
    assert [m.value for m in Product] == ["NO_DRAW", "SPLIT_DRAW", "SPLIT_PRINCIPAL", "WHOLETAIL"]
    assert [m.value for m in Tranche] == ["T1", "T2", "T3", "T4", "T5"]
    assert [m.value for m in ExperienceBucket] == ["0", "1_2", "3_5", "6_PLUS"]
    assert [m.value for m in ExperienceTier] == ["E0", "E1", "E2", "E3"]
    assert [m.value for m in TermBucket] == ["3", "6", "9", "12", "12_PLUS"]
    assert [m.value for m in Channel] == ["SMS", "LINK", "CONTRACT", "TEAM"]
    assert [m.value for m in StatedExit] == ["FLIP", "HOLD", "WHOLETAIL", "UNKNOWN"]
    assert [m.value for m in State] == ["OK", "CO", "OTHER"]
    assert [m.value for m in Verdict] == ["GO", "CONDITIONAL", "DECLINE"]
    assert [m.value for m in Status] == [
        "NEW",
        "NEEDS_INFO",
        "SCREENED",
        "IN_REVIEW",
        "UNDERWRITING",
        "LOI_SENT",
        "HANDED_OFF",
        "DECLINED",
        "DEAD",
    ]


def test_defaults() -> None:
    record = IntakeRecord(channel=Channel.SMS, raw_payload="hi")
    assert record.status is Status.NEW
    assert record.missing_fields == []
    assert record.property.state is State.OTHER
    assert record.created_at.tzinfo is UTC
    assert record.borrower.repeat_borrower is None


def test_json_round_trip() -> None:
    record = IntakeRecord(
        channel=Channel.TEAM,
        raw_payload={"address": "1 Main St"},
        borrower={"name": "A", "phone": "+19185550100", "credit_range": "T2"},
        property={"address_raw": "1 Main St", "state": "OK"},
        deal={"purchase_price": "150000.00", "rehab_costs": 0, "term_bucket": "12_PLUS"},
        missing_fields=["deal.loan_requested"],
    )
    again = IntakeRecord.model_validate_json(record.model_dump_json())
    assert again == record
    assert again.deal.purchase_price == Decimal("150000.00")
    assert again.deal.term_bucket is TermBucket.M12_PLUS
    assert again.borrower.credit_range is Tranche.T2


@pytest.mark.parametrize(
    "field, value",
    [
        ("purchase_price", "0"),
        ("purchase_price", "-1"),
        ("loan_requested", "0"),
        ("rehab_costs", "-0.01"),
        ("purchase_price", "100.123"),
        ("loan_requested", "1000000000000.00"),
    ],
)
def test_money_bounds_and_precision(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        DealInfo(**{field: Decimal(value)})


def test_rehab_costs_zero_allowed() -> None:
    assert DealInfo(rehab_costs=Decimal("0")).rehab_costs == 0


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        IntakeRecord(channel=Channel.TEAM, raw_payload={}, bogus=1)
    with pytest.raises(ValidationError):
        IntakeRecord(channel=Channel.TEAM, raw_payload={}, borrower={"fico": 700})


# --- Phase 1 review: state_source, actual opex; Phase 2a: flag codes --------------------------


def test_state_source_enum_and_default() -> None:
    assert [m.value for m in StateSource] == ["ENTERED", "INFERRED"]
    assert PropertyInfo().state_source is StateSource.INFERRED
    assert PropertyInfo(state="CO", state_source="ENTERED").state_source is StateSource.ENTERED


def test_flag_codes_are_stable_and_do_not_collide() -> None:
    assert all(m.value == m.name for m in ScreenFlag)
    assert all(m.value == m.name for m in CourtFlag)
    assert {m.value for m in ScreenFlag}.isdisjoint({m.value for m in CourtFlag})


def test_the_new_economics_fields_are_optional() -> None:
    """The holding cost is a share of the cost (SPEC §8.1); the rent is money."""
    assert DealInfo().holding_costs_pct_of_cost is None
    assert DealInfo().monthly_rent is None
    deal = DealInfo(holding_costs_pct_of_cost="0.025", monthly_rent=0)
    assert deal.holding_costs_pct_of_cost == Decimal("0.025")
    assert deal.monthly_rent == 0
    with pytest.raises(ValidationError):
        DealInfo(holding_costs_pct_of_cost=Decimal("-1"))
    with pytest.raises(ValidationError):
        DealInfo(holding_costs_pct_of_cost=Decimal("1.5"))  # a share, not a multiple
    with pytest.raises(ValidationError):
        DealInfo(monthly_rent=Decimal("1.005"))


def test_a_term_is_whole_months_plus_a_stub_and_runs_for_some_time() -> None:
    """SPEC §8.1: a payoff date between two anchors leaves days the ledger prices."""
    assert DealInfo().term_stub_days is None
    stubbed = DealInfo(closing_date=date(2027, 3, 15), term_months=9, term_stub_days=11)
    assert stubbed.term == Term(9, 11)
    assert stubbed.payoff_date == date(2027, 12, 26)
    all_stub = DealInfo(closing_date=date(2027, 1, 1), term_months=0, term_stub_days=20)
    assert all_stub.payoff_date == date(2027, 1, 21)
    with pytest.raises(ValidationError, match="no months and no days"):
        DealInfo(term_months=0)
    with pytest.raises(ValidationError, match="needs a term in months"):
        DealInfo(term_stub_days=11)


def test_committed_intake_json_carries_the_new_fields() -> None:
    schema = json.loads(INTAKE_JSON.read_text(encoding="utf-8"))
    assert "state_source" in schema["$defs"]["PropertyInfo"]["properties"]
    for name in (
        "closing_date",
        "interest_rate",
        "holding_costs_pct_of_cost",
        "term_stub_days",
        "monthly_rent",
    ):
        assert name in schema["$defs"]["DealInfo"]["properties"], name
    for name in ("city", "units", "sf", "beds", "baths", "garage_spaces"):
        assert name in schema["$defs"]["PropertyInfo"]["properties"], name
    assert schema["$defs"]["StateSource"]["enum"] == ["ENTERED", "INFERRED"]
    assert schema["$defs"]["LoanPurpose"]["enum"] == [
        "PURCHASE",
        "REFINANCE",
        "CASH_OUT",
        "CONSTRUCTION",
    ]


def test_product_source_enum_and_pairing() -> None:
    assert [m.value for m in ProductSource] == ["ENTERED", "INFERRED"]
    assert DealInfo().product is None and DealInfo().product_source is None
    deal = DealInfo(product="WHOLETAIL", product_source="ENTERED")
    assert deal.product is Product.WHOLETAIL and deal.product_source is ProductSource.ENTERED
    with pytest.raises(ValidationError, match="set together"):
        DealInfo(product="NO_DRAW")
    with pytest.raises(ValidationError, match="set together"):
        DealInfo(product_source="INFERRED")
    schema = json.loads(INTAKE_JSON.read_text(encoding="utf-8"))
    assert schema["$defs"]["ProductSource"]["enum"] == ["ENTERED", "INFERRED"]
    assert "product_source" in schema["$defs"]["DealInfo"]["properties"]
