"""POST /intake/team stores the deal and returns the IntakeRecord.  # SPEC §4.2"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.main import app
from db.models import Borrower, Deal, Entity, IntakeSubmission, Property
from db.session import get_session
from schema.models import Channel, State, Status, TermBucket, Tranche
from tests.conftest import requires_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"

pytestmark = requires_db


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_complete_team_entry_is_stored(client: TestClient, db_session: Session) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    response = client.post("/intake/team", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "NEW"
    assert body["missing_fields"] == []
    assert body["borrower"]["phone"] == "+19185550142"

    deal = db_session.get(Deal, body["id"])
    assert deal is not None
    assert deal.status is Status.NEW
    assert deal.channel is Channel.TEAM
    assert deal.purchase_price == Decimal("185000.00")
    assert deal.term_bucket is TermBucket.M9
    assert deal.credit_range_self_reported is Tranche.T2
    assert deal.missing_fields == []
    assert deal.credit_authorization_signed is False
    assert deal.borrower is not None and deal.borrower.phone == "+19185550142"
    assert [e.name for e in deal.borrower.entities] == ["Whitfield Holdings LLC"]
    assert deal.property is not None and deal.property.state is State.OK
    assert deal.property.address_normalized == "1412 S CHEYENNE AVE, TULSA, OK 74119"
    assert len(deal.submissions) == 1
    assert deal.submissions[0].raw_payload["borrower_phone"] == "(918) 555-0142"


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
    second = client.post("/intake/team", json={**payload, "borrower_phone": "918-555-0142"})
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
