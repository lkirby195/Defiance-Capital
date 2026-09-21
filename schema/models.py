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

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class AssetType(StrEnum):
    """Subject-property asset type; drives the exit inference with the term.  # SPEC §3"""

    SFR = "SFR"
    UNITS_2_4 = "UNITS_2_4"
    UNITS_5_PLUS = "UNITS_5_PLUS"
    OTHER = "OTHER"


class ExitSource(StrEnum):
    """Whether the exit was stated by the team or inferred from term x asset type.  # SPEC §3"""

    STATED = "STATED"
    INFERRED = "INFERRED"


class State(StrEnum):
    """Property state; anything outside OK/CO is OTHER.  # SPEC §4.5"""

    OK = "OK"
    CO = "CO"
    OTHER = "OTHER"


class StateSource(StrEnum):
    """Whether ``property.state`` was entered by a person or inferred from the address."""

    ENTERED = "ENTERED"
    INFERRED = "INFERRED"


class ProductSource(StrEnum):
    """Whether ``deal.product`` was entered by the team or inferred by the normalizer."""

    ENTERED = "ENTERED"
    INFERRED = "INFERRED"


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


class LienKind(StrEnum):
    """Subject-property encumbrance types.  # SPEC §7.2"""

    LIEN = "LIEN"
    LIS_PENDENS = "LIS_PENDENS"


class ValueSource(StrEnum):
    """Where a value the engine ran on came from.  # SPEC §6, §8.1

    An adapter value always wins over a team value; the team value stays on the deal either
    way, so a later reader can see what was entered by hand and what superseded it.
    """

    ADAPTER = "ADAPTER"  # an enrichment adapter or a paid pull (SPEC §6)
    TEAM = "TEAM"  # entered by hand in the review queue


class CourtRecordsStatus(StrEnum):
    """What the team found when they searched the courts by hand.  # SPEC §6, §7.2

    The court-record adapters are Phase 3; until then a person searches OSCN, PACER or the
    county and records the outcome here. NOT_CHECKED is not the same as CLEAN: the screen
    flags the first as unknown (INFO) and treats the second as a clean record.
    """

    NOT_CHECKED = "NOT_CHECKED"
    CLEAN = "CLEAN"
    FLAGS = "FLAGS"


class DocumentKind(StrEnum):
    """Document types stored on ``documents``.  # SPEC §5"""

    CREDIT_REPORT = "CREDIT_REPORT"
    VALUATION = "VALUATION"
    CONTRACT = "CONTRACT"
    CREDIT_MEMO = "CREDIT_MEMO"
    LOI = "LOI"


class AuditAction(StrEnum):
    """Stable codes for ``audit_log.action``.  # SPEC §5, §11

    One per thing a person can do, not one per column that moved: the row carries what
    changed in ``before``/``after``, and the code says what the person thought they were
    doing. Stable because an auditor reads them years later and a renamed code would orphan
    every row already written under the old name.

    ``LOI_SENT`` and ``HANDED_OFF`` are written by the SPEC §12 Phase 5 and 6 actions, which
    do not exist yet. They are named here because the queue already reads them: a Hard flag
    raised *after* a deal reached one of those states is the case worth pinning to the top,
    and "after" is measured against the row that recorded the move (SPEC §12).
    """

    # Deals
    INTAKE_CREATED = "INTAKE_CREATED"
    SCREEN_RUN = "SCREEN_RUN"
    UNDERWRITE_RUN = "UNDERWRITE_RUN"
    OVERRIDES_SAVED = "OVERRIDES_SAVED"
    ADVANCED_TO_REVIEW = "ADVANCED_TO_REVIEW"
    DECLINED = "DECLINED"
    MARKED_DEAD = "MARKED_DEAD"
    REOPENED = "REOPENED"
    NOTE_ADDED = "NOTE_ADDED"
    LOI_SENT = "LOI_SENT"  # SPEC §9.3, Phase 5
    HANDED_OFF = "HANDED_OFF"  # SPEC §9.4, Phase 6
    # Users and access (SPEC §11: credit data access is logged)
    USER_CREATED = "USER_CREATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    SIGNED_IN = "SIGNED_IN"
    SIGNED_OUT = "SIGNED_OUT"


# The statuses a deal is pinned out of when a Hard flag lands after it got there (SPEC §12),
# and the audit actions that record it getting there.
PINNED_AFTER_ACTIONS: tuple[AuditAction, ...] = (AuditAction.LOI_SENT, AuditAction.HANDED_OFF)


# What the screen tests each court code on (SPEC §7.2), so a team-entered matter can be
# rejected at the door when it does not carry it.
DATED_COURT_FLAGS: frozenset[CourtFlag] = frozenset(
    {
        CourtFlag.BANKRUPTCY_IN_LOOKBACK,
        CourtFlag.SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK,
    }
)
AMOUNT_COURT_FLAGS: frozenset[CourtFlag] = frozenset(
    {
        CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD,
        CourtFlag.OPEN_TAX_LIEN,
        CourtFlag.ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT,
    }
)
LIEN_COURT_FLAGS: frozenset[CourtFlag] = frozenset({CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS})


class TeamCourtRecord(BaseModel):
    """One court or filing matter the team found by hand, typed by the flag it feeds.

    The team is the court-record source until the Phase 3 adapters land (SPEC §6). Each
    entry carries the facts the screen's own test for that code needs (SPEC §7.2) - the date
    for the two lookback codes, the amount for the three threshold codes, the lien facts for
    a subject-property encumbrance - so the config thresholds still decide the outcome
    rather than the team asserting a verdict. One matter per entry; a code with no matter is
    simply absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CourtFlag
    occurred_on: date | None = None  # filing date, or the date a judgment was satisfied
    amount_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    lien_kind: LienKind | None = None
    senior: bool | None = None
    resolved_at_close: bool | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _carries_what_its_code_is_tested_on(self) -> TeamCourtRecord:
        code = self.code
        if code in DATED_COURT_FLAGS and self.occurred_on is None:
            raise ValueError(f"{code.value} is tested against a lookback and needs occurred_on")
        if code in AMOUNT_COURT_FLAGS and self.amount_usd is None:
            raise ValueError(f"{code.value} is tested against a threshold and needs amount_usd")
        lien_fields = (self.lien_kind, self.senior, self.resolved_at_close)
        if code in LIEN_COURT_FLAGS:
            if any(field is None for field in lien_fields):
                raise ValueError(
                    f"{code.value} needs lien_kind, senior and resolved_at_close: it is "
                    "flagged only when senior and unresolved at close"
                )
        elif any(field is not None for field in lien_fields):
            raise ValueError(
                f"lien_kind, senior and resolved_at_close apply only to "
                f"{CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS.value}, not {code.value}"
            )
        return self


def validate_court_records(
    status: CourtRecordsStatus | None,
    as_of: date | None,
    records: list[TeamCourtRecord],
) -> None:
    """A team court search says what it found and when, or says it was not done.

    Shared by ``DealInfo`` and the team-entry form so an incoherent block is refused at the
    door with a 422 rather than part-way through assembly. The date is not optional on a
    real search: the engine has no clock, so every SPEC §7.2 lookback is measured from it.
    """
    if status is None or status is CourtRecordsStatus.NOT_CHECKED:
        if records or as_of is not None:
            raise ValueError(
                "court findings need court_records_status CLEAN or FLAGS; "
                "NOT_CHECKED and unset carry neither a date nor a matter"
            )
        return
    if as_of is None:
        raise ValueError(
            f"court_records_status {status.value} needs court_records_as_of: the engine "
            "has no clock and the lookbacks are measured from the search date"
        )
    if status is CourtRecordsStatus.CLEAN and records:
        raise ValueError("court_records_status CLEAN cannot carry a matter")
    if status is CourtRecordsStatus.FLAGS and not records:
        raise ValueError("court_records_status FLAGS needs at least one matter")


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
    state_source: StateSource = StateSource.INFERRED


class DealInfo(BaseModel):
    """Deal terms as requested by the borrower.  # SPEC §4.5"""

    model_config = ConfigDict(extra="forbid")

    purchase_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rehab_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    term_bucket: TermBucket | None = None
    # Asset type from intake; with the term it drives the exit inference (SPEC §3). None
    # means unknown, which only ever leaves the exit UNKNOWN - it never forces one.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None
    # Product: entered by the team, or inferred by the normalizer (NO_DRAW when rehab_budget
    # is 0, else SPLIT_DRAW). WHOLETAIL and SPLIT_PRINCIPAL are never inferred.  # SPEC §3
    product: Product | None = None
    product_source: ProductSource | None = None
    # Team-supplied actuals (annual USD) that override the %-of-as-is-value opex defaults
    # in config when present; the underwrite reads them via UnderwriteInputs.  # SPEC §8.1, §8.6
    actual_annual_taxes_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    actual_annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    # The other two SPEC §8.1 team inputs: annual utilities (holding costs and the REO carry)
    # and the monthly market rent the DSCR takeout is computed on. Neither has a config
    # default or an adapter behind it, so the team enters them once and the underwrite reads
    # them off the deal unless an UnderwriteRequest carries a newer number.
    actual_annual_utilities_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    market_rent_monthly: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Team-supplied valuation, used when no adapter has produced one (SPEC §6). An adapter
    # value always wins; these stay on the deal either way, for audit.
    as_is_value_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    arv_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # Team court search (SPEC §7.2): the outcome, the date it was searched (the engine has
    # no clock, so the lookbacks are measured from it), and one typed entry per matter.
    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _product_and_source_together(self) -> DealInfo:
        if (self.product is None) != (self.product_source is None):
            raise ValueError("product and product_source must be set together")
        return self

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> DealInfo:
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self


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


# --- Engine flags and enums (SPEC §7, §8.2, §8.7) ---------------------------------------------


class ScreenFlag(StrEnum):
    """Stable codes for flags the screen raises itself (court flags are ``CourtFlag``).

    Severities are fixed by the verdict rules in SPEC §7.5 rather than by config:
    HARD codes decline, SOFT codes make the screen Conditional, INFO codes only inform.
    """

    CREDIT_BELOW_FLOOR = "CREDIT_BELOW_FLOOR"  # HARD, SPEC §7.5
    LTC_OVER_CAP = "LTC_OVER_CAP"  # HARD beyond the tolerance band, SOFT within it
    LTV_AS_IS_OVER_CAP = "LTV_AS_IS_OVER_CAP"
    LTARV_OVER_CAP = "LTARV_OVER_CAP"
    AS_IS_VALUE_MISSING = "AS_IS_VALUE_MISSING"  # SOFT, SPEC §7.4
    ARV_MISSING = "ARV_MISSING"  # SOFT, SPEC §7.4
    STATE_NOT_SERVED = "STATE_NOT_SERVED"  # SOFT, SPEC §7.5
    CREDIT_MISMATCH = "CREDIT_MISMATCH"  # SOFT, SPEC §7.5
    EXPERIENCE_MISMATCH = "EXPERIENCE_MISMATCH"  # SOFT, SPEC §6, §7.5
    REPEAT_BORROWER_MISMATCH = "REPEAT_BORROWER_MISMATCH"  # SOFT, SPEC §7.5
    REPEAT_BORROWER_OVERRIDE_APPLIED = "REPEAT_BORROWER_OVERRIDE_APPLIED"  # INFO, SPEC §7.3
    REPEAT_BORROWER_UNVERIFIED = "REPEAT_BORROWER_UNVERIFIED"  # INFO
    REPEAT_BORROWER_PAYOFF_NOT_CLEAN = "REPEAT_BORROWER_PAYOFF_NOT_CLEAN"  # INFO
    COURT_RECORDS_NOT_CHECKED = "COURT_RECORDS_NOT_CHECKED"  # INFO
    TEAM_SOURCED_VALUES = "TEAM_SOURCED_VALUES"  # INFO, SPEC §6.1: entered by hand, not pulled
    COMMITMENT_BELOW_REQUEST = "COMMITMENT_BELOW_REQUEST"  # INFO, SPLIT_PRINCIPAL override, §8.2


class UnderwriteFlag(StrEnum):
    """Stable codes for flags the underwrite raises.  # SPEC §8.6, §8.7

    The two credit codes take their severity from ``flags.underwrite_severities``; the two
    informational codes are fixed INFO in code and config must not grade them.
    """

    REFI_SHORTFALL = "REFI_SHORTFALL"  # DSCR takeout does not cover commitment + payoff fees
    DOWNSIDE_COVER_BELOW_FLOOR = "DOWNSIDE_COVER_BELOW_FLOOR"  # REO recovery / exposure
    NO_REHAB_PERIOD = "NO_REHAB_PERIOD"  # INFO, SPEC §8.3: SPLIT_PRINCIPAL, term <= listing_months
    SOLVED_RATE_BELOW_GRID = "SOLVED_RATE_BELOW_GRID"  # INFO, SPEC §8.4: r* under rate_grid.min


# Underwrite codes whose severity is a config decision; every other code is fixed INFO
# in code (SPEC §8.7) and ``flags.underwrite_severities`` rejects it.
GRADED_UNDERWRITE_FLAGS: frozenset[UnderwriteFlag] = frozenset(
    {UnderwriteFlag.REFI_SHORTFALL, UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR}
)


class RepeatBorrowerStatus(StrEnum):
    """Mortgage Automator borrower-match outcome.  # SPEC §6, §7.3"""

    CLEAN = "CLEAN"  # matched; prior GLENWOOD loans paid off cleanly
    NOT_CLEAN = "NOT_CLEAN"  # matched; payoff history is not clean
    NO_MATCH = "NO_MATCH"


class LeverageMetric(StrEnum):
    """The three implied-leverage metrics.  # SPEC §7.4"""

    LTC = "LTC"
    LTV_AS_IS = "LTV_AS_IS"
    LTARV = "LTARV"


class CapStatus(StrEnum):
    """How a leverage metric sits against its cap.  # SPEC §7.5"""

    PASS = "PASS"  # actual <= cap
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"  # cap < actual <= cap + tolerance band -> Conditional
    FAIL = "FAIL"  # actual > cap + tolerance band -> Decline
    NOT_AVAILABLE = "NOT_AVAILABLE"  # denominator unavailable (ARV missing)


class ValueBasis(StrEnum):
    """Denominator used for LTV.  # SPEC §7.4"""

    AS_IS_VALUE = "AS_IS_VALUE"
    PURCHASE_PRICE = "PURCHASE_PRICE"  # fallback when the as-is value is unavailable


class Flag(BaseModel):
    """One flag: stable code, severity, message naming the threshold tested.  # SPEC §8.7"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CourtFlag | ScreenFlag | UnderwriteFlag
    severity: Severity
    message: str


# --- Engine inputs (SPEC §7.2, §7.4, §8.2) ----------------------------------------------------


class SubjectPropertyLien(BaseModel):
    """An existing lien or lis pendens on the subject property.  # SPEC §7.2"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LienKind
    senior: bool
    resolved_at_close: bool
    amount_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    description: str | None = None


class CourtRecordInputs(BaseModel):
    """Typed court and filing facts from enrichment.  # SPEC §7.2

    Thresholds and lookbacks are config. Amounts are per matter; unsatisfied judgments and
    open tax liens are summed across matters before the threshold test, active litigation
    is tested per matter. Dates are compared with ``as_of`` (the engine has no clock). A
    record with only ``as_of`` set is clean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date
    source: ValueSource = ValueSource.ADAPTER
    bankruptcy_filing_dates: list[date] = Field(default_factory=list)
    active_foreclosure_as_owner: bool = False
    unsatisfied_judgments_usd: list[Decimal] = Field(default_factory=list)
    open_tax_liens_usd: list[Decimal] = Field(default_factory=list)
    active_civil_litigation_as_defendant_usd: list[Decimal] = Field(default_factory=list)
    satisfied_judgment_or_released_lien_dates: list[date] = Field(default_factory=list)
    landlord_tenant_matters_as_landlord: int = Field(default=0, ge=0)
    subject_property_liens: list[SubjectPropertyLien] = Field(default_factory=list)


class SizingInputs(BaseModel):
    """Deal numbers for implied leverage and the commitment split.  # SPEC §7.4, §8.2

    ``as_is_value`` / ``arv`` are None when enrichment or valuation has not supplied them.
    ``purchase_portion_override`` is the team override of the purchase portion for the
    split products only (SPEC §8.2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: Product
    purchase_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    rehab_budget: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    as_is_value: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    arv: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    as_is_value_source: ValueSource | None = None
    arv_source: ValueSource | None = None
    purchase_portion_override: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )

    @model_validator(mode="before")
    @classmethod
    def _unstated_source_is_an_adapter(cls, data: Any) -> Any:
        """A valuation with no stated source came from enrichment; the team names itself."""
        if not isinstance(data, dict):
            return data
        for value, source in (("as_is_value", "as_is_value_source"), ("arv", "arv_source")):
            if data.get(value) is not None and data.get(source) is None:
                data = {**data, source: ValueSource.ADAPTER}
        return data

    @model_validator(mode="after")
    def _source_needs_a_value(self) -> SizingInputs:
        for value, source in (
            (self.as_is_value, self.as_is_value_source),
            (self.arv, self.arv_source),
        ):
            if source is not None and value is None:
                raise ValueError("a valuation source cannot be recorded without its value")
        return self

    @model_validator(mode="after")
    def _override_only_for_split_products(self) -> SizingInputs:
        if self.purchase_portion_override is None:
            return self
        if self.product not in (Product.SPLIT_DRAW, Product.SPLIT_PRINCIPAL):
            raise ValueError(
                "purchase_portion_override applies only to SPLIT_DRAW / SPLIT_PRINCIPAL"
            )
        if self.purchase_portion_override > self.loan_requested:
            raise ValueError("purchase_portion_override cannot exceed loan_requested")
        return self


class BorrowerInputs(BaseModel):
    """Borrower facts for the screen: self-reported plus whatever is verified.  # SPEC §7.1, §7.3"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_range_self_reported: Tranche
    verified_credit_score: int | None = Field(default=None, ge=300, le=850)
    experience_bucket_self_reported: ExperienceBucket
    verified_deals_36mo: int | None = Field(default=None, ge=0)
    repeat_borrower_self_reported: bool
    repeat_borrower_verified: RepeatBorrowerStatus | None = None


class ScreenInputs(BaseModel):
    """Everything the screen needs; the caller assembles it from deal + enrichment.  # SPEC §7"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deal: SizingInputs
    state: State
    borrower: BorrowerInputs
    court_records: CourtRecordInputs | None = None


# --- Engine outputs (SPEC §7.5, §8.2, §8.7) ---------------------------------------------------


class MetricCheck(BaseModel):
    """One leverage metric vs. its cap: actual, cap, pass/fail, tolerance status.  # SPEC §8.2"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: LeverageMetric
    actual: Decimal | None  # None when the denominator is unavailable
    cap: Decimal
    tolerance_band: Decimal
    status: CapStatus
    passed: bool  # status is PASS
    basis: ValueBasis | None = None  # LTV only: which denominator was used


class CommitmentSplit(BaseModel):
    """Purchase / rehab split of a split product.  # SPEC §8.2

    SPLIT_DRAW: ``rehab_portion`` is the holdback on the single note.
    SPLIT_PRINCIPAL: ``purchase_portion`` is the Principal Note, ``rehab_portion`` Tranche A.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purchase_portion: Decimal
    rehab_portion: Decimal
    purchase_portion_overridden: bool


class SizingResult(BaseModel):
    """Implied leverage and commitment for one caps cell.  # SPEC §7.4, §8.2"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: Product
    credit_tranche: Tranche
    experience_tier: ExperienceTier
    rehab_adj: Decimal
    buy_closing: Decimal  # purchase_price x borrower_closing_pct_of_price, borrower cash
    total_cost: Decimal
    loan_requested: Decimal
    commitment: Decimal
    funded_at_close: Decimal
    split: CommitmentSplit | None
    ltv_basis: ValueBasis
    as_is_value_source: ValueSource | None  # None when no as-is value was available
    arv_source: ValueSource | None
    metrics: dict[LeverageMetric, MetricCheck]
    all_pass: bool


class ScreenComponents(BaseModel):
    """Score components recorded on ``screens.score_components``.  # SPEC §5, §7"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_tranche: Tranche  # tranche used for the floor check and the caps lookup
    credit_tranche_verified: bool  # True when derived from a verified score
    floor_tranche: Tranche
    credit_meets_floor: bool
    experience_tier_self_reported: ExperienceTier
    experience_tier_verified: ExperienceTier | None
    repeat_borrower_override_applied: bool
    experience_tier: ExperienceTier  # tier used for the caps lookup
    court_records_source: ValueSource | None  # None when no source was checked


class ScreenResult(BaseModel):
    """Screen verdict with its reasons, flags, components, and sizing.  # SPEC §7.5"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_version: str
    config_hash: str
    verdict: Verdict
    reasons: list[str]
    flags: list[Flag]
    suggested_reply: str
    components: ScreenComponents
    sizing: SizingResult


# --- Underwrite inputs and outputs (SPEC §8.1, §8.3-8.7) ---------------------------------------


class UnderwriteInputs(BaseModel):
    """Everything the underwrite needs; the caller assembles it from deal + enrichment.  # SPEC §8.1

    ``deal`` carries the verified ``as_is_value`` and ``arv`` (both required here, unlike the
    screen). ``borrower`` carries the verified credit score and deal count when known; the
    underwrite derives the caps cell from them exactly as the screen does. Annual taxes and
    insurance are team actuals; ``None`` falls back to the config defaults as a percentage of
    the as-is value. ``exit_price`` defaults to the ARV (flip); the team sets a retail price
    for wholetail. ``extension_fee_pct`` defaults to the config default. ``asset_type`` and
    ``stated_exit`` drive the SPEC §3 exit inference.

    ``court_records`` is the latest source in force at underwrite time, adapter over team,
    exactly as at the screen (SPEC §6.1, §8.1). The underwrite re-runs the SPEC §7.2 tests on
    it rather than trusting the screen's: weeks can pass between the two, the pull an adapter
    now has may have superseded the hand search the screen ran on, and the stored underwrite
    is what a credit memo is written from. ``None`` means no source was checked, which is
    reported as an INFO flag and never read as clean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deal: SizingInputs
    state: State
    borrower: BorrowerInputs
    term_months: int = Field(ge=1, le=60)
    market_rent_monthly: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    annual_taxes_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    annual_utilities_usd: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    extension_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    exit_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    asset_type: AssetType | None = None
    stated_exit: StatedExit = StatedExit.UNKNOWN
    court_records: CourtRecordInputs | None = None

    @model_validator(mode="after")
    def _valuation_is_complete(self) -> UnderwriteInputs:
        if self.deal.as_is_value is None or self.deal.arv is None:
            raise ValueError("underwrite requires both as_is_value and arv on the deal")
        return self

    @property
    def as_is_value(self) -> Decimal:
        """The verified as-is value (validated present)."""
        if self.deal.as_is_value is None:  # pragma: no cover - guarded by the validator
            raise ValueError("as_is_value is required")
        return self.deal.as_is_value

    @property
    def arv(self) -> Decimal:
        """The verified ARV (validated present)."""
        if self.deal.arv is None:  # pragma: no cover - guarded by the validator
            raise ValueError("arv is required")
        return self.deal.arv


class OpexSource(StrEnum):
    """Where an annual taxes / insurance figure came from.  # SPEC §8.6"""

    ACTUAL = "ACTUAL"  # team-supplied
    DEFAULT = "DEFAULT"  # config percentage of the as-is value


class FeeSchedule(BaseModel):
    """Lender fees for a payoff at one month, all on the total commitment.  # SPEC §3, §8.4"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origination_at_close: Decimal
    origination_at_payoff: Decimal
    extension: Decimal  # zero unless the payoff month is past the term
    total: Decimal


class LenderReturn(BaseModel):
    """Lender economics for a payoff at ``month`` and note rate ``rate``.  # SPEC §8.4"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    rate: Decimal
    avg_outstanding: Decimal
    interest: Decimal
    fees: FeeSchedule
    annualized_yield: Decimal  # (interest + fees) / commitment x 12 / month


class GridCell(BaseModel):
    """One cell of the lender yield grid.  # SPEC §8.5"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    rate: Decimal
    annualized_yield: Decimal
    meets_target: bool  # annualized_yield >= target_irr
    is_solved_rate: bool  # this column is r*


class GridRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    cells: list[GridCell]


class YieldGrid(BaseModel):
    """lender_yield(m, r) over the rate grid (plus r*) and the month window.  # SPEC §8.5"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Decimal
    solved_rate: Decimal
    solved_rate_inserted: bool  # False when r* already sat on the configured grid
    rates: list[Decimal]  # columns, ascending
    months: list[int]  # rows, term .. term + after_term
    rows: list[GridRow]


class BorrowerEconomics(BaseModel):
    """Borrower profit and cash-on-cash at one (month, rate); information only.  # SPEC §8.6"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    rate: Decimal
    purchase_price: Decimal
    rehab_adj: Decimal
    buy_closing: Decimal
    total_project_cost: Decimal
    interest_paid: Decimal
    fees_paid: Decimal
    holding_costs: Decimal
    exit_price: Decimal
    exit_net: Decimal
    profit: Decimal
    cash_in: Decimal
    cash_on_cash: Decimal | None  # None when cash_in <= 0


class ExitResult(BaseModel):
    """DSCR takeout, run on every deal.  # SPEC §8.6"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: StatedExit  # stated by the team, else inferred from term x asset type (SPEC §3)
    exit_source: ExitSource  # STATED when the team set it, INFERRED when the engine did
    gross_rent_annual: Decimal
    annual_taxes: Decimal
    annual_taxes_source: OpexSource
    annual_insurance: Decimal
    annual_insurance_source: OpexSource
    opex_annual: Decimal
    noi_annual: Decimal
    ltv_takeout: Decimal  # arv x takeout ltv
    dscr_takeout: Decimal  # loan whose debt service = noi / dscr_floor (0 when noi <= 0)
    max_takeout: Decimal  # min of the two
    payoff_due: Decimal  # commitment + payoff fees
    dscr_at_payoff: Decimal | None  # noi / debt service on payoff_due
    refi_covers: bool
    shortfall: Decimal  # max(0, payoff_due - max_takeout)


class DownsideResult(BaseModel):
    """REO downside, run on every deal.  # SPEC §8.6"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recovery_basis: Decimal  # min(as_is_value + rehab_adj, arv)
    liquidation: Decimal  # recovery_basis x (1 - reo_haircut)
    selling_costs: Decimal
    foreclosure_cost: Decimal
    foreclosure_months: int
    monthly_holding_cost: Decimal
    holding_through_foreclosure: Decimal
    recovery: Decimal
    unpaid_fees: Decimal
    exposure: Decimal  # commitment + unpaid fees
    cover: Decimal  # recovery / exposure
    cover_floor: Decimal
    passed: bool  # cover >= cover_floor


class UnderwriteResult(BaseModel):
    """Full underwrite output; stored on ``underwrites`` with the grid as JSONB.  # SPEC §8.7"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_version: str
    config_hash: str
    term_months: int
    rehab_months: int
    sizing: SizingResult
    solved_rate: Decimal
    lender_yield_at_solve: Decimal
    lender_at_solve: LenderReturn
    grid_lender: YieldGrid
    borrower_at_solve: BorrowerEconomics
    exit: ExitResult
    downside: DownsideResult
    flags: list[Flag]
