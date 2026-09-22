"""The Underwrite inputs checklist, and the button it governs.  # SPEC §8.1

The Run underwrite button takes no form, so a person pressing it is betting on what the deal
already holds. The checklist is that bet made visible: one row per SPEC §8.1 input, the value
in force, and where it came from.

The rule that matters is the last one here: ``readiness.missing`` and the ``DealNotReady``
the run raises are derived from the same place (``services/assemble.py``), so a disabled
button and the refusal behind it cannot name different things. A page that says a deal is
ready and a run that then refuses it is worse than no checklist at all.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from db.models import Deal
from schema.models import CourtRecordsStatus, Product, TermBucket
from services import DealNotReady, InputSource, underwrite_readiness
from services.assemble import underwrite_inputs
from services.enrichment import AdapterValues
from services.requests import UnderwriteRequest
from tests.conftest import requires_db

pytestmark = requires_db

D = Decimal


def rows(deal: Deal, adapters: AdapterValues | None = None) -> dict[str, Any]:
    readiness = (
        underwrite_readiness(deal) if adapters is None else underwrite_readiness(deal, adapters)
    )
    return {row.key: row for row in readiness.rows}


# --- one row per input, with where it came from -------------------------------------------------


def test_every_spec_8_1_input_has_a_row(deal_with_overrides: Deal) -> None:
    keys = set(rows(deal_with_overrides))
    assert {
        "as_is_value",
        "arv",
        "term_months",
        "market_rent_monthly",
        "annual_taxes_usd",
        "annual_insurance_usd",
        "annual_utilities_usd",
        "verified_credit_score",
        "court_records",
        "asset_type",
        "stated_exit",
    } <= keys


def test_a_hand_entered_value_says_team(deal_with_overrides: Deal) -> None:
    row = rows(deal_with_overrides)["as_is_value"]
    assert row.source is InputSource.TEAM
    assert row.value == deal_with_overrides.as_is_value_team
    assert row.required is True


def test_an_adapter_value_beats_the_team_and_says_so(deal_with_overrides: Deal) -> None:
    """SPEC §6.1 precedence, shown rather than only applied."""
    pulled = AdapterValues(as_is_value=D("999000.00"))
    row = rows(deal_with_overrides, pulled)["as_is_value"]
    assert row.source is InputSource.ADAPTER
    assert row.value == D("999000.00")


def test_an_opex_line_nobody_entered_says_default_and_shows_the_number(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.actual_annual_utilities_usd = None
    db_session.commit()
    row = rows(deal_with_overrides)["annual_utilities_usd"]
    assert row.source is InputSource.DEFAULT
    assert row.required is False
    assert row.value == deal_with_overrides.as_is_value_team * D("0.004")
    assert "config default" in row.note


def test_the_market_rent_has_no_default_and_says_what_it_costs(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.market_rent_monthly = None
    db_session.commit()
    row = rows(deal_with_overrides)["market_rent_monthly"]
    assert row.source is InputSource.MISSING
    assert row.required is False, "optional: the run goes ahead without a takeout"
    assert "DSCR takeout is not evaluated" in row.note


def test_a_split_product_lists_its_two_portions_by_their_real_names(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.product = Product.SPLIT_PRINCIPAL
    db_session.commit()
    by_key = rows(deal_with_overrides)
    assert by_key["deal.loan_purchase_portion"].label == "Principal Note"
    assert by_key["deal.loan_rehab_portion"].label == "Tranche A"
    assert by_key["deal.loan_purchase_portion"].required is True


def test_a_single_note_product_lists_no_portions(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.product = Product.NO_DRAW
    deal_with_overrides.loan_purchase_portion = None
    deal_with_overrides.loan_rehab_portion = None
    db_session.commit()
    assert "deal.loan_purchase_portion" not in rows(deal_with_overrides)


def test_a_not_checked_court_search_is_missing_rather_than_clean(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """SPEC §7.2: the checklist must not let NOT_CHECKED read as a clean bill."""
    deal_with_overrides.court_records_status = CourtRecordsStatus.NOT_CHECKED
    deal_with_overrides.court_records_as_of = None
    db_session.commit()
    row = rows(deal_with_overrides)["court_records"]
    assert row.source is InputSource.MISSING
    assert "not clean" in row.note


# --- ready, and what is missing -----------------------------------------------------------------


def test_a_complete_deal_is_ready(deal_with_overrides: Deal) -> None:
    readiness = underwrite_readiness(deal_with_overrides)
    assert readiness.ready is True
    assert readiness.missing == []


@pytest.mark.parametrize(
    "clear,expected",
    [
        (["as_is_value_team"], "As-is value"),
        (["arv_team"], "ARV"),
        (["loan_purchase_portion", "loan_rehab_portion"], "Purchase portion"),
    ],
)
def test_a_required_input_that_is_absent_turns_the_button_off(
    db_session: Session, deal_with_overrides: Deal, clear: list[str], expected: str
) -> None:
    for column in clear:
        setattr(deal_with_overrides, column, None)
    db_session.commit()
    readiness = underwrite_readiness(deal_with_overrides)
    assert readiness.ready is False
    assert expected in readiness.missing


def test_a_term_bucket_that_names_no_months_turns_it_off_too(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """12+ is a team decision; without it there is no term to price.  # SPEC §8.1"""
    deal_with_overrides.term_bucket = TermBucket.M12_PLUS
    db_session.commit()
    readiness = underwrite_readiness(deal_with_overrides)
    assert readiness.ready is False
    assert "Term (months)" in readiness.missing


def test_an_intake_gap_is_named_the_way_the_refusal_names_it(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.purchase_price = None
    db_session.commit()
    assert "deal.purchase_price" in underwrite_readiness(deal_with_overrides).missing


@pytest.mark.parametrize(
    "clear",
    [
        ["as_is_value_team"],
        ["arv_team"],
        ["loan_purchase_portion", "loan_rehab_portion"],
        ["purchase_price"],
    ],
)
def test_the_page_and_the_run_agree_about_what_is_missing(
    db_session: Session, deal_with_overrides: Deal, clear: list[str]
) -> None:
    """The point of the whole module: one rule, two readers."""
    for column in clear:
        setattr(deal_with_overrides, column, None)
    db_session.commit()

    assert underwrite_readiness(deal_with_overrides).ready is False
    with pytest.raises(DealNotReady):
        underwrite_inputs(deal_with_overrides, UnderwriteRequest())


def test_a_deal_the_page_calls_ready_actually_assembles(deal_with_overrides: Deal) -> None:
    """The other half: ready has to mean the run goes through."""
    assert underwrite_readiness(deal_with_overrides).ready is True
    assembled = underwrite_inputs(deal_with_overrides, UnderwriteRequest())
    assert assembled.deal.as_is_value is not None and assembled.deal.arv is not None


# --- the page ------------------------------------------------------------------------------------


def page(client: TestClient, deal: Deal) -> str:
    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200
    return response.text


def test_the_deal_page_shows_the_checklist(client: TestClient, deal_with_overrides: Deal) -> None:
    body = page(client, deal_with_overrides)
    assert "Underwrite inputs" in body
    for label in ("As-is value", "ARV", "Market rent (monthly)", "Annual utilities"):
        assert label in body, label
    assert "TEAM" in body


def test_the_button_is_live_on_a_ready_deal(client: TestClient, deal_with_overrides: Deal) -> None:
    body = page(client, deal_with_overrides)
    assert ">Run underwrite</button>" in body
    assert "disabled>Run underwrite" not in body
    assert "Run underwrite is off until these are entered" not in body


def test_the_button_is_off_and_the_page_says_why(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.arv_team = None
    db_session.commit()
    body = page(client, deal_with_overrides)
    assert "disabled>Run underwrite</button>" in body
    assert "Run underwrite is off until these are entered" in body
    assert "ARV" in body
