"""The shape every enrichment adapter has.  # SPEC §6, CLAUDE.md "Adapters are thin and mockable"

An adapter is asked about a ``Subject`` - the property as the deal knows it - and answers
with one ``AdapterResult`` per remote call it made: the source it called, how the call went,
when, the raw body it got back and what it parsed out of it. Every result is written to
``enrichment_runs`` by the service that asked (``services/market_data.py``), raw body
included, because the raw body is the record and the parsed result is one reading of it.

An adapter never raises on a remote failure. A timeout, a refused connection, a 4xx or 5xx,
a body that is not JSON or a body that is not the shape the docs promised each come back as
``FAILED`` with the error in words, so the arrival run proceeds on whatever the deal already
holds and the row says why there is nothing more. The two statuses an adapter never returns
- ``CACHED`` and ``SKIPPED`` - are the service's: a stored run reused for a second deal at
the same address, and a pull not sent because the month's request budget is spent. They are
on the same enum because the same column holds them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol


class AdapterStatus(StrEnum):
    """What one ``enrichment_runs`` row says about its call."""

    OK = "OK"  # the call returned and the body parsed
    FAILED = "FAILED"  # the remote failed, timed out, refused, or sent an unreadable body
    CACHED = "CACHED"  # no call: an OK run at the same address reused (services/market_data.py)
    SKIPPED = "SKIPPED"  # no call: the month's request budget is spent


@dataclass(frozen=True)
class Subject:
    """What an adapter is asked about: the property as the deal holds it.  # SPEC §8.1

    The address is the lookup key and the only thing required; the rest is passed along
    when the deal has it, because an AVM asked about a 3/2 of 1,878 square feet answers
    better than one left to look the attributes up.
    """

    address: str
    bedrooms: int | None = None
    bathrooms: Decimal | None = None
    square_footage: int | None = None
    property_type: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    """One remote call as it went.  # SPEC §5, §6

    ``raw`` is the response body as JSON whatever the status - an error body is kept too -
    and None when nothing came back at all. ``parsed`` is the adapter's reading of an OK
    body, JSON-able (money as a string of two decimals, dates as ``YYYY-MM-DD``), and None
    on anything but OK. ``error`` says what went wrong, in words a person reads off the row.
    """

    source: str
    status: AdapterStatus
    requested_at: datetime
    completed_at: datetime
    raw: Any = None
    parsed: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is AdapterStatus.OK


class Adapter(Protocol):
    """One enrichment source: asked about a subject, answers with its calls."""

    @property
    def source(self) -> str:
        """The short name every row this adapter writes is keyed under (``rentcast``)."""
        ...

    def enrich(self, subject: Subject) -> list[AdapterResult]:
        """Every call this adapter makes about ``subject``, in the order it made them."""
        ...
