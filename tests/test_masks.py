"""What a person types where the model stores a number, and reads where it stores a code.

Two modules, two halves of the same boundary:

* ``schema/masks.py`` is the formatting itself - money as ``$#,###``, a percent as ``12%``
  over a stored ``0.12``, a phone as ``###-###-####`` over stored digits. Everything here is
  a **round trip**: what ``*_display`` writes, ``*_parse`` reads back to the same number, and
  the deal carries the number either way.
* ``schema/labels.py`` is the other direction for codes: ``SPLIT_DRAW`` is "Split Draw"
  wherever a person reads it, and the stored value never changes.

``api/masks.py`` says which box is which, and holds the one rule that is not pure
formatting: a box still showing its config default stores nothing, so the deal goes on saying
"the config decides" rather than recording a number nobody chose (SPEC §8.1).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.intake_form import (
    blank_form_values,
    deal_defaults,
    holding_costs_hint,
    read_form,
)
from api.masks import (
    DEFAULTED_FIELDS,
    MONEY_FIELDS,
    PERCENT_FIELDS,
    PHONE_FIELDS,
    drop_defaults,
    holding_costs_amount,
    mask_one,
    masked,
    unmask_one,
    unmasked,
)
from config.config import Config
from db.models import Deal
from schema.labels import enum_label, product_definition
from schema.masks import (
    money_display,
    money_parse,
    pct_display,
    pct_parse,
    phone_digits,
    phone_display,
)
from schema.models import AssetType, LoanPurpose, Product, StatedExit
from tests.conftest import QueueClient, requires_db

CONFIG = Config.load()
D = Decimal


# --- money ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored, shown",
    [
        (D("425000"), "$425,000"),
        (D("425000.00"), "$425,000"),  # the trailing cents are noise on a round number
        (D("1234.56"), "$1,234.56"),
        (D("0"), "$0"),
        (D("999"), "$999"),
        (D("1000000"), "$1,000,000"),
    ],
)
def test_money_shows_and_reads_back_the_same_number(stored: Decimal, shown: str) -> None:
    assert money_display(stored) == shown
    assert D(money_parse(shown)) == stored


def test_money_takes_what_a_person_types_with_or_without_the_furniture() -> None:
    for typed in ("$425,000", "425,000", "425000", " $425,000 "):
        assert D(money_parse(typed)) == D("425000")


def test_no_money_is_an_empty_box_rather_than_a_zero() -> None:
    assert money_display(None) == ""
    assert money_parse("") == "" and money_parse(None) == ""


# --- percent -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored, shown",
    [
        (D("0.12"), "12%"),
        (D("0.12000"), "12%"),  # NUMERIC(7,5) hands back the zeros; they mean nothing
        (D("0.125"), "12.5%"),
        (D("0.00"), "0%"),
        (D("0.065"), "6.5%"),
        (D("1"), "100%"),
    ],
)
def test_percent_shows_and_reads_back_the_same_fraction(stored: Decimal, shown: str) -> None:
    assert pct_display(stored) == shown
    assert D(pct_parse(shown)) == stored


def test_a_person_types_twelve_and_the_deal_carries_nought_point_one_two() -> None:
    """The whole convention, in one line (SPEC §8.1)."""
    assert D(pct_parse("12")) == D("0.12")
    assert D(pct_parse("12%")) == D("0.12")
    assert pct_display(D("0.12")) == "12%"


def test_text_that_is_not_a_number_comes_back_untouched() -> None:
    """The model's complaint should be about what was typed, not about a mangling of it."""
    assert pct_parse("about twelve") == "about twelve"
    assert pct_display(None) == ""


# --- phone ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    ["555-123-4567", "(555) 123-4567", "5551234567", "+1 555 123 4567", "1-555-123-4567"],
)
def test_a_phone_stores_as_digits_and_shows_as_a_mask(typed: str) -> None:
    assert phone_digits(typed) == "5551234567"
    assert phone_display(phone_digits(typed)) == "555-123-4567"


def test_a_number_that_is_not_nanp_is_kept_rather_than_guessed_at() -> None:
    assert phone_digits("+44 20 7946 0958") == "+44 20 7946 0958"
    assert phone_display("+44 20 7946 0958") == "+44 20 7946 0958"
    assert phone_digits("   ") is None and phone_digits(None) is None


# --- which box is which --------------------------------------------------------------------------


def test_every_masked_field_round_trips_through_its_own_shape() -> None:
    """``masked`` then ``unmasked`` is the identity on what the deal stores."""
    stored: dict[str, Any] = {
        "purchase_price": D("425000"),
        "loan_requested": D("318750.50"),
        "interest_rate": D("0.1225"),
        "contingency_pct": D("0.10"),
        "borrower_phone": "5551234567",
        "address": "3320 Meade St, Denver, CO 80211",
    }
    shown = masked(stored)
    assert shown["purchase_price"] == "$425,000"
    assert shown["loan_requested"] == "$318,750.50"
    assert shown["interest_rate"] == "12.25%"
    assert shown["contingency_pct"] == "10%"
    assert shown["borrower_phone"] == "555-123-4567"
    assert shown["address"] == stored["address"]  # not a masked box; untouched

    back = unmasked(shown)
    for name, value in stored.items():
        if name in MONEY_FIELDS or name in PERCENT_FIELDS:
            assert D(back[name]) == value, name
        else:
            assert back[name] == value, name


def test_the_three_sets_do_not_overlap() -> None:
    assert MONEY_FIELDS.isdisjoint(PERCENT_FIELDS)
    assert PHONE_FIELDS.isdisjoint(MONEY_FIELDS | PERCENT_FIELDS)
    assert set(DEFAULTED_FIELDS) <= MONEY_FIELDS | PERCENT_FIELDS


def test_a_box_with_no_mask_is_left_alone() -> None:
    assert unmask_one("address", "12 Elm St") == "12 Elm St"
    assert mask_one("credit_range", "T1") == "T1"
    assert mask_one("flip_analysis", True) == "true"
    assert mask_one("purchase_price", None) == ""


# --- the config defaults (SPEC §8.1) --------------------------------------------------------------


def test_a_box_still_holding_its_default_stores_nothing() -> None:
    """So the column stays null, the engine reads config, and the checklist says DEFAULT."""
    defaults = deal_defaults(None, CONFIG)
    submitted = {"contingency_pct": "0.00", "origination_fee_pct": "0.02", "rehab_costs": "0"}
    kept = drop_defaults(submitted, defaults)
    assert "contingency_pct" not in kept and "origination_fee_pct" not in kept
    assert kept["rehab_costs"] == "0"  # not a defaulted field; untouched


def test_a_number_somebody_chose_is_kept_even_when_it_is_close() -> None:
    defaults = deal_defaults(None, CONFIG)
    kept = drop_defaults({"origination_fee_pct": "0.025"}, defaults)
    assert kept["origination_fee_pct"] == "0.025"


def test_all_four_defaults_are_flat_config_numbers_on_a_blank_form() -> None:
    """Holding costs are a percentage now, so nothing about the deal is needed to show it."""
    assert deal_defaults(None, CONFIG)["holding_costs_pct_of_cost"] == D("0.02")
    assert blank_form_values(CONFIG)["holding_costs_pct_of_cost"] == "2%"
    assert blank_form_values(CONFIG)["contingency_pct"] == "0%"
    assert blank_form_values(CONFIG)["origination_fee_pct"] == "2%"
    assert blank_form_values(CONFIG)["closing_costs_usd"] == "$1,000"


def test_the_dollars_a_holding_percentage_comes_to_are_shown_beside_the_box() -> None:
    """The box holds 2%; what a person checks is the dollars (SPEC §8.1)."""
    assert holding_costs_amount(D("0.02"), D("200000"), D("48000")) == D("4960.00")
    # ...and there is no figure until both halves of the cost basis are known.
    assert holding_costs_amount(D("0.02"), None, D("48000")) is None
    assert holding_costs_amount(None, D("200000"), D("48000")) is None
    assert holding_costs_amount(D("0.02"), D("0"), D("0")) is None
    hint = holding_costs_hint(D("0.02"), D("200000"), D("48000"))
    assert "$4,960.00" in hint and "$248,000.00" in hint
    assert holding_costs_hint(None, None, None) == (
        "of the purchase price plus the rehab costs, over the whole hold"
    )


COMPLETE: dict[str, str] = {
    "borrower_name": "Rafael Ortiz",
    "experience_bucket": "6_PLUS",
    "repeat_borrower": "false",
    "address": "3320 Meade St, Denver, CO 80211",
    "closing_date": "2026-10-01",
    "purchase_price": "$200,000",
    "rehab_costs": "$48,000",
    "loan_requested": "$195,000",
    "loan_purchase_portion": "$147,000",
    "loan_rehab_portion": "$48,000",
    "interest_rate": "12%",
    "term_months": "9",
    "holding_costs_pct_of_cost": "2%",
}


def test_the_form_reads_a_masked_submission_into_what_the_deal_stores() -> None:
    form, complaints = read_form(COMPLETE, [], CONFIG)
    assert not complaints, complaints.lines
    assert form is not None
    assert form.purchase_price == D("200000")
    assert form.loan_requested == D("195000")
    assert form.interest_rate == D("0.12")
    assert form.holding_costs_pct_of_cost is None  # it is the default, so it stores nothing


# --- the labels a person reads --------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, label",
    [
        (Product.NO_DRAW, "No Draw"),
        (Product.SPLIT_DRAW, "Split Draw"),
        (Product.SPLIT_PRINCIPAL, "Split Principal"),
        (Product.WHOLETAIL, "Wholetail"),
        (LoanPurpose.CASH_OUT, "Cash Out"),
        (LoanPurpose.PURCHASE, "Purchase"),
        (AssetType.SFR, "SFR"),
        (AssetType.UNITS_2_4, "Units 2–4"),
        (AssetType.UNITS_5_PLUS, "Units 5+"),
        (StatedExit.UNKNOWN, "Unknown"),
        (None, "—"),
    ],
)
def test_an_enum_a_person_reads_is_title_case_with_spaces(code: Any, label: str) -> None:
    assert enum_label(code) == label


def test_the_stored_value_is_untouched_by_any_of_it() -> None:
    assert Product.SPLIT_DRAW.value == "SPLIT_DRAW"
    assert AssetType.UNITS_2_4.value == "UNITS_2_4"


def test_every_product_has_a_one_line_definition() -> None:
    """SPEC §3, shown under the Loan Type box so a person is told what they are picking."""
    for product in Product:
        text = product_definition(product)
        assert text and text.endswith(".") and len(text) < 300, product
    assert product_definition(None) == ""


# --- and on the page itself -----------------------------------------------------------------------


@requires_db
def test_the_page_tags_a_defaulted_box_and_offers_a_reset(
    client: QueueClient, stored_deal: Deal
) -> None:
    """The tag says "this is the config's number"; the reset link puts it back."""
    body = client.get("/queue/new").text
    for name in ("contingency_pct", "origination_fee_pct", "closing_costs_usd"):
        assert f'data-default-tag="{name}"' in body, name
        assert re.search(rf'id="{name}"[^>]*data-default="[^"]+"', body), name
        assert f'data-reset="{name}"' in body, name
    # a box with no default carries neither
    assert 'data-default-tag="purchase_price"' not in body
    assert 'data-reset="purchase_price"' not in body
    # ...and the same treatment on the deal page's override block
    deal_page = client.get(f"/queue/deals/{stored_deal.id}").text
    assert 'data-default-tag="contingency_pct"' in deal_page
    assert 'data-reset="contingency_pct"' in deal_page


@requires_db
def test_the_masked_boxes_are_the_ones_the_script_formats(client: QueueClient) -> None:
    body = client.get("/queue/new").text
    for name in ("purchase_price", "loan_requested", "closing_costs_usd"):
        assert re.search(rf'id="{name}"[^>]*data-mask="money"', body), name
    for name in ("interest_rate", "contingency_pct", "origination_fee_pct"):
        assert re.search(rf'id="{name}"[^>]*data-mask="pct"', body), name
    assert re.search(r'id="borrower_phone"[^>]*data-mask="phone"', body)
    # the script is a convenience, not a control: the server parses either shape
    assert "Nothing here validates, fetches, or decides anything." not in body


@requires_db
def test_a_default_left_alone_is_not_recorded_as_a_team_entry(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The point of the whole arrangement (SPEC §8.1, §9.2)."""
    from tests.conftest import form_body

    body = form_body(team_entry)
    body["contingency_pct"] = "0%"  # exactly the config default, as the box showed it
    assert client.post("/intake/team", data=body, follow_redirects=False).status_code == 303

    deal = db_session.query(Deal).one()
    assert deal.contingency_pct is None, "a default the team left alone became a team entry"
