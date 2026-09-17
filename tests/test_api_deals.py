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
from schema.models import Status
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
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    stored_deal = deal_with_overrides
    response = client.post(f"/deals/{stored_deal.id}/underwrite", json=UNDERWRITE_BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["term_months"] == 9  # from the deal's term bucket
    assert body["rehab_months"] == 6
    assert body["engine_version"] == ENGINE_VERSION
    # no stated exit on this deal: 9 months on an SFR infers a resale (SPEC §3)
    assert body["exit"]["type"] == "FLIP" and body["exit"]["exit_source"] == "INFERRED"
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


def test_underwrite_route_requires_a_valuation(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    """Past the screen gate, a deal with no ARV anywhere is a 422 naming it."""
    stored_deal.status = Status.SCREENED
    db_session.flush()
    body = {key: value for key, value in UNDERWRITE_BODY.items() if key != "arv"}
    response = client.post(f"/deals/{stored_deal.id}/underwrite", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["missing"] == [
        "arv (no adapter value, none on the request, none on the deal)"
    ]


def test_read_deal_returns_the_deal_with_its_latest_screen_and_underwrite(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    before = client.get(f"/deals/{deal_id}")
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["screen"] is None and body["underwrite"] is None
    assert body["status"] == "NEW"
    assert body["asset_type"] == "SFR"
    assert body["stated_exit"] is None  # the exit is inferred, not stated (SPEC §3)
    assert body["term_bucket"] == "9"
    assert body["product"] == "SPLIT_DRAW" and body["product_source"] == "INFERRED"
    assert body["borrower"]["phone"] == "+19185550188"
    assert body["borrower"]["entities"] == ["Ellery Property Group LLC"]
    assert body["property"]["state"] == "OK"
    assert body["missing_fields"] == []
    # the team's own entries are on the read model, so the queue can show what a Go rests on
    assert D(body["as_is_value_team"]) == D("250000.00")
    assert D(body["arv_team"]) == D("295000.00")
    assert body["court_records_status"] == "CLEAN"
    assert body["court_records_as_of"] == "2026-09-16"
    assert body["court_records_team"] == []

    screened = client.post(f"/deals/{deal_id}/screen").json()
    assert screened["verdict"] == "GO"
    underwritten = client.post(f"/deals/{deal_id}/underwrite", json=UNDERWRITE_BODY).json()

    after = client.get(f"/deals/{deal_id}").json()
    assert after["screen"]["result"] == screened
    assert after["underwrite"]["result"] == underwritten
    assert after["screen"]["created_at"] and after["underwrite"]["created_at"]
    assert after["status"] == "UNDERWRITING"


def test_read_deal_shows_the_latest_of_each_run(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    client.post(f"/deals/{deal_id}/screen")
    second = client.post(
        f"/deals/{deal_id}/underwrite", json={**UNDERWRITE_BODY, "term_months": 6}
    ).json()
    third = client.post(
        f"/deals/{deal_id}/underwrite", json={**UNDERWRITE_BODY, "term_months": 15}
    ).json()
    assert second["term_months"] == 6 and third["term_months"] == 15

    body = client.get(f"/deals/{deal_id}").json()
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


def test_a_screen_moves_the_deal_along_the_lifecycle(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    assert client.get(f"/deals/{deal_with_overrides.id}").json()["status"] == "NEW"
    assert client.post(f"/deals/{deal_with_overrides.id}/screen").json()["verdict"] == "GO"
    assert client.get(f"/deals/{deal_with_overrides.id}").json()["status"] == "SCREENED"


def test_a_declined_screen_closes_the_deal(client: TestClient, stored_deal: Deal) -> None:
    """No valuation behind it, so it declines and the deal closes with it."""
    assert client.post(f"/deals/{stored_deal.id}/screen").json()["verdict"] == "DECLINE"
    assert client.get(f"/deals/{stored_deal.id}").json()["status"] == "DECLINED"


def test_underwriting_a_closed_deal_is_409_not_422(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """The inputs are fine; the status is what refuses, so it is a conflict, not a 422."""
    deal_with_overrides.status = Status.DEAD
    db_session.flush()
    response = client.post(f"/deals/{deal_with_overrides.id}/underwrite", json=UNDERWRITE_BODY)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "DEAD"
    assert "re-opens it" in detail["message"]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Underwrite)
            .where(Underwrite.deal_id == deal_with_overrides.id)
        )
        == 0
    )


def test_the_underwrite_route_falls_back_to_the_teams_valuation(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    """A request with no as-is value and no ARV still underwrites off the deal's own."""
    body = {
        key: value for key, value in UNDERWRITE_BODY.items() if key not in {"as_is_value", "arv"}
    }
    response = client.post(f"/deals/{deal_with_overrides.id}/underwrite", json=body)
    assert response.status_code == 201, response.text
    sizing = response.json()["sizing"]
    assert D(sizing["metrics"]["LTV_AS_IS"]["actual"]) == D("185000.00") / D("250000.00")
    assert sizing["as_is_value_source"] == "TEAM" and sizing["arv_source"] == "TEAM"


def test_the_underwrite_route_screens_an_unscreened_deal_first(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert client.get(f"/deals/{deal_id}").json()["screen"] is None
    assert client.post(f"/deals/{deal_id}/underwrite", json=UNDERWRITE_BODY).status_code == 201
    body = client.get(f"/deals/{deal_id}").json()
    assert body["screen"]["result"]["verdict"] == "GO"  # run on the way in, and recorded
    assert body["status"] == "UNDERWRITING"


def test_an_underwrite_that_screens_a_decline_is_409_and_keeps_the_screen(
    client: TestClient, stored_deal: Deal
) -> None:
    """No valuation behind it: the screen it runs on the way in declines, and that is kept."""
    response = client.post(f"/deals/{stored_deal.id}/underwrite", json=UNDERWRITE_BODY)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "DECLINED"
    assert "the screen it just ran declined it" in detail["message"]

    after = client.get(f"/deals/{stored_deal.id}").json()
    assert after["status"] == "DECLINED"
    assert after["screen"]["result"]["verdict"] == "DECLINE"
    assert after["underwrite"] is None
