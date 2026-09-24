"""Normalizer tests: missing_fields, status, phone/address/state cleanup.  # SPEC §4.1, §4.5"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from intake.normalize import (
    MINIMUM_FIELDS,
    ParsedIntake,
    infer_product,
    infer_state,
    normalize,
    normalize_address,
    normalize_phone,
)
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import (
    SPLIT_PRODUCTS,
    BorrowerInfo,
    Channel,
    DealInfo,
    Product,
    ProductSource,
    PropertyInfo,
    State,
    StateSource,
    Status,
    TermBucket,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def complete_form(**overrides: object) -> TeamEntryForm:
    """The complete team entry, with anything the caller names replaced.

    The fixture is a SPLIT_DRAW deal and carries a loan split. An override that moves it off
    a split product takes the split with it, because a NO_DRAW or WHOLETAIL loan has none
    (SPEC §8.2) and the form refuses one - which is its own test rather than a trap for
    every test that changes the product.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    chosen = payload.get("product")
    budget = payload.get("rehab_costs")
    product = (
        Product(chosen)
        if chosen is not None
        else (infer_product(Decimal(str(budget))) if budget is not None else None)
    )
    if product not in SPLIT_PRODUCTS:
        payload.pop("loan_purchase_portion", None)
        payload.pop("loan_rehab_portion", None)
    return TeamEntryForm.model_validate(payload)


def test_complete_team_entry_is_new_with_nothing_missing() -> None:
    form = complete_form()
    record = normalize(parse_team_form(form), Channel.TEAM, form.model_dump(mode="json"))
    assert record.missing_fields == []
    assert record.status is Status.NEW
    assert record.channel is Channel.TEAM
    assert record.borrower.phone == "7205550192"  # digits (SPEC §4.1)
    assert record.borrower.entity_name == "Ortiz Builds LLC"
    assert record.property.address_raw == "3320 Meade St, Denver, CO 80211"
    assert record.property.address_normalized == "3320 MEADE ST, DENVER, CO 80211"
    assert record.property.state is State.CO
    assert record.deal.purchase_price == Decimal("200000.00")
    assert record.deal.rehab_costs == Decimal("48000.00")
    assert record.raw_payload == form.model_dump(mode="json")


def test_partial_entry_lists_missing_fields_in_asking_order() -> None:
    form = TeamEntryForm(address="12 Elm St, Denver, CO 80202", purchase_price=Decimal("100000"))
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.status is Status.NEEDS_INFO
    assert record.missing_fields == [
        "deal.rehab_costs",
        "deal.loan_requested",
        "deal.term",
        "borrower.name",
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


def test_rehab_costs_zero_counts_as_answered() -> None:
    form = complete_form(rehab_costs="0")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert "deal.rehab_costs" not in record.missing_fields
    assert record.deal.rehab_costs == 0


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
    # ...but not the phone: it is optional (SPEC §4.1), the match key when there is one
    assert record.borrower.phone is None
    assert "borrower.phone" not in record.missing_fields


def test_the_term_is_one_row_for_two_ways_of_saying_it() -> None:
    """A bucket from a borrower channel or months from the team form; either answers it."""
    from_team = normalize(parse_team_form(TeamEntryForm(term_months=7)), Channel.TEAM, {})
    assert "deal.term" not in from_team.missing_fields
    from_bucket = normalize(ParsedIntake(deal=DealInfo(term_bucket=TermBucket.M6)), Channel.SMS, {})
    assert "deal.term" not in from_bucket.missing_fields
    assert "deal.term" in normalize(ParsedIntake(), Channel.SMS, {}).missing_fields


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
        ("(918) 555-0100", "9185550100"),
        ("918.555.0100", "9185550100"),
        ("1-918-555-0100", "9185550100"),
        ("+1 918 555 0100", "9185550100"),
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


# --- state_source (Phase 1 review) and actual opex pass-through ------------------------------


def test_state_entered_by_the_team_is_recorded_as_entered() -> None:
    form = complete_form(state="CO")  # address says OK; the explicit entry wins
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.property.state is State.CO
    assert record.property.state_source is StateSource.ENTERED


def test_state_inferred_from_the_address_is_recorded_as_inferred() -> None:
    record = normalize(parse_team_form(complete_form()), Channel.TEAM, {})
    assert record.property.state is State.CO
    assert record.property.state_source is StateSource.INFERRED


def test_entered_other_is_not_overridden_by_the_address() -> None:
    form = complete_form(state="OTHER")  # address says CO
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.property.state is State.OTHER
    assert record.property.state_source is StateSource.ENTERED


def test_no_address_and_no_state_is_inferred_other() -> None:
    record = normalize(ParsedIntake(), Channel.SMS, "hi")
    assert record.property.state is State.OTHER
    assert record.property.state_source is StateSource.INFERRED


def test_prenormalized_inferred_state_is_re_inferred_from_the_address() -> None:
    parsed = ParsedIntake(
        property=PropertyInfo(
            address_raw="1 Main St, Denver, CO 80202",
            state=State.OK,
            state_source=StateSource.INFERRED,
        )
    )
    record = normalize(parsed, Channel.LINK, {})
    assert record.property.state is State.CO


def test_actual_opex_pass_through_the_team_form() -> None:
    form = complete_form(holding_costs_pct_of_cost="0.04", monthly_rent="2500.00")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.deal.holding_costs_pct_of_cost == Decimal("0.04")
    assert record.deal.monthly_rent == Decimal("2500.00")
    assert (
        normalize(parse_team_form(complete_form()), Channel.TEAM, {}).deal.origination_fee_pct
        is None
    )


# --- product inference (review decision) -------------------------------------------------------


def test_product_is_inferred_from_the_rehab_costs() -> None:
    assert infer_product(Decimal("0")) is Product.NO_DRAW
    assert infer_product(Decimal("0.00")) is Product.NO_DRAW
    assert infer_product(Decimal("0.01")) is Product.SPLIT_DRAW
    assert infer_product(Decimal("50000")) is Product.SPLIT_DRAW
    assert infer_product(None) is None


def test_wholetail_and_split_principal_are_never_inferred() -> None:
    inferred = {infer_product(Decimal(x)) for x in ("0", "1", "42000", "999999")}
    assert inferred == {Product.NO_DRAW, Product.SPLIT_DRAW}


def test_inferred_product_is_recorded_as_inferred() -> None:
    record = normalize(parse_team_form(complete_form()), Channel.TEAM, {})  # rehab 48,000
    assert record.deal.product is Product.SPLIT_DRAW
    assert record.deal.product_source is ProductSource.INFERRED
    record = normalize(parse_team_form(complete_form(rehab_costs="0")), Channel.TEAM, {})
    assert record.deal.product is Product.NO_DRAW
    assert record.deal.product_source is ProductSource.INFERRED


def test_entered_product_wins_and_is_recorded_as_entered() -> None:
    record = normalize(parse_team_form(complete_form(product="WHOLETAIL")), Channel.TEAM, {})
    assert record.deal.product is Product.WHOLETAIL
    assert record.deal.product_source is ProductSource.ENTERED
    form = complete_form(rehab_costs="0", product="SPLIT_PRINCIPAL")
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.deal.product is Product.SPLIT_PRINCIPAL  # inference does not override


def test_product_stays_unset_until_the_rehab_costs_is_known() -> None:
    form = TeamEntryForm(address="12 Elm St, Denver, CO 80202", purchase_price=Decimal("100000"))
    record = normalize(parse_team_form(form), Channel.TEAM, {})
    assert record.deal.product is None
    assert record.deal.product_source is None
    assert "deal.rehab_costs" in record.missing_fields
