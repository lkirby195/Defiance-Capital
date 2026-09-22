"""The purchase / rehab split of a split loan.  # SPEC §8.2

It used to be derived — the purchase portion was `commitment - rehab_adj`, and a team member
who disagreed typed one number into an `UnderwriteRequest` at run time. It is now two columns
on the deal, entered together on the team-entry form and adding up to the loan requested.

Three states, and keeping them apart is most of what is tested here:

* **entered** — a split product with both portions. The screen and the underwrite both use
  them; the rehab side is capped at the contingency-adjusted rehab budget.
* **not entered** — a split product with neither. That is a borrower-channel intake nobody
  has divided (SPEC §4.1, §4.2): it screens, sized on the loan requested with no split, and
  the underwrite refuses it by name.
* **forbidden** — a NO_DRAW or WHOLETAIL deal, which is one advance and carries no split at
  all. The model, the form and the database each refuse one.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cli.fixtures import run_fixture
from config.config import Config
from db.models import Deal
from intake.parsers.team_form import TeamEntryForm
from outputs.console import render_underwrite
from schema.models import (
    Channel,
    CommitmentSplit,
    DealInfo,
    Product,
    ProductSource,
    Status,
)
from services import DealNotReady, TeamOverrides, run_screen, save_overrides
from services.assemble import underwrite_inputs
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, requires_db, store_deal

pytestmark = requires_db

D = Decimal
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"
CONFIG = Config.load()


def payload(**overrides: Any) -> dict[str, Any]:
    """The complete team entry (a SPLIT_DRAW deal with a split), with changes."""
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def form_post(client: TestClient, data: dict[str, Any]) -> Any:
    body = {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in data.items()
    }
    return client.post("/intake/team", data=body, follow_redirects=False)


# --- what a coherent split is (the model) -------------------------------------------------------


def test_the_two_portions_have_to_add_up_to_the_loan() -> None:
    """A split is a division of the request, not a second opinion about its size."""
    with pytest.raises(ValidationError, match="must add up to the loan requested"):
        TeamEntryForm(**payload(loan_purchase_portion="143800.00", loan_rehab_portion="46200.01"))


def test_one_portion_without_the_other_is_refused() -> None:
    with pytest.raises(ValidationError, match="set together or not at all"):
        TeamEntryForm(**payload(loan_rehab_portion=None))


@pytest.mark.parametrize("product", ["NO_DRAW", "WHOLETAIL"])
def test_a_single_note_product_cannot_carry_a_split(product: str) -> None:
    with pytest.raises(ValidationError, match="applies only to"):
        TeamEntryForm(**payload(product=product))


def test_the_rule_is_tested_against_the_inferred_product_too() -> None:
    """The product box may be blank; the normalizer infers one from the rehab budget."""
    with pytest.raises(ValidationError, match="not NO_DRAW"):
        TeamEntryForm(**payload(rehab_budget="0"))  # inferred NO_DRAW, split still attached


def test_a_split_product_with_no_split_is_coherent() -> None:
    """A borrower-channel intake carries a loan amount and nothing about its shape."""
    form = TeamEntryForm(**payload(loan_purchase_portion=None, loan_rehab_portion=None))
    assert form.effective_product is Product.SPLIT_DRAW
    assert form.loan_purchase_portion is None


def test_deal_info_holds_the_same_line() -> None:
    """The record the normalizer produces is checked the same way the form is."""
    with pytest.raises(ValidationError, match="must add up to the loan requested"):
        DealInfo(
            product=Product.SPLIT_DRAW,
            product_source=ProductSource.INFERRED,
            loan_requested=D("100000.00"),
            loan_purchase_portion=D("60000.00"),
            loan_rehab_portion=D("50000.00"),
        )


# --- what the form insists on -------------------------------------------------------------------


def test_the_form_requires_both_portions_on_a_split_product(client: TestClient) -> None:
    response = form_post(client, payload(loan_purchase_portion=None, loan_rehab_portion=None))
    assert response.status_code == 422
    assert "Purchase portion is required on a SPLIT_DRAW loan." in response.text
    assert "Rehab portion is required on a SPLIT_DRAW loan." in response.text


def test_the_form_takes_a_split_that_adds_up(client: TestClient, db_session: Session) -> None:
    assert form_post(client, payload()).status_code == 303
    deal = db_session.query(Deal).one()
    assert deal.loan_purchase_portion == D("143800.00")
    assert deal.loan_rehab_portion == D("46200.00")


def test_the_form_says_so_when_a_split_does_not_add_up(client: TestClient) -> None:
    response = form_post(client, payload(loan_rehab_portion="50000.00"))
    assert response.status_code == 422
    assert "must add up to the loan requested" in response.text


def test_a_json_intake_may_still_arrive_without_one(
    client: TestClient, db_session: Session
) -> None:
    """The borrower channel: a loan amount, no shape.  # SPEC §4.2"""
    response = client.post(
        "/intake/team", json=payload(loan_purchase_portion=None, loan_rehab_portion=None)
    )
    assert response.status_code == 201, response.text
    deal = db_session.get(Deal, response.json()["id"])
    assert deal is not None
    assert deal.product is Product.SPLIT_DRAW
    assert deal.loan_purchase_portion is None


# --- and what the database insists on -----------------------------------------------------------


def test_half_a_split_is_refused_by_the_database(db_session: Session) -> None:
    db_session.add(
        Deal(
            channel=Channel.TEAM,
            status=Status.NEW,
            missing_fields=[],
            product=Product.SPLIT_DRAW,
            product_source=ProductSource.INFERRED,
            loan_purchase_portion=D("1.00"),
        )
    )
    with pytest.raises(IntegrityError, match="ck_deals_loan_split_set_together"):
        db_session.flush()
    db_session.rollback()


def test_a_split_on_a_deal_with_no_product_is_refused_by_the_database(db_session: Session) -> None:
    """`NULL IN (...)` is NULL and a CHECK passes on NULL, so the constraint tests presence."""
    db_session.add(
        Deal(
            channel=Channel.TEAM,
            status=Status.NEEDS_INFO,
            missing_fields=[],
            loan_purchase_portion=D("1.00"),
            loan_rehab_portion=D("2.00"),
        )
    )
    with pytest.raises(IntegrityError, match="ck_deals_loan_split_only_on_split_products"):
        db_session.flush()
    db_session.rollback()


# --- the screen runs without one; the underwrite does not ---------------------------------------


def undivided(session: Session) -> Deal:
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals")
        .joinpath("go_team_overrides_tulsa.json")
        .read_text(encoding="utf-8")
    )
    entry = dict(fixture["team_entry"])
    entry.pop("loan_purchase_portion", None)
    entry.pop("loan_rehab_portion", None)
    return store_deal(session, entry)


def test_an_undivided_deal_still_screens(db_session: Session) -> None:
    deal = undivided(db_session)
    result = run_screen(db_session, deal.id, actor=ACTOR)
    db_session.commit()
    assert result.sizing.split is None
    assert result.sizing.commitment == deal.loan_requested


def test_an_undivided_deal_is_refused_an_underwrite_by_name(db_session: Session) -> None:
    deal = undivided(db_session)
    with pytest.raises(DealNotReady) as refusal:
        underwrite_inputs(deal, UnderwriteRequest())
    named = " ".join(refusal.value.missing)
    assert "deal.loan_purchase_portion" in named
    assert "deal.loan_rehab_portion" in named
    assert "advanced in two parts" in named


def test_moving_off_a_split_product_takes_the_split_with_it(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A WHOLETAIL loan has no division, and the database would refuse a stale one."""
    assert deal_with_overrides.loan_purchase_portion is not None
    save_overrides(
        db_session,
        deal_with_overrides.id,
        TeamOverrides(product=Product.WHOLETAIL),
        actor=ACTOR,
    )
    db_session.commit()
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None
    assert deal.product is Product.WHOLETAIL
    assert deal.loan_purchase_portion is None and deal.loan_rehab_portion is None


# --- and it is shown wherever the commitment is -------------------------------------------------


def test_the_deal_page_shows_the_split(client: TestClient, deal_with_overrides: Deal) -> None:
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert "Loan split" in body
    assert "$138,800.00 purchase" in body


def test_the_sizing_table_and_grid_header_name_the_portions(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    client.post(f"/queue/deals/{deal_with_overrides.id}/underwrite", follow_redirects=False)
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert "Rehab holdback" in body  # SPLIT_DRAW names, not SPLIT_PRINCIPAL's
    assert "advanced at close + holdback" in body
    del db_session


def test_the_console_report_names_the_two_portions_and_the_grid_says_the_shape() -> None:
    """The CLI is where the math is checked by hand, so the shape belongs beside it."""
    fixtures = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
    run = run_fixture(fixtures / "go_split_principal_repeat_override.json", CONFIG, True)
    assert run.underwrite_result is not None
    text = "\n".join(render_underwrite(run.underwrite_result, CONFIG))
    assert "Principal Note" in text and "Tranche A" in text
    assert "Principal Note / Tranche A" in text  # the grid heading
    assert "$104,000.00 + $66,000.00" in text


def test_a_pre_0_8_0_stored_split_still_rebuilds() -> None:
    """``purchase_portion_overridden`` is gone; a row written under it is not.

    ``screens`` and ``underwrites`` are append-only (SPEC §5) and a stored result has to
    rebuild into the model that produced it. The retired field is dropped on the way in and
    the two that replaced it take the values the old row's numbers already describe.
    """
    old_row = {
        "purchase_portion": "104000.00",
        "rehab_portion": "66000.00",
        "purchase_portion_overridden": True,
    }
    split = CommitmentSplit.model_validate(old_row)
    assert split.purchase_portion == D("104000.00")
    assert split.rehab_portion == D("66000.00")
    assert split.rehab_portion_requested == D("66000.00")
    assert split.rehab_portion_capped is False
    assert not hasattr(split, "purchase_portion_overridden")


def test_a_current_row_still_has_to_carry_the_new_fields() -> None:
    """The shim is for old rows only; it does not make the new fields optional."""
    with pytest.raises(ValidationError):
        CommitmentSplit.model_validate({"purchase_portion": "1.00", "rehab_portion": "2.00"})
