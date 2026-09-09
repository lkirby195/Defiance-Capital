"""Normalizer tests: missing_fields, status, phone/address/state cleanup.  # SPEC §4.1, §4.5"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from intake.normalize import (
    MINIMUM_FIELDS,
    ParsedIntake,
    infer_state,
    normalize,
    normalize_address,
    normalize_phone,
)
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import BorrowerInfo, Channel, DealInfo, PropertyInfo, State, Status

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def complete_form(**overrides: object) -> TeamEntryForm:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return TeamEntryForm.model_validate(payload)


def test_complete_team_entry_is_new_with_nothing_missing() -> None:
    form = complete_form()
    record = normalize(parse_team_form(form), Channel.TEAM, form.model_dump(mode="json"))
    assert record.missing_fields == []
    assert record.status is Status.NEW
    assert record.channel is Channel.TEAM
    assert record.borrower.phone == "+19185550142"
    assert record.borrower.entity_name == "Whitfield Holdings LLC"
    assert record.property.address_raw == "1412 S Cheyenne Ave, Tulsa, OK 74119"
    assert record.property.address_normalized == "1412 S CHEYENNE AVE, TULSA, OK 74119"
    assert record.property.state is State.OK
    assert record.deal.purchase_price == Decimal("185000.00")
    assert record.deal.rehab_budget == Decimal("42000.00")
    assert record.raw_payload == form.model_dump(mode="json")


def test_partial_entry_lists_missing_fields_in_asking_order() -> None:
    form = TeamEntryForm(address="12 Elm St, Denver, CO 80202", purchase_price=Decimal("100000"))
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.status is Status.NEEDS_INFO
    assert record.missing_fields == [
        "deal.rehab_budget",
        "deal.loan_requested",
        "deal.term_bucket",
        "borrower.name",
        "borrower.phone",
        "borrower.credit_range",
        "borrower.experience_bucket",
        "borrower.repeat_borrower",
    ]
    assert record.property.state is State.CO


def test_empty_entry_is_missing_everything() -> None:
    record = normalize(ParsedIntake(), Channel.SMS, "hello?")
    assert record.missing_fields == list(MINIMUM_FIELDS)
    assert record.status is Status.NEEDS_INFO
    assert record.property.state is State.OTHER


def test_rehab_budget_zero_counts_as_answered() -> None:
    form = complete_form(rehab_budget="0")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert "deal.rehab_budget" not in record.missing_fields
    assert record.deal.rehab_budget == 0


def test_listing_url_satisfies_the_property_requirement() -> None:
    form = complete_form(address=None, listing_url="https://www.zillow.com/homedetails/1-Main-St")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert "property.address_raw" not in record.missing_fields
    assert record.property.address_raw is None
    assert record.property.address_normalized is None
    assert record.property.state is State.OTHER


def test_blank_strings_count_as_missing() -> None:
    form = complete_form(borrower_name="   ", borrower_phone="")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.borrower.name is None
    assert "borrower.name" in record.missing_fields
    assert "borrower.phone" in record.missing_fields


def test_explicit_state_wins_over_address_text() -> None:
    form = complete_form(state="CO")  # address says OK
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.property.state is State.CO


def test_prenormalized_address_is_kept() -> None:
    parsed = ParsedIntake(
        borrower=BorrowerInfo(),
        property=PropertyInfo(address_raw="1 main st", address_normalized="1 MAIN ST, TULSA, OK"),
        deal=DealInfo(),
    )
    assert normalize(parsed, Channel.LINK, {}).property.address_normalized == "1 MAIN ST, TULSA, OK"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(918) 555-0100", "+19185550100"),
        ("918.555.0100", "+19185550100"),
        ("1-918-555-0100", "+19185550100"),
        ("+1 918 555 0100", "+19185550100"),
        ("+44 20 7946 0958", "+44 20 7946 0958"),
        ("555-0100", "555-0100"),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  123  main st,tulsa ,ok  ", "123 MAIN ST, TULSA, OK"),
        ("12 Elm St., Denver, CO 80202.", "12 ELM ST., DENVER, CO 80202"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_address(raw: str | None, expected: str | None) -> None:
    assert normalize_address(raw) == expected


@pytest.mark.parametrize(
    "address, expected",
    [
        ("1412 S Cheyenne Ave, Tulsa, OK 74119", State.OK),
        ("1412 S Cheyenne Ave Tulsa OK", State.OK),
        ("1412 S Cheyenne Ave, Tulsa, Oklahoma", State.OK),
        ("12 Elm St, Denver, CO 80202-1234", State.CO),
        ("12 Elm St, Denver, co", State.CO),
        ("500 Congress Ave, Austin, TX 78701", State.OTHER),
        ("1 Pikes Peak Ave, Colorado Springs", State.OTHER),
        ("123 Main Street", State.OTHER),
        ("", State.OTHER),
        (None, State.OTHER),
    ],
)
def test_infer_state(address: str | None, expected: State) -> None:
    assert infer_state(address) is expected


def test_team_form_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        TeamEntryForm.model_validate({"fico": 720})
