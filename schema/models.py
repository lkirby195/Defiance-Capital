"""Canonical intake schema and the enums shared across the pipeline.  # SPEC §4.5

Two kinds of optional field live on ``IntakeRecord``:

* Fields the spec marks ``?`` (email, entity_name, address_normalized,
  listing_url, county, stated_exit) are never required.
* Minimum-viable fields (SPEC §4.1) are ``Optional`` only because an intake may
  arrive incomplete. ``missing_fields`` names the ones the team still has to
  ask for; ``intake/normalize.py`` is the sole writer of that list.

``schema/intake.json`` is generated from ``IntakeRecord`` by ``schema/generate.py``
and committed; ``tests/test_schema.py`` fails if the two drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Product(StrEnum):
    """Loan products.  # SPEC §3"""

    NO_DRAW = "NO_DRAW"
    SPLIT_DRAW = "SPLIT_DRAW"
    SPLIT_PRINCIPAL = "SPLIT_PRINCIPAL"
    WHOLETAIL = "WHOLETAIL"


class Tranche(StrEnum):
    """Credit tranches; cutoffs live in config.  # SPEC §7.1"""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    T5 = "T5"


class ExperienceBucket(StrEnum):
    """Self-reported deals completed in the last 3 years.  # SPEC §4.1"""

    ZERO = "0"
    ONE_TO_TWO = "1_2"
    THREE_TO_FIVE = "3_5"
    SIX_PLUS = "6_PLUS"


class ExperienceTier(StrEnum):
    """Verified experience tier from deed history.  # SPEC §7.3"""

    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"


class TermBucket(StrEnum):
    """Requested loan term in months.  # SPEC §4.1"""

    M3 = "3"
    M6 = "6"
    M9 = "9"
    M12 = "12"
    M12_PLUS = "12_PLUS"


class Channel(StrEnum):
    """Intake channel.  # SPEC §4.2"""

    SMS = "SMS"
    LINK = "LINK"
    CONTRACT = "CONTRACT"
    TEAM = "TEAM"


class StatedExit(StrEnum):
    """Borrower-stated exit, confirmed by the team.  # SPEC §3, §4.5"""

    FLIP = "FLIP"
    HOLD = "HOLD"
    WHOLETAIL = "WHOLETAIL"
    UNKNOWN = "UNKNOWN"


class State(StrEnum):
    """Property state; anything outside OK/CO is OTHER.  # SPEC §4.5"""

    OK = "OK"
    CO = "CO"
    OTHER = "OTHER"


class Status(StrEnum):
    """Deal lifecycle status.  # SPEC §4.5"""

    NEW = "NEW"
    NEEDS_INFO = "NEEDS_INFO"
    SCREENED = "SCREENED"
    IN_REVIEW = "IN_REVIEW"
    UNDERWRITING = "UNDERWRITING"
    LOI_SENT = "LOI_SENT"
    HANDED_OFF = "HANDED_OFF"
    DECLINED = "DECLINED"
    DEAD = "DEAD"


class Verdict(StrEnum):
    """Screen verdict.  # SPEC §7.5"""

    GO = "GO"
    CONDITIONAL = "CONDITIONAL"
    DECLINE = "DECLINE"


class Severity(StrEnum):
    """Flag severity.  # SPEC §7.2"""

    HARD = "HARD"
    SOFT = "SOFT"
    INFO = "INFO"


class CourtFlag(StrEnum):
    """Stable codes for the court and filing flags; severity is config.  # SPEC §7.2"""

    BANKRUPTCY_IN_LOOKBACK = "BANKRUPTCY_IN_LOOKBACK"
    ACTIVE_FORECLOSURE_AS_OWNER = "ACTIVE_FORECLOSURE_AS_OWNER"
    UNSATISFIED_JUDGMENT_OVER_THRESHOLD = "UNSATISFIED_JUDGMENT_OVER_THRESHOLD"
    OPEN_TAX_LIEN = "OPEN_TAX_LIEN"
    ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT = "ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT"
    SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK = (
        "SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK"
    )
    LANDLORD_TENANT_AS_LANDLORD = "LANDLORD_TENANT_AS_LANDLORD"
    SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS = "SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS"


class DocumentKind(StrEnum):
    """Document types stored on ``documents``.  # SPEC §5"""

    CREDIT_REPORT = "CREDIT_REPORT"
    VALUATION = "VALUATION"
    CONTRACT = "CONTRACT"
    CREDIT_MEMO = "CREDIT_MEMO"
    LOI = "LOI"


def _now_utc() -> datetime:
    return datetime.now(UTC)


class BorrowerInfo(BaseModel):
    """Who the borrower is; credit and experience are self-reported here.  # SPEC §4.5"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    entity_name: str | None = None
    credit_range: Tranche | None = None
    experience_bucket: ExperienceBucket | None = None
    repeat_borrower: bool | None = None


class PropertyInfo(BaseModel):
    """Subject property; address or listing URL satisfies the minimum.  # SPEC §4.5"""

    model_config = ConfigDict(extra="forbid")

    address_raw: str | None = None
    address_normalized: str | None = None
    listing_url: str | None = None
    county: str | None = None
    state: State = State.OTHER


class DealInfo(BaseModel):
    """Deal terms as requested by the borrower.  # SPEC §4.5"""

    model_config = ConfigDict(extra="forbid")

    purchase_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rehab_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    term_bucket: TermBucket | None = None
    stated_exit: StatedExit | None = None


class IntakeRecord(BaseModel):
    """Canonical intake record; one per property x borrower inquiry.  # SPEC §4.5"""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_now_utc)
    channel: Channel
    raw_payload: dict[str, Any] | str
    borrower: BorrowerInfo = Field(default_factory=BorrowerInfo)
    property: PropertyInfo = Field(default_factory=PropertyInfo)
    deal: DealInfo = Field(default_factory=DealInfo)
    missing_fields: list[str] = Field(default_factory=list)
    status: Status = Status.NEW
