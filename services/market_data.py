"""What the property-data adapter said about a deal, and the asking of it.  # SPEC §6.2

The seam between ``adapters/rentcast.py`` and the deal. Three things live here and nowhere
else:

* **The pull.** ``fetch_market_data`` asks the adapter about the deal's address and writes
  one ``enrichment_runs`` row per call, raw body included (CLAUDE.md "Record everything").
  It runs at arrival on every channel, before the screen (``services/intake.create_deal``),
  so the as-is value and the market rent are on the first run, and again behind the deal
  page's Refresh market data button. A failure is a FAILED row and the run proceeds on
  whatever the deal already holds; nothing here raises for the remote.
* **The cache and the budget.** A second deal at the same address inside
  ``rentcast.cache_days`` reuses the stored runs as CACHED rows of its own rather than
  spending three more requests; ``properties`` is keyed by the normalized address and shared
  across deals (SPEC §5), so the property row is the cache key. The month's requests are
  counted off the rows (every row a request was actually sent for, OK or FAILED), and when
  the next pull would pass ``rentcast.monthly_request_budget`` it is skipped - one SKIPPED
  row, and ``MARKET_DATA_BUDGET_EXHAUSTED`` (Info) on the runs after it - rather than failed.
* **The reading.** ``market_data`` turns the latest rows into ``MarketData``: the AVM and its
  range with the sale comps, the rent and its range with the rental comps, the last sale
  and the sale history, when it was fetched and from where. ``services/enrichment.py``
  hands the engine the two values and the range; the deal page prints the rest.

What the pull writes onto the deal is deliberately little. The as-is value and the rent are
not copied onto ``deals`` - the adapter value wins at assembly time over the team's own
column (``services/assemble.py``, SPEC §6.1), and the team's column stays what it was. The
estimated sale price is never touched: the AVM is an as-is figure and the sale price is after
the repair, and ``SALE_PRICE_ABOVE_AVM_RANGE`` (Info) is what says when the two are further
apart than the rehab budget. The property record fills blanks on ``properties`` - beds,
baths, square feet, year built, units - and never overwrites a value a person entered.

Every pull is audited as ``MARKET_DATA_FETCHED`` against the actor who caused it (the intake's
actor at arrival, the person who pressed Refresh), naming what each call did and what it
filled. Nothing here commits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.base import Adapter, AdapterResult, AdapterStatus, Subject
from adapters.rentcast import (
    API_KEY_ENV,
    AVM_RENT,
    AVM_VALUE,
    PROPERTIES,
    SOURCE,
    SOURCES,
    RentCastAdapter,
)
from config.config import Config, get_config
from db.models import Deal, EnrichmentRun, Property
from schema.models import AuditAction
from services.audit import DEALS, jsonable, record_audit

SOURCE_LABEL = "RentCast"
# The rows that mean a request was sent, for the month's budget.
LIVE_STATUSES: tuple[str, ...] = (AdapterStatus.OK.value, AdapterStatus.FAILED.value)
# The rows that carry a usable body: a call that came back, or a copy of one.
VALUE_STATUSES: tuple[str, ...] = (AdapterStatus.OK.value, AdapterStatus.CACHED.value)
# Every row the adapter family writes: the three calls and the one SKIPPED marker.
ALL_SOURCES: tuple[str, ...] = (*SOURCES, SOURCE)
# One enrichment is this many requests; the budget check is made for all of them at once.
REQUESTS_PER_PULL = len(SOURCES)

# The property columns the record may fill, by the parsed key that fills each (SPEC §8.1).
FILLABLE: tuple[tuple[str, str], ...] = (
    ("beds", "bedrooms"),
    ("baths", "bathrooms"),
    ("sf", "square_footage"),
    ("year_built", "year_built"),
    ("units", "unit_count"),
)


class _Default:
    """The marker for "the adapter the environment has a key for" (``default_adapter``)."""


DEFAULT_ADAPTER = _Default()


def configured(environ: dict[str, str] | None = None) -> bool:
    """Whether a RentCast key is set, which is what turns the Refresh button on."""
    return bool((environ if environ is not None else os.environ).get(API_KEY_ENV, "").strip())


def default_adapter() -> Adapter | None:
    """The adapter the deployment is configured with; None without a key."""
    return RentCastAdapter.from_env()


# --- reading the rows ----------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Comparable:
    """One comp as the page's table shows it.  # SPEC §6.2"""

    address: str | None
    distance_miles: Decimal | None
    price: Decimal | None  # the asking price on a sale comp, the monthly rent on a rental one
    bedrooms: int | None
    bathrooms: Decimal | None
    square_footage: int | None
    date: date | None

    @classmethod
    def read(cls, comp: dict[str, Any]) -> Comparable:
        return cls(
            address=comp.get("address"),
            distance_miles=_decimal(comp.get("distance_miles")),
            price=_decimal(comp.get("price")),
            bedrooms=comp.get("bedrooms"),
            bathrooms=_decimal(comp.get("bathrooms")),
            square_footage=comp.get("square_footage"),
            date=_date(comp.get("date")),
        )


@dataclass(frozen=True)
class SaleEvent:
    """One line of the property's sale history."""

    date: date | None
    event: str | None
    price: Decimal | None


@dataclass(frozen=True)
class MarketData:
    """What the latest pull says about the deal's property, read for the engine and the page.

    ``fetched_at`` is when the rows in force were written for this deal; ``cached_from`` is
    the time of the pull they were copied from when they are CACHED rows, else None.
    ``errors`` names each call that failed, by source, in the words the adapter gave.
    ``budget_exhausted`` is true when the latest attempt was skipped for the budget and no
    value is in force, which is when the engine's flag says so.
    """

    fetched_at: datetime
    cached_from: datetime | None = None
    as_is_value: Decimal | None = None
    as_is_value_low: Decimal | None = None
    as_is_value_high: Decimal | None = None
    value_comps: list[Comparable] = field(default_factory=list)
    monthly_rent: Decimal | None = None
    monthly_rent_low: Decimal | None = None
    monthly_rent_high: Decimal | None = None
    rent_comps: list[Comparable] = field(default_factory=list)
    last_sale_price: Decimal | None = None
    last_sale_date: date | None = None
    sale_history: list[SaleEvent] = field(default_factory=list)
    property_type: str | None = None
    lot_size: int | None = None
    errors: dict[str, str] = field(default_factory=dict)
    budget_exhausted: bool = False
    source_label: str = SOURCE_LABEL

    @property
    def has_values(self) -> bool:
        return self.as_is_value is not None or self.monthly_rent is not None


def latest_runs(session: Session, deal_id: UUID) -> dict[str, EnrichmentRun]:
    """The newest row per source for one deal, SKIPPED marker included."""
    rows = session.scalars(
        select(EnrichmentRun)
        .where(EnrichmentRun.deal_id == deal_id, EnrichmentRun.source.in_(ALL_SOURCES))
        .order_by(EnrichmentRun.requested_at.desc(), EnrichmentRun.id.desc())
    )
    latest: dict[str, EnrichmentRun] = {}
    for row in rows:
        latest.setdefault(row.source, row)
    return latest


def _parsed(run: EnrichmentRun | None) -> dict[str, Any] | None:
    if run is None or run.status not in VALUE_STATUSES:
        return None
    return run.parsed_result if isinstance(run.parsed_result, dict) else None


def read_market_data(session: Session, deal: Deal) -> MarketData | None:
    """What is in force for the deal, or None when no pull was ever attempted for it."""
    latest = latest_runs(session, deal.id)
    if not latest:
        return None
    newest = max(latest.values(), key=lambda row: (row.requested_at, str(row.id)))
    prop = _parsed(latest.get(PROPERTIES))
    value = _parsed(latest.get(AVM_VALUE))
    rent = _parsed(latest.get(AVM_RENT))
    errors = {
        source: run.error or "failed"
        for source, run in latest.items()
        if run.status == AdapterStatus.FAILED.value and run.error is not None
    }
    # When the rows were copied, the copy says when the original was pulled.
    cached_from = None
    for run in latest.values():
        if run.status == AdapterStatus.CACHED.value and isinstance(run.parsed_result, dict):
            original = run.parsed_result.get("cached_from_at")
            if isinstance(original, str):
                try:
                    cached_from = datetime.fromisoformat(original)
                except ValueError:
                    cached_from = None
                break
    data = MarketData(
        fetched_at=newest.requested_at,
        cached_from=cached_from,
        as_is_value=_decimal(value.get("estimate")) if value else None,
        as_is_value_low=_decimal(value.get("low")) if value else None,
        as_is_value_high=_decimal(value.get("high")) if value else None,
        value_comps=[Comparable.read(comp) for comp in (value or {}).get("comparables", [])],
        monthly_rent=_decimal(rent.get("estimate")) if rent else None,
        monthly_rent_low=_decimal(rent.get("low")) if rent else None,
        monthly_rent_high=_decimal(rent.get("high")) if rent else None,
        rent_comps=[Comparable.read(comp) for comp in (rent or {}).get("comparables", [])],
        last_sale_price=_decimal(prop.get("last_sale_price")) if prop else None,
        last_sale_date=_date(prop.get("last_sale_date")) if prop else None,
        sale_history=[
            SaleEvent(_date(item.get("date")), item.get("event"), _decimal(item.get("price")))
            for item in (prop or {}).get("sale_history", [])
            if isinstance(item, dict)
        ],
        property_type=prop.get("property_type") if prop else None,
        lot_size=prop.get("lot_size") if prop else None,
        errors=errors,
        budget_exhausted=False,
    )
    skipped = newest.status == AdapterStatus.SKIPPED.value and not data.has_values
    return MarketData(**{**data.__dict__, "budget_exhausted": skipped})


# --- the pull ------------------------------------------------------------------------------------


class FetchOutcome(StrEnum):
    """What ``fetch_market_data`` did, for the notice and the audit row."""

    NOT_CONFIGURED = "NOT_CONFIGURED"  # no key: nothing written
    NO_ADDRESS = "NO_ADDRESS"  # the deal has no street address to ask about: nothing written
    CACHED = "CACHED"  # a pull at the same address inside the window was copied
    FETCHED = "FETCHED"  # three requests were sent
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # the month's budget would be passed: one SKIPPED row


@dataclass(frozen=True)
class Fetch:
    """One pull as it went, and what the deal now holds."""

    outcome: FetchOutcome
    data: MarketData | None = None
    filled: dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def wrote(self) -> bool:
        return self.outcome not in (FetchOutcome.NOT_CONFIGURED, FetchOutcome.NO_ADDRESS)

    def words(self) -> str:
        """One sentence for a redirect notice."""
        if self.outcome is FetchOutcome.NOT_CONFIGURED:
            return f"Market data is not configured: {API_KEY_ENV} is not set."
        if self.outcome is FetchOutcome.NO_ADDRESS:
            return "Market data was not pulled: the deal has no street address."
        if self.outcome is FetchOutcome.BUDGET_EXHAUSTED:
            return "Market data was not pulled: the month's RentCast request budget is exhausted."
        if self.outcome is FetchOutcome.CACHED:
            return "Market data reused from a recent pull at this address."
        if self.failed:
            named = ", ".join(sorted(self.failed))
            if len(self.failed) == REQUESTS_PER_PULL:
                return f"Market data could not be fetched ({named})."
            return f"Market data refreshed; {named} failed."
        return "Market data refreshed."


def subject_for(deal: Deal) -> Subject | None:
    """The deal's property as the adapter is asked about it; None without a street address."""
    prop = deal.property
    if prop is None or not prop.address_raw:
        return None
    units = prop.units
    property_type = None
    if units is not None and 2 <= units <= 4:
        property_type = "Multi-Family"
    elif units is not None and units >= 5:
        property_type = "Apartment"
    return Subject(
        address=prop.address_raw,
        bedrooms=prop.beds,
        bathrooms=prop.baths,
        square_footage=prop.sf,
        property_type=property_type,
    )


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def requests_this_month(session: Session, now: datetime) -> int:
    """How many requests the rows say were sent since the first of the month, UTC."""
    rows = session.scalars(
        select(EnrichmentRun.id).where(
            EnrichmentRun.source.in_(SOURCES),
            EnrichmentRun.status.in_(LIVE_STATUSES),
            EnrichmentRun.requested_at >= month_start(now),
        )
    )
    return len(list(rows))


def cached_runs(
    session: Session, deal: Deal, now: datetime, cache_days: int
) -> dict[str, EnrichmentRun]:
    """The latest OK row per source at this deal's address inside the window, any deal's.

    The property row is shared by every deal at the same normalized address (SPEC §5), so
    its id is the key; CACHED copies are not themselves reused, so the window is measured
    from a real pull.
    """
    if deal.property_id is None or cache_days <= 0:
        return {}
    rows = session.scalars(
        select(EnrichmentRun)
        .join(Deal, Deal.id == EnrichmentRun.deal_id)
        .where(
            Deal.property_id == deal.property_id,
            EnrichmentRun.source.in_(SOURCES),
            EnrichmentRun.status == AdapterStatus.OK.value,
            EnrichmentRun.requested_at >= now - timedelta(days=cache_days),
        )
        .order_by(EnrichmentRun.requested_at.desc(), EnrichmentRun.id.desc())
    )
    found: dict[str, EnrichmentRun] = {}
    for row in rows:
        found.setdefault(row.source, row)
    return found


def _write(session: Session, deal: Deal, result: AdapterResult) -> EnrichmentRun:
    row = EnrichmentRun(
        deal_id=deal.id,
        source=result.source,
        status=result.status.value,
        requested_at=result.requested_at,
        completed_at=result.completed_at,
        raw_response=result.raw,
        parsed_result=result.parsed,
        error=result.error,
    )
    session.add(row)
    return row


def _copy(session: Session, deal: Deal, original: EnrichmentRun, now: datetime) -> EnrichmentRun:
    """A CACHED row of the deal's own, naming the pull it was copied from."""
    parsed = dict(original.parsed_result) if isinstance(original.parsed_result, dict) else {}
    parsed["cached_from_run"] = str(original.id)
    parsed["cached_from_at"] = original.requested_at.isoformat()
    row = EnrichmentRun(
        deal_id=deal.id,
        source=original.source,
        status=AdapterStatus.CACHED.value,
        requested_at=now,
        completed_at=now,
        raw_response=original.raw_response,
        parsed_result=parsed,
        error=None,
    )
    session.add(row)
    return row


def fill_property_blanks(prop: Property, record: dict[str, Any]) -> dict[str, Any]:
    """Put the record's attributes where the property has none; never over a value.  # SPEC §6.2

    Returns what moved, keyed ``property.<column>`` with the value written, for the audit
    row. A column a person filled - on the form, on an edit - is left exactly as it was,
    whatever the record says.
    """
    moved: dict[str, Any] = {}
    for column, key in FILLABLE:
        if getattr(prop, column) is not None:
            continue
        value = record.get(key)
        if value is None:
            continue
        if column == "baths":
            value = _decimal(value)
            if value is None:
                continue
        setattr(prop, column, value)
        moved[f"property.{column}"] = jsonable(value)
    return moved


def fetch_market_data(
    session: Session,
    deal: Deal,
    *,
    actor: str,
    config: Config | None = None,
    adapter: Adapter | None | _Default = DEFAULT_ADAPTER,
    bypass_cache: bool = False,
    now: datetime | None = None,
    trigger: str = "arrival",
) -> Fetch:
    """Ask the adapter about the deal's address and record what it said.  # SPEC §6.2

    In order: nothing without a key or an address; a pull at the same address inside the
    window is copied unless ``bypass_cache`` (the Refresh button); a pull that would pass the
    month's budget is skipped with one SKIPPED row; else three requests, three rows, the
    property's blanks filled from the record. One audit row for anything that wrote.
    Flushes; no commit.
    """
    if isinstance(adapter, _Default):
        adapter = default_adapter()
    if adapter is None:
        return Fetch(FetchOutcome.NOT_CONFIGURED)
    subject = subject_for(deal)
    if subject is None:
        return Fetch(FetchOutcome.NO_ADDRESS)
    settings = config if config is not None else get_config()
    moment = now if now is not None else datetime.now(UTC)
    runs: dict[str, str] = {}
    filled: dict[str, Any] = {}
    failed: dict[str, str] = {}
    cached_from: str | None = None

    if not bypass_cache:
        reusable = cached_runs(session, deal, moment, settings.rentcast.cache_days)
    else:
        reusable = {}
    if reusable:
        outcome = FetchOutcome.CACHED
        for source in SOURCES:
            original = reusable.get(source)
            if original is None:
                continue
            _copy(session, deal, original, moment)
            runs[source] = AdapterStatus.CACHED.value
            cached_from = str(original.deal_id)
        record = _parsed(reusable.get(PROPERTIES))
    else:
        used = requests_this_month(session, moment)
        budget = settings.rentcast.monthly_request_budget
        if used + REQUESTS_PER_PULL > budget:
            outcome = FetchOutcome.BUDGET_EXHAUSTED
            _write(
                session,
                deal,
                AdapterResult(
                    source=SOURCE,
                    status=AdapterStatus.SKIPPED,
                    requested_at=moment,
                    completed_at=moment,
                    error=(
                        f"monthly request budget of {budget} exhausted: {used} sent this month, "
                        f"{REQUESTS_PER_PULL} more needed"
                    ),
                ),
            )
            runs[SOURCE] = AdapterStatus.SKIPPED.value
            record = None
        else:
            outcome = FetchOutcome.FETCHED
            results = adapter.enrich(subject)
            record = None
            for result in results:
                _write(session, deal, result)
                runs[result.source] = result.status.value
                if result.status is AdapterStatus.FAILED and result.error:
                    failed[result.source] = result.error
                if result.source == PROPERTIES and result.ok and result.parsed is not None:
                    record = result.parsed
    if record is not None and deal.property is not None:
        filled = fill_property_blanks(deal.property, record)
    session.flush()
    data = read_market_data(session, deal)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.MARKET_DATA_FETCHED,
        table_name=DEALS,
        row_id=deal.id,
        after={
            "trigger": trigger,
            "outcome": outcome.value,
            "source": SOURCE_LABEL,
            "runs": runs,
            "as_is_value": jsonable(data.as_is_value) if data else None,
            "monthly_rent": jsonable(data.monthly_rent) if data else None,
            **({"cached_from_deal": cached_from} if cached_from else {}),
            **({"filled": filled} if filled else {}),
            **({"failed": failed} if failed else {}),
        },
    )
    return Fetch(outcome, data=data, filled=filled, failed=failed)


def refresh_market_data(
    session: Session,
    deal: Deal,
    *,
    actor: str,
    config: Config | None = None,
    adapter: Adapter | None | _Default = DEFAULT_ADAPTER,
    now: datetime | None = None,
) -> Fetch:
    """The Refresh market data button: a fresh pull, cache or no cache, audited as a refresh."""
    return fetch_market_data(
        session,
        deal,
        actor=actor,
        config=config,
        adapter=adapter,
        bypass_cache=True,
        now=now,
        trigger="refresh",
    )


def market_snapshot(session: Session, deal: Deal) -> dict[str, Any] | None:
    """What a run read from the adapter, for the version's snapshot (SPEC §5): a record,
    not an input a restore puts back - a restore runs on the market data then in force."""
    data = read_market_data(session, deal)
    if data is None:
        return None
    return {
        "fetched_at": data.fetched_at.isoformat(),
        "cached_from": data.cached_from.isoformat() if data.cached_from else None,
        "as_is_value": jsonable(data.as_is_value),
        "as_is_value_low": jsonable(data.as_is_value_low),
        "as_is_value_high": jsonable(data.as_is_value_high),
        "monthly_rent": jsonable(data.monthly_rent),
        "monthly_rent_low": jsonable(data.monthly_rent_low),
        "monthly_rent_high": jsonable(data.monthly_rent_high),
        "budget_exhausted": data.budget_exhausted,
        "runs": {source: str(run.id) for source, run in latest_runs(session, deal.id).items()},
    }
