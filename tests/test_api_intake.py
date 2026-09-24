"""POST /intake/team stores the deal and returns the IntakeRecord.  # SPEC §4.2"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Borrower, Deal, Entity, IntakeSubmission, Property
from schema.models import (
    Channel,
    Product,
    ProductSource,
    State,
    StateSource,
    Status,
    Tranche,
)
from tests.conftest import requires_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"

pytestmark = requires_db


def test_complete_team_entry_is_stored(client: TestClient, db_session: Session) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    response = client.post("/intake/team", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "NEW"
    assert body["missing_fields"] == []
    assert body["borrower"]["phone"] == "7205550192"  # digits (SPEC §4.1)

    deal = db_session.get(Deal, body["id"])
    assert deal is not None
    assert deal.status is Status.NEW
    assert deal.channel is Channel.TEAM
    assert deal.purchase_price == Decimal("200000.00")
    assert deal.term_bucket is None and deal.term_months == 9
    assert deal.credit_range_self_reported is Tranche.T1
    assert deal.missing_fields == []
    assert deal.credit_authorization_signed is False
    assert deal.borrower is not None and deal.borrower.phone == "7205550192"
    assert [e.name for e in deal.borrower.entities] == ["Ortiz Builds LLC"]
    assert deal.property is not None and deal.property.state is State.CO
    assert deal.property.state_source is StateSource.INFERRED  # no state on the form
    assert deal.property.address_normalized == "3320 MEADE ST, DENVER, CO 80211"
    assert deal.monthly_rent is None and deal.estimated_sale_price_team is None
    assert deal.closing_date is not None and deal.interest_rate == Decimal("0.12000")
    assert len(deal.submissions) == 1
    assert deal.submissions[0].raw_payload["borrower_phone"] == "720-555-0192"


def test_partial_entry_is_needs_info(client: TestClient, db_session: Session) -> None:
    response = client.post("/intake/team", json={"address": "12 Elm St, Denver, CO 80202"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "NEEDS_INFO"
    assert body["missing_fields"][0] == "deal.purchase_price"
    deal = db_session.get(Deal, body["id"])
    assert deal is not None
    assert deal.borrower_id is None
    assert deal.property is not None and deal.property.state is State.CO
    assert db_session.scalar(select(func.count()).select_from(Borrower)) == 0


def test_repeat_inquiry_reuses_borrower_property_and_entity(
    client: TestClient, db_session: Session
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = client.post("/intake/team", json=payload)
    second = client.post("/intake/team", json={**payload, "borrower_phone": "(720) 555-0192"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert db_session.scalar(select(func.count()).select_from(Borrower)) == 1
    assert db_session.scalar(select(func.count()).select_from(Property)) == 1
    assert db_session.scalar(select(func.count()).select_from(Entity)) == 1
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 2
    assert db_session.scalar(select(func.count()).select_from(IntakeSubmission)) == 2


def test_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post("/intake/team", json={"fico": 720})
    assert response.status_code == 422


def test_bad_money_is_rejected(client: TestClient) -> None:
    response = client.post("/intake/team", json={"purchase_price": "100.123"})
    assert response.status_code == 422


def test_entered_state_and_the_team_economics_are_stored(
    client: TestClient, db_session: Session
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(
        {
            "state": "OK",
            "holding_costs_total_usd": "9600.00",
            "monthly_rent": "2500.00",
            "contingency_pct": "0.1",
        }
    )
    response = client.post("/intake/team", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["property"]["state_source"] == "ENTERED"
    assert body["deal"]["holding_costs_total_usd"] == "9600.00"
    deal = db_session.get(Deal, body["id"])
    assert deal is not None and deal.property is not None
    assert deal.property.state is State.OK
    assert deal.property.state_source is StateSource.ENTERED
    assert deal.holding_costs_total_usd == Decimal("9600.00")
    assert deal.monthly_rent == Decimal("2500.00")
    assert deal.contingency_pct == Decimal("0.10000")


def test_inferred_state_source_is_stored(client: TestClient, db_session: Session) -> None:
    response = client.post("/intake/team", json={"address": "12 Elm St, Denver, CO 80202"})
    assert response.status_code == 201, response.text
    deal = db_session.get(Deal, response.json()["id"])
    assert deal is not None and deal.property is not None
    assert deal.property.state is State.CO
    assert deal.property.state_source is StateSource.INFERRED


def test_product_inference_and_source_are_stored(client: TestClient, db_session: Session) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))  # rehab 42,000 -> SPLIT_DRAW
    inferred = client.post("/intake/team", json=payload)
    assert inferred.status_code == 201, inferred.text
    assert inferred.json()["deal"]["product"] == "SPLIT_DRAW"
    assert inferred.json()["deal"]["product_source"] == "INFERRED"
    deal = db_session.get(Deal, inferred.json()["id"])
    assert deal is not None
    assert deal.product is Product.SPLIT_DRAW
    assert deal.product_source is ProductSource.INFERRED

    # WHOLETAIL is one advance, so the fixture's split comes off with the product (SPEC §8.2)
    wholetail = {
        **payload,
        "product": "WHOLETAIL",
        "loan_purchase_portion": None,
        "loan_rehab_portion": None,
    }
    entered = client.post("/intake/team", json=wholetail)
    assert entered.status_code == 201, entered.text
    deal = db_session.get(Deal, entered.json()["id"])
    assert deal is not None
    assert deal.product is Product.WHOLETAIL
    assert deal.product_source is ProductSource.ENTERED


def test_product_without_source_is_rejected_by_the_database(db_session: Session) -> None:
    db_session.add(
        Deal(channel=Channel.TEAM, status=Status.NEW, product=Product.NO_DRAW, missing_fields=[])
    )
    with pytest.raises(IntegrityError, match="ck_deals_product_and_source_together"):
        db_session.flush()
    db_session.rollback()
