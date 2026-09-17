"""POST /deals/{id}/screen, POST /deals/{id}/underwrite, GET /deals/{id}.  # SPEC §7, §8, §9.1"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.main import app
from db.models import Deal, Screen, Underwrite
from db.session import get_session
from engine.version import ENGINE_VERSION
from tests.conftest import requires_db

pytestmark = requires_db

D = Decimal
MISSING_UUID = "00000000-0000-0000-0000-000000000000"

UNDERWRITE_BODY: dict[str, Any] = {
    "as_is_value": "230000.00",
    "arv": "260000.00",
    "verified_credit_score": 715,
    "verified_deals_36mo": 4,
    "market_rent_monthly": "1800.00",
    "annual_utilities_usd": "720.00",
}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_screen_route_stores_a_row_and_returns_the_result(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    response = client.post(f"/deals/{stored_deal.id}/screen")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["engine_version"] == ENGINE_VERSION
    assert body["verdict"] in {"GO", "CONDITIONAL", "DECLINE"}
    assert body["reasons"] and len(body["reasons"]) == len(body["flags"])
    assert body["suggested_reply"]
    # money and rates cross the wire as exact decimal strings, not floats
    assert body["sizing"]["commitment"] == "190000.00"
    assert D(body["sizing"]["buy_closing"]) == D("5550.00")  # 3% of the 185,000 price

    row = db_session.scalar(select(Screen).where(Screen.deal_id == stored_deal.id))
    assert row is not None and row.verdict.value == body["verdict"]


def test_screen_route_is_append_only(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    for _ in range(3):
        assert client.post(f"/deals/{stored_deal.id}/screen").status_code == 201
    count = db_session.scalar(
        select(func.count()).select_from(Screen).where(Screen.deal_id == stored_deal.id)
    )
    assert count == 3


def test_underwrite_route_takes_the_spec_8_1_inputs(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    response = client.post(f"/deals/{stored_deal.id}/underwrite", json=UNDERWRITE_BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["term_months"] == 9  # from the deal's term bucket
    assert body["rehab_months"] == 6
    assert body["engine_version"] == ENGINE_VERSION
    assert body["exit"]["type"] == "FLIP" and body["exit"]["exit_source"] == "STATED"
    assert body["grid_lender"]["rows"]
    assert D(body["lender_yield_at_solve"]).quantize(D("0.000001")) == D("0.175000")

    row = db_session.scalar(select(Underwrite).where(Underwrite.deal_id == stored_deal.id))
    assert row is not None
    assert row.solved_rate == D(body["solved_rate"]).quantize(D("0.00001"))
    assert "grid_borrower" not in row.__table__.columns  # dropped in migration 0004


def test_underwrite_route_rejects_a_body_it_does_not_recognise(
    client: TestClient, stored_deal: Deal
) -> None:
    response = client.post(
        f"/deals/{stored_deal.id}/underwrite", json={**UNDERWRITE_BODY, "cap_rate": "0.07"}
    )
    assert response.status_code == 422


def test_underwrite_route_requires_a_valuation(client: TestClient, stored_deal: Deal) -> None:
    body = {key: value for key, value in UNDERWRITE_BODY.items() if key != "arv"}
    assert client.post(f"/deals/{stored_deal.id}/underwrite", json=body).status_code == 422


def test_read_deal_returns_the_deal_with_its_latest_screen_and_underwrite(
    client: TestClient, stored_deal: Deal
) -> None:
    before = client.get(f"/deals/{stored_deal.id}")
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["screen"] is None and body["underwrite"] is None
    assert body["asset_type"] == "SFR"
    assert body["stated_exit"] == "FLIP"
    assert body["term_bucket"] == "9"
    assert body["product"] == "SPLIT_DRAW" and body["product_source"] == "INFERRED"
    assert body["borrower"]["phone"] == "+19185550142"
    assert body["borrower"]["entities"] == ["Whitfield Holdings LLC"]
    assert body["property"]["state"] == "OK"
    assert body["missing_fields"] == []

    screened = client.post(f"/deals/{stored_deal.id}/screen").json()
    underwritten = client.post(f"/deals/{stored_deal.id}/underwrite", json=UNDERWRITE_BODY).json()

    after = client.get(f"/deals/{stored_deal.id}").json()
    assert after["screen"]["result"] == screened
    assert after["underwrite"]["result"] == underwritten
    assert after["screen"]["created_at"] and after["underwrite"]["created_at"]


def test_read_deal_shows_the_latest_of_each_run(client: TestClient, stored_deal: Deal) -> None:
    client.post(f"/deals/{stored_deal.id}/screen")
    second = client.post(
        f"/deals/{stored_deal.id}/underwrite", json={**UNDERWRITE_BODY, "term_months": 6}
    ).json()
    third = client.post(
        f"/deals/{stored_deal.id}/underwrite", json={**UNDERWRITE_BODY, "term_months": 15}
    ).json()
    assert second["term_months"] == 6 and third["term_months"] == 15

    body = client.get(f"/deals/{stored_deal.id}").json()
    assert body["underwrite"]["result"]["term_months"] == 15
    assert body["underwrite"]["result"] == third


def test_unknown_deal_is_404_on_every_route(client: TestClient) -> None:
    assert client.get(f"/deals/{MISSING_UUID}").status_code == 404
    assert client.post(f"/deals/{MISSING_UUID}/screen").status_code == 404
    response = client.post(f"/deals/{MISSING_UUID}/underwrite", json=UNDERWRITE_BODY)
    assert response.status_code == 404


def test_an_incomplete_deal_is_422_naming_what_is_missing(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    stored_deal.purchase_price = None
    db_session.flush()
    response = client.post(f"/deals/{stored_deal.id}/screen")
    assert response.status_code == 422
    assert response.json()["detail"]["missing"] == ["deal.purchase_price"]
