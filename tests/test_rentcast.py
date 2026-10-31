"""The RentCast adapter and the market data it puts on a deal.  # SPEC §6.2

Three layers, each held to its own contract. The adapter (``adapters/rentcast.py``) runs
against recorded bodies for the three endpoints through an ``httpx.MockTransport`` - no
network - and is held to the header, the query parameters, the parsed shape and the rule
that a remote failure of any kind is a FAILED result and never an exception. The engine's
two Info flags (``engine/screen.market_data_flags``) are tested pure. The service
(``services/market_data.py``) is tested on the database: the arrival pull on both channels
with and without an adapter, the precedence of the AVM and the rent over the team's own, the
fill-blanks rule on the property row, the cache by address and its window, the month's
budget, the Refresh button and its audit row, failed calls as rows the run proceeds past, the
snapshot, and the borrower's pages showing none of it.

The one live test at the bottom runs only with ``RENTCAST_LIVE=1`` and a key in the
environment, against one known address, and asserts the shape of what comes back rather
than any value.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adapters.base import AdapterStatus, Subject
from adapters.rentcast import (
    API_KEY_ENV,
    AVM_RENT,
    AVM_VALUE,
    HEADER,
    PROPERTIES,
    SOURCES,
    RentCastAdapter,
)
from api.ratelimit import LIMITER
from config.config import DEFAULT_PATH, Config, load_yaml
from db.models import AuditLog, Deal, EnrichmentRun, Screen, Underwrite, Version
from engine.screen import market_data_flags
from schema.models import (
    AuditAction,
    MarketDataInputs,
    Product,
    ScreenFlag,
    SizingInputs,
    ValueSource,
)
from services import (
    DEALS,
    FetchOutcome,
    adapter_values,
    fetch_market_data,
    latest_screen,
    latest_underwrite,
    read_market_data,
    run_analysis,
    run_screen,
    screen_result,
    underwrite_readiness,
)
from services.assemble import underwrite_inputs
from services.market_data import fill_property_blanks, subject_for
from services.readiness import InputSource
from services.requests import UnderwriteRequest
from tests.conftest import (
    ACTOR,
    RENTCAST_LIVE_KEY,
    USER_EMAIL,
    QueueClient,
    form_body,
    requires_db,
    store_deal,
)
from tests.test_apply import VALID
from tests.test_autorun import notice

D = Decimal
CONFIG = Config.load()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/rentcast"
ADDRESS = "3320 Meade St, Denver, CO 80211"
T0 = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)

PATHS = {
    "/v1/properties": "properties",
    "/v1/avm/value": "avm_value",
    "/v1/avm/rent/long-term": "avm_rent",
}


def body(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Recorder:
    """A RentCast that answers from the recorded bodies and remembers what it was asked.

    ``overrides`` maps a path to a response, or to an exception to raise in place of one,
    so a test can make one endpoint fail while the other two answer.
    """

    def __init__(self, overrides: dict[str, httpx.Response | Exception] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        override = self.overrides.get(request.url.path)
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        return httpx.Response(200, json=body(PATHS[request.url.path]))

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def fake(
    overrides: dict[str, httpx.Response | Exception] | None = None, when: datetime = T0
) -> tuple[RentCastAdapter, Recorder]:
    recorder = Recorder(overrides)
    adapter = RentCastAdapter(
        "test-key", transport=httpx.MockTransport(recorder), clock=lambda: when
    )
    return adapter, recorder


def yaml_data() -> dict[str, Any]:
    data: dict[str, Any] = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    return data


def config_with(**rentcast: int) -> Config:
    data = yaml_data()
    data["rentcast"] = {**data["rentcast"], **rentcast}
    return Config.from_dict(data)


SUBJECT = Subject(ADDRESS, bedrooms=3, bathrooms=D("2.0"), square_footage=1420)


# --- the adapter, against recorded bodies (no network) -------------------------------------------


def test_three_calls_carry_the_key_header_and_the_subjects_attributes() -> None:
    adapter, recorder = fake()
    results = adapter.enrich(SUBJECT)
    assert [result.source for result in results] == list(SOURCES)
    assert all(result.status is AdapterStatus.OK for result in results)
    assert recorder.paths() == list(PATHS)
    for request in recorder.requests:
        assert request.headers[HEADER] == "test-key"
        assert request.url.params["address"] == ADDRESS
    # the record is asked by address alone; the two AVMs get the attributes the deal has
    assert dict(recorder.requests[0].url.params) == {"address": ADDRESS}
    for request in recorder.requests[1:]:
        params = dict(request.url.params)
        assert params == {
            "address": ADDRESS,
            "bedrooms": "3",
            "bathrooms": "2",
            "squareFootage": "1420",
        }
    # timestamps come off the clock the adapter was given
    assert all(result.requested_at == T0 and result.completed_at == T0 for result in results)


def test_an_attribute_the_deal_lacks_is_left_off_and_a_property_type_is_passed_when_known() -> None:
    adapter, recorder = fake()
    adapter.enrich(Subject(ADDRESS, bathrooms=D("2.5"), property_type="Multi-Family"))
    params = dict(recorder.requests[1].url.params)
    assert params == {"address": ADDRESS, "bathrooms": "2.5", "propertyType": "Multi-Family"}
    adapter.enrich(Subject(ADDRESS, property_type="Mansion"))
    assert "propertyType" not in recorder.requests[4].url.params


def test_the_property_record_is_read_into_the_deals_terms() -> None:
    adapter, _ = fake()
    record = adapter.enrich(SUBJECT)[0]
    assert record.parsed == {
        "formatted_address": ADDRESS,
        "property_type": "Single Family",
        "bedrooms": 3,
        "bathrooms": "2.0",
        "square_footage": 1420,
        "lot_size": 6250,
        "year_built": 1948,
        "unit_count": 1,
        "county": "Denver",
        "assessor_id": "02191-04-021-000",
        "owner_occupied": False,
        "last_sale_price": "212000.00",
        "last_sale_date": "2019-06-14",
        "sale_history": [
            {"date": "2019-06-14", "event": "Sale", "price": "212000.00"},
            {"date": "2011-03-02", "event": "Sale", "price": "148500.00"},
        ],
    }
    assert record.raw == body("properties")  # the raw body is kept whole


def test_the_two_estimates_are_read_the_same_way() -> None:
    adapter, _ = fake()
    _, value, rent = adapter.enrich(SUBJECT)
    assert value.parsed is not None and rent.parsed is not None
    assert (value.parsed["estimate"], value.parsed["low"], value.parsed["high"]) == (
        "236000.00",
        "214000.00",
        "258000.00",
    )
    assert (rent.parsed["estimate"], rent.parsed["low"], rent.parsed["high"]) == (
        "2450.00",
        "2250.00",
        "2650.00",
    )
    assert value.parsed["subject_address"] == ADDRESS
    assert len(value.parsed["comparables"]) == 2 and len(rent.parsed["comparables"]) == 1
    assert value.parsed["comparables"][0] == {
        "address": "3415 Lowell Blvd, Denver, CO 80211",
        "distance_miles": "0.24",
        "price": "249900.00",
        "bedrooms": 3,
        "bathrooms": "2.0",
        "square_footage": 1465,
        "year_built": 1951,
        "date": "2026-09-18",
        "status": "Active",
        "correlation": "0.9871",
    }


def test_a_404_is_a_failed_result_with_rentcasts_message_and_not_an_exception() -> None:
    adapter, _ = fake({"/v1/avm/value": httpx.Response(404, json=body("not_found"))})
    record, value, rent = adapter.enrich(SUBJECT)
    assert record.status is AdapterStatus.OK and rent.status is AdapterStatus.OK
    assert value.status is AdapterStatus.FAILED and value.parsed is None
    assert value.error == "HTTP 404: No results"
    assert value.raw == body("not_found")


@pytest.mark.parametrize(
    ("response", "starts"),
    [
        (httpx.ConnectTimeout("timed out"), "ConnectTimeout"),
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.Response(500, text="boom"), "HTTP 500: "),
        (
            httpx.Response(
                429,
                json={
                    "status": 429,
                    "error": "auth/rate-limit-exceeded",
                    "message": "The rate limit of 20 requests per second has been exceeded",
                },
            ),
            "HTTP 429: The rate limit",
        ),
        (httpx.Response(200, text="<html>"), "HTTP 200: the body is not JSON"),
        (httpx.Response(200, json={"unexpected": True}), "unexpected response shape"),
        (httpx.Response(200, json=[]), "unexpected response shape"),
    ],
)
def test_every_kind_of_remote_failure_is_a_failed_result(
    response: httpx.Response | Exception, starts: str
) -> None:
    adapter, _ = fake({"/v1/properties": response})
    record = adapter.enrich(SUBJECT)[0]
    assert record.status is AdapterStatus.FAILED
    assert record.error is not None and record.error.startswith(starts), record.error


def test_the_adapter_comes_from_the_environment_or_not_at_all() -> None:
    assert RentCastAdapter.from_env({}) is None
    assert RentCastAdapter.from_env({API_KEY_ENV: "  "}) is None
    assert RentCastAdapter.from_env({API_KEY_ENV: "k"}) is not None
    with pytest.raises(ValueError):
        RentCastAdapter("")


# --- the two flags, pure (SPEC §6.2) --------------------------------------------------------------


def sizing(price: str | None) -> SizingInputs:
    return SizingInputs(
        product=Product.ACQUISITION,
        purchase_price=D("200000.00"),
        rehab_costs=D("48000.00"),
        loan_requested=D("195000.00"),
        estimated_sale_price=None if price is None else D(price),
        estimated_sale_price_source=None if price is None else ValueSource.TEAM,
    )


RANGE = MarketDataInputs(as_is_value_low=D("214000.00"), as_is_value_high=D("258000.00"))


def test_a_sale_price_above_the_avm_high_plus_the_rehab_is_flagged_and_both_sides_are_shown() -> (
    None
):
    (flag,) = market_data_flags(sizing("320000.00"), RANGE, CONFIG)
    assert flag.code is ScreenFlag.SALE_PRICE_ABOVE_AVM_RANGE and flag.severity.value == "INFO"
    assert flag.message == (
        "Estimated sale price $320,000.00 is above the AVM range high $258,000.00 plus rehab "
        "costs $48,000.00 ($306,000.00)."
    )


def test_a_sale_price_inside_the_rehab_of_the_avm_high_is_not() -> None:
    assert market_data_flags(sizing("306000.00"), RANGE, CONFIG) == []
    assert market_data_flags(sizing(None), RANGE, CONFIG) == []
    assert market_data_flags(sizing("320000.00"), MarketDataInputs(), CONFIG) == []
    assert market_data_flags(sizing("320000.00"), None, CONFIG) == []


def test_an_exhausted_budget_is_flagged_and_names_the_budget() -> None:
    (flag,) = market_data_flags(sizing(None), MarketDataInputs(budget_exhausted=True), CONFIG)
    assert flag.code is ScreenFlag.MARKET_DATA_BUDGET_EXHAUSTED
    assert "request budget of 50 for the month is exhausted" in flag.message


# --- the service, on the database (SPEC §6.2) -----------------------------------------------------


def runs(session: Session, deal: Deal) -> list[EnrichmentRun]:
    return list(
        session.scalars(
            select(EnrichmentRun)
            .where(EnrichmentRun.deal_id == deal.id)
            .order_by(EnrichmentRun.requested_at, EnrichmentRun.source)
        )
    )


def statuses(session: Session, deal: Deal) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for run in runs(session, deal):
        out.setdefault(run.source, []).append(run.status)
    return out


def audits(session: Session, deal: Deal) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.table_name == DEALS,
                AuditLog.row_id == str(deal.id),
                AuditLog.action == AuditAction.MARKET_DATA_FETCHED.value,
            )
            .order_by(AuditLog.created_at)
        )
    )


def one_deal(session: Session) -> Deal:
    (deal,) = list(session.scalars(select(Deal)))
    return deal


def count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def use(monkeypatch: pytest.MonkeyPatch, adapter: RentCastAdapter | None) -> None:
    """Make ``adapter`` the one the deployment is configured with, key and all."""
    monkeypatch.setattr("services.market_data.default_adapter", lambda: adapter)
    if adapter is None:
        monkeypatch.delenv(API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(API_KEY_ENV, "test-key")


@requires_db
def test_a_team_entry_is_enriched_on_arrival_and_the_first_run_reads_it(
    client: QueueClient,
    db_session: Session,
    team_entry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, recorder = fake()
    use(monkeypatch, adapter)
    # a sale price the AVM does not support: $400,000 against a $258,000 high plus $48,000
    posted = form_body(team_entry, estimated_sale_price_team="400000.00")
    response = client.post("/intake/team", data=posted, follow_redirects=False)
    assert response.status_code == 303, response.text
    deal = one_deal(db_session)
    assert recorder.paths() == list(PATHS)
    assert statuses(db_session, deal) == {source: ["OK"] for source in SOURCES}
    # the pull is before the run: the first underwrite already ran on the AVM and the rent
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 1
    row = latest_underwrite(db_session, deal.id)
    assert row is not None
    inputs = row.inputs
    assert (inputs["as_is_value"], inputs["as_is_value_source"]) == ("236000.00", "ADAPTER")
    assert (inputs["monthly_rent"], inputs["monthly_rent_source"]) == ("2450.00", "ADAPTER")
    assert inputs["market_data"] == {
        "as_is_value_low": "214000.00",
        "as_is_value_high": "258000.00",
        "monthly_rent_low": "2250.00",
        "monthly_rent_high": "2650.00",
        "budget_exhausted": False,
    }
    # the sale price is the team's still, and the flag says the AVM does not reach it
    assert D(inputs["deal"]["estimated_sale_price"]) == D("400000.00")
    assert inputs["deal"]["estimated_sale_price_source"] == "TEAM"
    screen_row = latest_screen(db_session, deal.id)
    assert screen_row is not None
    flags = {flag.code.value: flag.message for flag in screen_result(screen_row).flags}
    assert flags["SALE_PRICE_ABOVE_AVM_RANGE"] == (
        "Estimated sale price $400,000.00 is above the AVM range high $258,000.00 plus rehab "
        "costs $48,000.00 ($306,000.00)."
    )
    # readiness says where the two values came from
    rows = {
        row.key: row for row in underwrite_readiness(deal, adapter_values(db_session, deal)).rows
    }
    assert (rows["as_is_value"].value, rows["as_is_value"].source) == (
        D("236000.00"),
        InputSource.ADAPTER,
    )
    assert (rows["monthly_rent"].value, rows["monthly_rent"].source) == (
        D("2450.00"),
        InputSource.ADAPTER,
    )
    # one audit row, the intake's actor, at arrival
    (audit,) = audits(db_session, deal)
    assert audit.actor == USER_EMAIL and audit.after is not None
    assert audit.after["trigger"] == "arrival" and audit.after["outcome"] == "FETCHED"
    assert audit.after["runs"] == {source: "OK" for source in SOURCES}
    assert (audit.after["as_is_value"], audit.after["monthly_rent"]) == ("236000.00", "2450.00")
    # the version's snapshot records what the run read
    (version,) = list(db_session.scalars(select(Version)))
    market = version.inputs_snapshot["market_data"]
    assert (market["as_is_value"], market["monthly_rent"]) == ("236000.00", "2450.00")
    assert set(market["runs"]) == set(SOURCES)


@requires_db
def test_the_deal_page_shows_the_market_data_card(
    client: QueueClient,
    db_session: Session,
    team_entry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = fake()
    use(monkeypatch, adapter)
    assert (
        client.post("/intake/team", data=form_body(team_entry), follow_redirects=False).status_code
        == 303
    )
    deal = one_deal(db_session)
    page = client.get(f"/queue/deals/{deal.id}").text
    card = page[page.index('id="market-data"') : page.index('id="scored-as"')]
    assert "<h2>Market data</h2>" in card
    assert f'action="/queue/deals/{deal.id}/market-data"' in card
    assert ">Refresh market data</button>" in card
    for shown in (
        "<dt>AVM value</dt><dd>$236,000.00</dd>",
        "$214,000.00 – $258,000.00",
        "<dt>Market rent</dt><dd>$2,450.00</dd>",
        "$2,250.00 – $2,650.00",
        "$212,000.00 on Jun-14-2019",
        "<dt>Source</dt>",
        "RentCast",
        "2026-10-31 15:00",
        'Sale comps <span class="count">(2)</span>',
        'Rental comps <span class="count">(1)</span>',
        'Sale history <span class="count">(2)</span>',
        "3415 Lowell Blvd, Denver, CO 80211",
        "3240 Osceola St, Denver, CO 80212",
        "Sep-18-2026",
        "$148,500.00",
    ):
        assert shown in card, shown


@requires_db
def test_without_an_adapter_the_arrival_runs_as_before(
    client: QueueClient,
    db_session: Session,
    team_entry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, None)
    assert (
        client.post("/intake/team", data=form_body(team_entry), follow_redirects=False).status_code
        == 303
    )
    deal = one_deal(db_session)
    assert runs(db_session, deal) == [] and audits(db_session, deal) == []
    assert read_market_data(db_session, deal) is None
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 1
    row = latest_underwrite(db_session, deal.id)
    assert row is not None
    assert row.inputs["as_is_value"] is None and row.inputs["market_data"] is None
    page = client.get(f"/queue/deals/{deal.id}").text
    card = page[page.index('id="market-data"') : page.index('id="scored-as"')]
    assert "No market data." in card
    assert 'title="RENTCAST_API_KEY is not set">Refresh market data</span>' in card
    # the button is off, and the post behind it writes nothing and says why
    response = client.post(f"/queue/deals/{deal.id}/market-data", follow_redirects=False)
    assert response.status_code == 303
    assert notice(response) == "Market data is not configured: RENTCAST_API_KEY is not set."
    assert runs(db_session, deal) == []


@requires_db
def test_a_web_submission_is_enriched_too_and_the_borrower_sees_none_of_it(
    anon_client: QueueClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    LIMITER.reset()
    adapter, recorder = fake()
    use(monkeypatch, adapter)
    response = anon_client.post("/apply", data=VALID, follow_redirects=False)
    assert response.status_code == 303, response.text
    deal = one_deal(db_session)
    assert recorder.requests[0].url.params["address"] == "1412 S Peoria Ave, Tulsa, OK 74119" or (
        recorder.requests[0].url.params["address"].startswith("1412 S Peoria Ave")
    )
    assert statuses(db_session, deal) == {source: ["OK"] for source in SOURCES}
    (audit,) = audits(db_session, deal)
    assert audit.actor.startswith("web:")
    link = response.headers["location"]
    for view in (link, f"{link}&view=advanced"):
        page = anon_client.get(view).text
        for word in ("RentCast", "Market data", "AVM", "$236,000", "$2,450", "comps", "Last sale"):
            assert word not in page, (view, word)


@requires_db
def test_the_adapter_beats_the_teams_own_and_overwrites_nothing(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(
        db_session,
        {**team_entry, "estimated_sale_price_team": "300000.00", "monthly_rent": "2000.00"},
    )
    deal.as_is_value = D("250000.00")
    db_session.flush()
    adapter, _ = fake()
    fetch = fetch_market_data(db_session, deal, actor=ACTOR, adapter=adapter)
    assert fetch.outcome is FetchOutcome.FETCHED and fetch.failed == {}
    db_session.refresh(deal)
    # the team's columns stand exactly as they were...
    assert deal.as_is_value == D("250000.00") and deal.monthly_rent == D("2000.00")
    assert deal.estimated_sale_price_team == D("300000.00")
    # ...and the engine runs on the adapter's two, with the sale price still the team's
    inputs = underwrite_inputs(deal, UnderwriteRequest(), adapter_values(db_session, deal), CONFIG)
    assert (inputs.as_is_value, inputs.as_is_value_source) == (D("236000.00"), ValueSource.ADAPTER)
    assert (inputs.monthly_rent, inputs.monthly_rent_source) == (D("2450.00"), ValueSource.ADAPTER)
    assert (inputs.deal.estimated_sale_price, inputs.deal.estimated_sale_price_source) == (
        D("300000.00"),
        ValueSource.TEAM,
    )
    # a request's own as-is value is the team's newest and still loses to the adapter
    newer = underwrite_inputs(
        deal,
        UnderwriteRequest(as_is_value=D("260000.00")),
        adapter_values(db_session, deal),
        CONFIG,
    )
    assert newer.as_is_value == D("236000.00")
    # the flag is not raised: $300,000 is inside the $258,000 high plus $48,000 of rehab
    result = run_screen(db_session, deal.id, CONFIG, actor=ACTOR)
    assert "SALE_PRICE_ABOVE_AVM_RANGE" not in {flag.code.value for flag in result.flags}
    assert "TEAM_SOURCED_VALUES" in {flag.code.value for flag in result.flags}


@requires_db
def test_the_record_fills_blanks_on_the_property_and_never_a_value_a_person_entered(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    blank = {
        key: value
        for key, value in team_entry.items()
        if key not in ("beds", "baths", "sf", "year_built", "units")
    }
    deal = store_deal(db_session, blank)
    prop = deal.property
    assert prop is not None and prop.beds is None and prop.units is None
    prop.sf = 1500  # the one a person typed
    db_session.flush()
    adapter, _ = fake()
    fetch = fetch_market_data(db_session, deal, actor=ACTOR, adapter=adapter)
    db_session.refresh(prop)
    assert (prop.beds, prop.baths, prop.sf, prop.year_built, prop.units) == (
        3,
        D("2.0"),
        1500,
        1948,
        1,
    )
    assert fetch.filled == {
        "property.beds": 3,
        "property.baths": "2.0",
        "property.year_built": 1948,
        "property.units": 1,
    }
    (audit,) = audits(db_session, deal)
    assert audit.after is not None and audit.after["filled"] == fetch.filled
    # the pure rule, once more on a full property: nothing moves
    assert fill_property_blanks(prop, {"bedrooms": 9, "square_footage": 1, "unit_count": 4}) == {}
    assert (prop.beds, prop.sf, prop.units) == (3, 1500, 1)


@requires_db
def test_a_second_deal_at_the_same_address_reuses_the_pull_inside_the_window(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    adapter, recorder = fake()
    first = store_deal(db_session, team_entry)
    assert (
        fetch_market_data(db_session, first, actor=ACTOR, adapter=adapter, now=T0).outcome
        is FetchOutcome.FETCHED
    )
    assert len(recorder.requests) == 3
    # the same address, a different borrower: the property row is shared (SPEC §5)
    second = store_deal(
        db_session, {**team_entry, "borrower_name": "Nia Park", "borrower_phone": "720-555-0100"}
    )
    assert second.property_id == first.property_id
    later = T0 + timedelta(days=29)
    fetch = fetch_market_data(db_session, second, actor=ACTOR, adapter=adapter, now=later)
    assert fetch.outcome is FetchOutcome.CACHED
    assert len(recorder.requests) == 3  # nothing was sent
    assert statuses(db_session, second) == {source: ["CACHED"] for source in SOURCES}
    data = read_market_data(db_session, second)
    assert data is not None and data.as_is_value == D("236000.00") and data.cached_from == T0
    assert data.fetched_at == later
    (audit,) = audits(db_session, second)
    assert audit.after is not None and audit.after["outcome"] == "CACHED"
    assert audit.after["cached_from_deal"] == str(first.id)
    assert audit.after["runs"] == {source: "CACHED" for source in SOURCES}
    # the engine reads a cached pull exactly as a live one
    values = adapter_values(db_session, second)
    assert values.as_is_value == D("236000.00") and values.monthly_rent == D("2450.00")
    # outside the window it is a miss, and so is a different address, and so is a refresh
    third = store_deal(
        db_session, {**team_entry, "borrower_name": "Omar Reyes", "borrower_phone": "720-555-0101"}
    )
    assert (
        fetch_market_data(
            db_session, third, actor=ACTOR, adapter=adapter, now=T0 + timedelta(days=31)
        ).outcome
        is FetchOutcome.FETCHED
    )
    assert len(recorder.requests) == 6
    elsewhere = store_deal(
        db_session,
        {
            **team_entry,
            "address": "12 Elm St, Denver, CO 80202",
            "borrower_name": "Ada Lin",
            "borrower_phone": "720-555-0102",
        },
    )
    assert (
        fetch_market_data(db_session, elsewhere, actor=ACTOR, adapter=adapter, now=T0).outcome
        is FetchOutcome.FETCHED
    )
    assert len(recorder.requests) == 9
    assert (
        fetch_market_data(
            db_session, second, actor=ACTOR, adapter=adapter, now=T0, bypass_cache=True
        ).outcome
        is FetchOutcome.FETCHED
    )
    assert len(recorder.requests) == 12
    # a zero-day window caches nothing
    fourth = store_deal(
        db_session, {**team_entry, "borrower_name": "Bo Chen", "borrower_phone": "720-555-0103"}
    )
    assert (
        fetch_market_data(
            db_session,
            fourth,
            actor=ACTOR,
            adapter=adapter,
            now=T0,
            config=config_with(cache_days=0),
        ).outcome
        is FetchOutcome.FETCHED
    )


@requires_db
def test_the_months_budget_skips_the_pull_and_the_run_says_so(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    adapter, recorder = fake()
    tight = config_with(monthly_request_budget=3)
    first = store_deal(db_session, team_entry)
    assert (
        fetch_market_data(
            db_session, first, actor=ACTOR, adapter=adapter, config=tight, now=T0
        ).outcome
        is FetchOutcome.FETCHED
    )
    other = store_deal(
        db_session,
        {
            **team_entry,
            "address": "12 Elm St, Denver, CO 80202",
            "borrower_name": "Ada Lin",
            "borrower_phone": "720-555-0102",
        },
    )
    fetch = fetch_market_data(
        db_session, other, actor=ACTOR, adapter=adapter, config=tight, now=T0 + timedelta(hours=1)
    )
    assert fetch.outcome is FetchOutcome.BUDGET_EXHAUSTED
    assert len(recorder.requests) == 3
    assert (
        fetch.words()
        == "Market data was not pulled: the month's RentCast request budget is exhausted."
    )
    (skipped,) = runs(db_session, other)
    assert (skipped.source, skipped.status) == ("rentcast", "SKIPPED")
    assert (
        skipped.error == "monthly request budget of 3 exhausted: 3 sent this month, 3 more needed"
    )
    data = read_market_data(db_session, other)
    assert data is not None and data.budget_exhausted and not data.has_values
    values = adapter_values(db_session, other)
    assert values.as_is_value is None and values.market_data is not None
    assert values.market_data.budget_exhausted
    result = run_screen(db_session, other.id, tight, actor=ACTOR)
    assert "MARKET_DATA_BUDGET_EXHAUSTED" in {flag.code.value for flag in result.flags}
    # a cached address is served before the budget is asked
    same = store_deal(
        db_session, {**team_entry, "borrower_name": "Nia Park", "borrower_phone": "720-555-0100"}
    )
    assert (
        fetch_market_data(
            db_session,
            same,
            actor=ACTOR,
            adapter=adapter,
            config=tight,
            now=T0 + timedelta(hours=2),
        ).outcome
        is FetchOutcome.CACHED
    )
    # a new month starts the count over; a failed call still counted toward the old one
    assert (
        fetch_market_data(
            db_session,
            other,
            actor=ACTOR,
            adapter=adapter,
            config=tight,
            now=datetime(2026, 11, 1, tzinfo=UTC),
            bypass_cache=True,
        ).outcome
        is FetchOutcome.FETCHED
    )
    assert len(recorder.requests) == 6
    refreshed = read_market_data(db_session, other)
    assert (
        refreshed is not None
        and not refreshed.budget_exhausted
        and refreshed.as_is_value == D("236000.00")
    )


@requires_db
def test_the_refresh_button_pulls_again_past_the_cache_and_runs(
    client: QueueClient,
    db_session: Session,
    team_entry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, recorder = fake()
    use(monkeypatch, adapter)
    assert (
        client.post("/intake/team", data=form_body(team_entry), follow_redirects=False).status_code
        == 303
    )
    deal = one_deal(db_session)
    assert len(recorder.requests) == 3 and count(db_session, Version) == 1
    response = client.post(f"/queue/deals/{deal.id}/market-data", follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(f"/queue/deals/{deal.id}?version=2")
    assert notice(response).startswith("Market data refreshed. Analysis recorded: ")
    assert notice(response).endswith("Saved as Auto v2 (Working).")
    assert len(recorder.requests) == 6  # the cache was not consulted
    assert statuses(db_session, deal) == {source: ["OK", "OK"] for source in SOURCES}
    arrival, refresh = audits(db_session, deal)
    assert refresh.actor == USER_EMAIL and refresh.after is not None
    assert (refresh.after["trigger"], refresh.after["outcome"]) == ("refresh", "FETCHED")
    assert arrival.after is not None and arrival.after["trigger"] == "arrival"
    assert count(db_session, Version) == 2
    # a refresh whose value call fails says which, and the page prints the error
    # a later clock: the rows in force are the newest by requested_at
    failing, _ = fake(
        {"/v1/avm/value": httpx.Response(404, json=body("not_found"))},
        when=T0 + timedelta(hours=1),
    )
    use(monkeypatch, failing)
    response = client.post(f"/queue/deals/{deal.id}/market-data", follow_redirects=False)
    assert notice(response).startswith(
        "Market data refreshed; rentcast/avm/value failed. Analysis recorded"
    )
    page = client.get(f"/queue/deals/{deal.id}").text
    assert "<dt>rentcast/avm/value</dt><dd>HTTP 404: No results</dd>" in page
    assert (
        "<dt>AVM value</dt><dd>—</dd>" in page and "<dt>Market rent</dt><dd>$2,450.00</dd>" in page
    )


@requires_db
def test_failed_calls_are_rows_the_run_proceeds_past(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    adapter, _ = fake(
        {
            "/v1/properties": httpx.ConnectTimeout("timed out"),
            "/v1/avm/value": httpx.Response(500, text="boom"),
            "/v1/avm/rent/long-term": httpx.Response(404, json=body("not_found")),
        }
    )
    deal = store_deal(db_session, team_entry)
    fetch = fetch_market_data(db_session, deal, actor=ACTOR, adapter=adapter)
    assert fetch.outcome is FetchOutcome.FETCHED
    assert set(fetch.failed) == set(SOURCES)
    assert fetch.words() == (
        "Market data could not be fetched (rentcast/avm/rent/long-term, rentcast/avm/value, "
        "rentcast/properties)."
    )
    assert statuses(db_session, deal) == {source: ["FAILED"] for source in SOURCES}
    data = read_market_data(db_session, deal)
    assert data is not None and not data.has_values and not data.budget_exhausted
    assert data.errors[AVM_VALUE] == "HTTP 500: Internal Server Error"
    assert data.errors[AVM_RENT] == "HTTP 404: No results"
    assert data.errors[PROPERTIES].startswith("ConnectTimeout")
    values = adapter_values(db_session, deal)
    assert values.as_is_value is None and values.monthly_rent is None
    assert values.market_data == MarketDataInputs()
    run = run_analysis(db_session, deal, actor=ACTOR, config=CONFIG)
    assert run.ran and run.underwrite is not None
    codes = {flag.code.value for flag in run.screen.flags} if run.screen else set()
    assert not codes & {"SALE_PRICE_ABOVE_AVM_RANGE", "MARKET_DATA_BUDGET_EXHAUSTED"}


def test_the_subject_is_the_deals_property_and_a_unit_count_names_the_type(
    team_entry: dict[str, Any],
) -> None:
    from db.repository import transient_deal
    from intake.normalize import normalize
    from intake.parsers.team_form import TeamEntryForm, parse_team_form
    from schema.models import Channel

    def deal_for(**changes: Any) -> Deal:
        payload = {**team_entry, **changes}
        record = normalize(
            parse_team_form(TeamEntryForm(**payload)), Channel.TEAM, raw_payload=payload
        )
        return transient_deal(record)

    assert subject_for(deal_for()) == Subject(ADDRESS, 3, D("2.0"), 1420, None)
    assert subject_for(deal_for(units=3)) is not None
    assert subject_for(deal_for(units=3)).property_type == "Multi-Family"  # type: ignore[union-attr]
    assert subject_for(deal_for(units=12)).property_type == "Apartment"  # type: ignore[union-attr]


# --- live, by request only ------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RENTCAST_LIVE") != "1" or not (RENTCAST_LIVE_KEY or "").strip(),
    reason="set RENTCAST_LIVE=1 and RENTCAST_API_KEY for the live smoke test (3 billed requests)",
)
def test_live_smoke_one_known_address_comes_back_in_shape() -> None:
    adapter = RentCastAdapter.from_env({API_KEY_ENV: RENTCAST_LIVE_KEY or ""})
    assert adapter is not None
    record, value, rent = adapter.enrich(Subject("5500 Grand Lake Dr, San Antonio, TX 78244"))
    for result in (record, value, rent):
        assert result.status is AdapterStatus.OK, (result.source, result.error)
        assert result.parsed is not None and result.raw is not None
    assert record.parsed is not None and value.parsed is not None and rent.parsed is not None
    assert isinstance(record.parsed["sale_history"], list)
    assert record.parsed["formatted_address"]
    for estimate in (value.parsed, rent.parsed):
        assert D(estimate["estimate"]) > 0
        assert estimate["low"] is None or D(estimate["low"]) <= D(estimate["estimate"])
        assert estimate["high"] is None or D(estimate["high"]) >= D(estimate["estimate"])
        assert isinstance(estimate["comparables"], list) and estimate["comparables"]
        comp = estimate["comparables"][0]
        assert set(comp) == {
            "address",
            "distance_miles",
            "price",
            "bedrooms",
            "bathrooms",
            "square_footage",
            "year_built",
            "date",
            "status",
            "correlation",
        }
        assert comp["date"] is None or date.fromisoformat(comp["date"])
