"""RentCast: the property record, the value estimate and the long-term rent estimate.  # SPEC §6.2

The first adapter (Phase 3, 9a). One enrichment of a deal is three GETs against
``https://api.rentcast.io/v1`` (https://developers.rentcast.io), each keyed by the address
and authenticated with the ``X-Api-Key`` header:

    /properties?address=...            the public record: attributes, last sale, sale history
    /avm/value?address=...             the automated valuation and its range, with sale comps
    /avm/rent/long-term?address=...    the rent estimate and its range, with rental comps

The two AVM calls also carry the bedrooms, bathrooms, square footage and property type when
the deal has them (``Subject``), which RentCast uses in place of its own lookup of the
subject's attributes. Every call is its own ``AdapterResult`` and its own ``enrichment_runs``
row, raw body kept, so a later reader can see exactly what RentCast said rather than what
was read into it. Nothing here touches the database or the deal; ``services/market_data.py``
does the asking, the caching, the budget and the writing.

Nothing here raises for a remote failure. A timeout, a connection error, any 4xx or 5xx
(RentCast answers 404 when no record matches the address, 400 when it cannot geolocate it,
401 on a bad key, 429 at 20 requests a second), a body that is not JSON and a body that is
not the documented shape each come back as ``FAILED`` with the reason in words. The remote
is given ``TIMEOUT`` seconds and no retry: an enrichment sits in front of the arrival run
and a borrower is waiting on the other end of it.

Money is parsed to a string of two decimals and dates to ``YYYY-MM-DD`` so the parsed result
is JSON the way the raw body is; the service turns them back into ``Decimal`` and ``date``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from adapters.base import AdapterResult, AdapterStatus, Subject

SOURCE = "rentcast"
API_KEY_ENV = "RENTCAST_API_KEY"
HEADER = "X-Api-Key"
BASE_URL = "https://api.rentcast.io/v1"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# The three rows one enrichment writes, keyed as ``enrichment_runs.source`` holds them.
PROPERTIES = "rentcast/properties"
AVM_VALUE = "rentcast/avm/value"
AVM_RENT = "rentcast/avm/rent/long-term"
SOURCES: tuple[str, ...] = (PROPERTIES, AVM_VALUE, AVM_RENT)
_PATHS: dict[str, str] = {
    PROPERTIES: "/properties",
    AVM_VALUE: "/avm/value",
    AVM_RENT: "/avm/rent/long-term",
}

# The property types RentCast's AVMs accept (developers.rentcast.io/reference/property-types);
# the service maps a unit count onto them where the deal gives one.
PROPERTY_TYPES: frozenset[str] = frozenset(
    {"Single Family", "Condo", "Townhouse", "Manufactured", "Multi-Family", "Apartment", "Land"}
)

CENT = Decimal("0.01")


# --- reading one body ----------------------------------------------------------------------------


def money(value: Any) -> str | None:
    """A JSON number as a string of two decimals; None when absent or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)).quantize(CENT))
    except (InvalidOperation, ValueError):
        return None


def number(value: Any, places: int = 4) -> str | None:
    """A JSON number as a Decimal string at ``places`` decimals (a distance, a correlation)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))
    except (InvalidOperation, ValueError):
        return None


def whole(value: Any) -> int | None:
    """A JSON number as an int (bedrooms, square footage, a year); None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def baths(value: Any) -> str | None:
    """Bathrooms at one decimal, as ``properties.baths`` holds them (``2.5``)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.1")))
    except (InvalidOperation, ValueError):
        return None


def day(value: Any) -> str | None:
    """``2024-11-18T00:00:00.000Z`` as ``2024-11-18``; None when absent or unreadable."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _record(body: Any) -> Mapping[str, Any]:
    """The one record a body holds: the first of an array, or the object itself."""
    if isinstance(body, list):
        if not body:
            raise ValueError("the response holds no record")
        body = body[0]
    if not isinstance(body, Mapping):
        raise ValueError("the response is not a record")
    return body


def parse_property(body: Any) -> dict[str, Any]:
    """The property record, read down to what the deal and the page use.  # SPEC §6.2

    ``/properties?address=`` answers with an array even for one address; the first record
    is the match. The sale history is RentCast's ``history`` object keyed by date, read into
    a list newest first; the last sale is its own two fields on the record.
    """
    record = _record(body)
    if text(record.get("formattedAddress")) is None:
        raise ValueError("the record has no formattedAddress")
    features = record.get("features")
    if not isinstance(features, Mapping):
        features = {}
    history: list[dict[str, Any]] = []
    events = record.get("history")
    if isinstance(events, Mapping):
        for key, event in events.items():
            if not isinstance(event, Mapping):
                continue
            history.append(
                {
                    "date": day(event.get("date")) or day(key),
                    "event": text(event.get("event")),
                    "price": money(event.get("price")),
                }
            )
    history.sort(key=lambda item: item["date"] or "", reverse=True)
    return {
        "formatted_address": text(record.get("formattedAddress")),
        "property_type": text(record.get("propertyType")),
        "bedrooms": whole(record.get("bedrooms")),
        "bathrooms": baths(record.get("bathrooms")),
        "square_footage": whole(record.get("squareFootage")),
        "lot_size": whole(record.get("lotSize")),
        "year_built": whole(record.get("yearBuilt")),
        "unit_count": whole(features.get("unitCount")),
        "county": text(record.get("county")),
        "assessor_id": text(record.get("assessorID")),
        "owner_occupied": (
            record.get("ownerOccupied") if isinstance(record.get("ownerOccupied"), bool) else None
        ),
        "last_sale_price": money(record.get("lastSalePrice")),
        "last_sale_date": day(record.get("lastSaleDate")),
        "sale_history": history,
    }


def _comparable(comp: Mapping[str, Any]) -> dict[str, Any]:
    """One comp as the page's table shows it: where, how far, what it asked, its size, when."""
    return {
        "address": text(comp.get("formattedAddress")),
        "distance_miles": number(comp.get("distance"), 2),
        "price": money(comp.get("price")),
        "bedrooms": whole(comp.get("bedrooms")),
        "bathrooms": baths(comp.get("bathrooms")),
        "square_footage": whole(comp.get("squareFootage")),
        "year_built": whole(comp.get("yearBuilt")),
        "date": day(comp.get("listedDate")) or day(comp.get("lastSeenDate")),
        "status": text(comp.get("status")),
        "correlation": number(comp.get("correlation")),
    }


def _parse_estimate(body: Any, field: str) -> dict[str, Any]:
    """An AVM body: the estimate, its range and its comps.  # SPEC §6.2

    ``field`` is ``price`` on ``/avm/value`` and ``rent`` on ``/avm/rent/long-term``; the
    range is ``<field>RangeLow`` / ``<field>RangeHigh``. The parsed result keys all three the
    same way whichever it was, so one reader serves both.
    """
    record = _record(body)
    estimate = money(record.get(field))
    if estimate is None:
        raise ValueError(f"the response has no {field}")
    comps_in = record.get("comparables")
    comps = [
        _comparable(comp)
        for comp in (comps_in if isinstance(comps_in, list) else [])
        if isinstance(comp, Mapping)
    ]
    subject = record.get("subjectProperty")
    return {
        "estimate": estimate,
        "low": money(record.get(f"{field}RangeLow")),
        "high": money(record.get(f"{field}RangeHigh")),
        "subject_address": (
            text(subject.get("formattedAddress")) if isinstance(subject, Mapping) else None
        ),
        "comparables": comps,
    }


def parse_value(body: Any) -> dict[str, Any]:
    return _parse_estimate(body, "price")


def parse_rent(body: Any) -> dict[str, Any]:
    return _parse_estimate(body, "rent")


_PARSERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    PROPERTIES: parse_property,
    AVM_VALUE: parse_value,
    AVM_RENT: parse_rent,
}


def _error_message(status_code: int, body: Any, fallback: str) -> str:
    """``HTTP 404: No results`` - the status and RentCast's own message when it sent one."""
    detail = fallback
    if isinstance(body, Mapping):
        message = body.get("message") or body.get("error")
        if isinstance(message, str) and message:
            detail = message
    return f"HTTP {status_code}: {detail}".strip()


# --- the adapter ---------------------------------------------------------------------------------


class RentCastAdapter:
    """Three GETs per subject, each its own result; never raises on the remote.  # SPEC §6.2

    ``transport`` is for the tests: an ``httpx.MockTransport`` serving recorded bodies makes
    the adapter run end to end without a network. ``clock`` is the time source for the
    timestamps on each result, so a test can pin them.
    """

    source = SOURCE

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: httpx.Timeout = TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(f"RentCast needs an API key ({API_KEY_ENV})")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RentCastAdapter | None:
        """The adapter the deployment has a key for, or None when ``RENTCAST_API_KEY`` is unset."""
        key = (environ if environ is not None else os.environ).get(API_KEY_ENV, "").strip()
        return cls(key) if key else None

    def enrich(self, subject: Subject) -> list[AdapterResult]:
        """The property record, the value estimate and the rent estimate, in that order."""
        attributes = self._attributes(subject)
        with httpx.Client(
            base_url=self._base_url,
            headers={HEADER: self._api_key, "Accept": "application/json"},
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            return [
                self._call(client, PROPERTIES, {"address": subject.address}),
                self._call(client, AVM_VALUE, {"address": subject.address, **attributes}),
                self._call(client, AVM_RENT, {"address": subject.address, **attributes}),
            ]

    @staticmethod
    def _attributes(subject: Subject) -> dict[str, str]:
        """The subject's attributes as query parameters, each only when the deal has it."""
        params: dict[str, str] = {}
        if subject.bedrooms is not None:
            params["bedrooms"] = str(subject.bedrooms)
        if subject.bathrooms is not None:
            params["bathrooms"] = format(subject.bathrooms.normalize(), "f")
        if subject.square_footage is not None:
            params["squareFootage"] = str(subject.square_footage)
        if subject.property_type in PROPERTY_TYPES:
            params["propertyType"] = str(subject.property_type)
        return params

    def _call(self, client: httpx.Client, source: str, params: dict[str, str]) -> AdapterResult:
        """One GET, read into a result whatever happens."""
        requested_at = self._clock()

        def failed(error: str, raw: Any = None) -> AdapterResult:
            return AdapterResult(
                source=source,
                status=AdapterStatus.FAILED,
                requested_at=requested_at,
                completed_at=self._clock(),
                raw=raw,
                error=error,
            )

        try:
            response = client.get(_PATHS[source], params=params)
        except httpx.HTTPError as exc:
            return failed(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
        try:
            body: Any = response.json()
        except ValueError:
            body = None
            if response.status_code < 400:
                return failed(f"HTTP {response.status_code}: the body is not JSON")
        if response.status_code >= 400:
            return failed(_error_message(response.status_code, body, response.reason_phrase), body)
        try:
            parsed = _PARSERS[source](body)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return failed(f"unexpected response shape: {exc}", body)
        return AdapterResult(
            source=source,
            status=AdapterStatus.OK,
            requested_at=requested_at,
            completed_at=self._clock(),
            raw=body,
            parsed=parsed,
        )
