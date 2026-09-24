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

from schema.dates import payoff_date_for


class Product(StrEnum):
    """Loan products.  # SPEC §3"""

    NO_DRAW = "NO_DRAW"
    SPLIT_DRAW = "SPLIT_DRAW"
    SPLIT_PRINCIPAL = "SPLIT_PRINCIPAL"
    WHOLETAIL = "WHOLETAIL"


class LoanPurpose(StrEnum):
    """What the borrower is doing with the money; team-selected.  # SPEC §8.1

    Recorded and reported, and nothing in the math reads it. It is here because a credit
    memo and an LOI both name it and a person deciding on a deal wants it in the Overview
    beside the product, not because any number depends on it.
    """

    PURCHASE = "PURCHASE"
    REFINANCE = "REFINANCE"
    CASH_OUT = "CASH_OUT"
    CONSTRUCTION = "CONSTRUCTION"


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


# The months each bucket names. 12_PLUS names none: a term past a year is a negotiation,
# so the team sets the number (SPEC §4.1, §8.1).
TERM_BUCKET_MONTHS: dict[TermBucket, int] = {
    TermBucket.M3: 3,
    TermBucket.M6: 6,
    TermBucket.M9: 9,
    TermBucket.M12: 12,
}


def months_for_bucket(bucket: TermBucket | None) -> int | None:
    """The months a bucket names, or None for 12_PLUS and for no bucket at all."""
    return None if bucket is None else TERM_BUCKET_MONTHS.get(bucket)


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
    INTAKE_EDITED = "INTAKE_EDITED"
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


# The products whose loan is advanced in two parts (SPEC §3, §8.2). Both carry an explicit
# purchase / rehab split; the other two products are one advance and carry none.
SPLIT_PRODUCTS: frozenset[Product] = frozenset({Product.SPLIT_DRAW, Product.SPLIT_PRINCIPAL})


def validate_loan_split(
    product: Product | None,
    loan_requested: Decimal | None,
    purchase_portion: Decimal | None,
    rehab_portion: Decimal | None,
) -> None:
    """The two halves of a split loan are coherent, or say why they are not.  # SPEC §8.2

    Shared by ``DealInfo``, the team-entry form and ``SizingInputs``, so an incoherent split
    is refused at the door rather than part-way through sizing. Three rules, and the third is
    the one that matters: the split is a division of the loan requested, not a second opinion
    about how much it is.

    What is *not* checked here is presence. A borrower-channel intake carries a loan amount
    and no split (SPEC §4.1, §4.2) - the split is a team entry - so a split product with
    neither portion is coherent and simply has no split yet. The team-entry form requires
    both (``api/intake_form.py``) and the underwrite refuses without them (SPEC §8.1).
    """
    if (purchase_portion is None) != (rehab_portion is None):
        raise ValueError(
            "loan_purchase_portion and loan_rehab_portion are set together or not at all"
        )
    if purchase_portion is None or rehab_portion is None:
        return
    if product is not None and product not in SPLIT_PRODUCTS:
        raise ValueError(
            f"a loan split applies only to {' / '.join(sorted(p.value for p in SPLIT_PRODUCTS))}, "
            f"not {product.value}"
        )
    if loan_requested is not None and purchase_portion + rehab_portion != loan_requested:
        raise ValueError(
            f"the loan split must add up to the loan requested: "
            f"{purchase_portion} + {rehab_portion} is {purchase_portion + rehab_portion}, "
            f"not {loan_requested}"
        )


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
    """Subject property; address or listing URL satisfies the minimum.  # SPEC §4.5, §8.1

    Everything below ``state`` is the SPEC §8.1 Property Overview: descriptive facts a
    person reads on the deal page and in the credit memo. No math reads any of them, so none
    is required to screen or to price a deal - they are captured because a reader wants to
    know what the property is, not because a number depends on it.
    """

    model_config = ConfigDict(extra="forbid")

    address_raw: str | None = None
    address_normalized: str | None = None
    listing_url: str | None = None
    city: str | None = None
    county: str | None = None
    state: State = State.OTHER
    state_source: StateSource = StateSource.INFERRED
    units: int | None = Field(default=None, ge=0)
    structures: int | None = Field(default=None, ge=0)
    sf: int | None = Field(default=None, ge=0)
    year_built: int | None = Field(default=None, ge=1600, le=2200)
    year_renovated: int | None = Field(default=None, ge=1600, le=2200)
    beds: int | None = Field(default=None, ge=0)
    baths: Decimal | None = Field(default=None, ge=0, max_digits=4, decimal_places=1)
    garage_spaces: int | None = Field(default=None, ge=0)


class DealInfo(BaseModel):
    """Deal terms as requested by the borrower, plus the team's own §8.1 economics.

    Grouped as SPEC §8.1 groups them: Deal Economics, then the valuation and rent the
    adapters will eventually supply. Nothing here belongs to the Overview any more - that
    group is who the borrower is, and every one of its fields lives on ``BorrowerInfo``.

    The four economics with a config default (``contingency_pct``, ``closing_costs_usd``,
    ``holding_costs_total_usd``, ``origination_fee_pct``) are None until somebody overrides
    them, and None is what tells the engine to use the config default rather than a number
    somebody chose. ``interest_rate`` has no default and is required to price a deal.

    ``payoff_date`` is deliberately not here. It is ``closing_date`` plus ``term_months``
    (``schema/dates.py``), so storing it would be storing the same fact twice and inviting
    the two to disagree; the team-entry form and the queue's override block take one as an
    alternative way to say the other, and both derive the term before they get here.
    """

    model_config = ConfigDict(extra="forbid")

    # Deal economics (SPEC §8.1). The loan purpose, the loan type, the closing date and
    # the term are here rather than in the Overview: they are terms of the loan, and the
    # Overview is who the borrower is.
    loan_purpose: LoanPurpose | None = None
    closing_date: date | None = None  # month 0 of the ledger (SPEC §8.3)
    purchase_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rehab_costs: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # How the loan requested divides between the purchase advance and the rehab money, for
    # the two split products only (SPEC §8.2). A team entry: a borrower-channel intake
    # carries the loan amount and nothing about its shape, so both are None until somebody
    # enters them and the underwrite refuses a split product without them (SPEC §8.1).
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    term_bucket: TermBucket | None = None
    # The term the deal is priced on (SPEC §8.1). Seeded from the bucket at intake for every
    # bucket that names a number; the team's own number - or the one their payoff date
    # implies - wins over it, and is allowed to differ from the bucket the borrower picked.
    term_months: int | None = Field(default=None, ge=1, le=60)
    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)  # annual; required to price
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    holding_costs_total_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    # Asset type from intake; with the term it drives the exit inference (SPEC §3). None
    # means unknown, which only ever leaves the exit UNKNOWN - it never forces one.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None
    # Product: entered by the team, or inferred by the normalizer (NO_DRAW when rehab_costs
    # is 0, else SPLIT_DRAW). WHOLETAIL and SPLIT_PRINCIPAL are never inferred.  # SPEC §3
    product: Product | None = None
    product_source: ProductSource | None = None
    # The two analysis toggles (SPEC §8.1). None leaves the §3-derived default in force; a
    # team member turning one on or off by hand wins over it, in either direction.
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    # The monthly rent the Rental and Take-Back analyses are computed on (SPEC §8.5, §8.6).
    # No config default and no adapter behind it: nothing stands in for what a property
    # lets for, so without one both analyses are NOT_EVALUATED rather than run on a zero.
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Team-supplied valuation, used when no adapter has produced one (SPEC §6). An adapter
    # value always wins; it stays on the deal either way, for audit.
    estimated_sale_price_team: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
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
    def _loan_split_is_coherent(self) -> DealInfo:
        validate_loan_split(
            self.product, self.loan_requested, self.loan_purchase_portion, self.loan_rehab_portion
        )
        return self

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> DealInfo:
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self

    @property
    def payoff_date(self) -> date | None:
        """``closing_date`` + ``term_months``, or None until both are known.  # SPEC §8.1"""
        if self.closing_date is None or self.term_months is None:
            return None
        return payoff_date_for(self.closing_date, self.term_months)


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
    LTV_OVER_CAP = "LTV_OVER_CAP"  # SPEC §7.4: LTV is commitment / estimated sale price
    ESTIMATED_SALE_PRICE_MISSING = "ESTIMATED_SALE_PRICE_MISSING"  # SOFT, SPEC §7.4
    STATE_NOT_SERVED = "STATE_NOT_SERVED"  # SOFT, SPEC §7.5
    CREDIT_MISMATCH = "CREDIT_MISMATCH"  # SOFT, SPEC §7.5
    EXPERIENCE_MISMATCH = "EXPERIENCE_MISMATCH"  # SOFT, SPEC §6, §7.5
    REPEAT_BORROWER_MISMATCH = "REPEAT_BORROWER_MISMATCH"  # SOFT, SPEC §7.5
    REPEAT_BORROWER_OVERRIDE_APPLIED = "REPEAT_BORROWER_OVERRIDE_APPLIED"  # INFO, SPEC §7.3
    REPEAT_BORROWER_UNVERIFIED = "REPEAT_BORROWER_UNVERIFIED"  # INFO
    REPEAT_BORROWER_PAYOFF_NOT_CLEAN = "REPEAT_BORROWER_PAYOFF_NOT_CLEAN"  # INFO
    COURT_RECORDS_NOT_CHECKED = "COURT_RECORDS_NOT_CHECKED"  # INFO
    TEAM_SOURCED_VALUES = "TEAM_SOURCED_VALUES"  # INFO, SPEC §6.1: entered by hand, not pulled
    COMMITMENT_BELOW_REQUEST = "COMMITMENT_BELOW_REQUEST"  # INFO, SPLIT_PRINCIPAL cap, §8.2
    REHAB_PORTION_EXCEEDS_BUDGET = "REHAB_PORTION_EXCEEDS_BUDGET"  # INFO, split products, §8.2


class UnderwriteFlag(StrEnum):
    """Stable codes for flags the underwrite raises.  # SPEC §8.6, §8.7

    The two DSCR codes take their severity from ``flags.underwrite_severities``; the three
    informational codes are fixed INFO in code and config must not grade them.
    """

    DSCR_BELOW_FLOOR = "DSCR_BELOW_FLOOR"  # SPEC §8.5: rental DSCR under rental.dscr_floor
    TAKE_BACK_DSCR_BELOW_FLOOR = "TAKE_BACK_DSCR_BELOW_FLOOR"  # SPEC §8.6: under take_back floor
    MONTHLY_RENT_MISSING = "MONTHLY_RENT_MISSING"  # INFO, SPEC §8.5, §8.6: no rent to run either on
    SALE_PRICE_MISSING = "SALE_PRICE_MISSING"  # INFO, SPEC §8.4: no price, so no flip to evaluate
    NO_REHAB_PERIOD = "NO_REHAB_PERIOD"  # INFO, SPEC §8.3: no rehab period, so no draw schedule


# Underwrite codes whose severity is a config decision; every other code is fixed INFO
# in code (SPEC §8.7) and ``flags.underwrite_severities`` rejects it.
GRADED_UNDERWRITE_FLAGS: frozenset[UnderwriteFlag] = frozenset(
    {UnderwriteFlag.DSCR_BELOW_FLOOR, UnderwriteFlag.TAKE_BACK_DSCR_BELOW_FLOOR}
)


class RepeatBorrowerStatus(StrEnum):
    """Mortgage Automator borrower-match outcome.  # SPEC §6, §7.3"""

    CLEAN = "CLEAN"  # matched; prior GLENWOOD loans paid off cleanly
    NOT_CLEAN = "NOT_CLEAN"  # matched; payoff history is not clean
    NO_MATCH = "NO_MATCH"


class LeverageMetric(StrEnum):
    """The two implied-leverage metrics.  # SPEC §7.4

    There is one value in the denominator of the second: the estimated sale price. LTARV is
    gone and so is the as-is value it sat beside - one ratio against the price the property
    is expected to sell for, not two against two opinions of what it is worth.
    """

    LTC = "LTC"
    LTV = "LTV"


class CapStatus(StrEnum):
    """How a leverage metric sits against its cap.  # SPEC §7.5"""

    PASS = "PASS"  # actual <= cap
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"  # cap < actual <= cap + tolerance band -> Conditional
    FAIL = "FAIL"  # actual > cap + tolerance band -> Decline
    NOT_AVAILABLE = "NOT_AVAILABLE"  # denominator unavailable (no estimated sale price)


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

    ``estimated_sale_price`` is None when enrichment or valuation has not supplied one. It
    stops nothing at either stage: it is what LTV is computed on (SPEC §7.4) and what the
    Flip analysis sells at (SPEC §8.4), so without one the screen reports LTV NOT_AVAILABLE
    and goes Conditional, and the underwrite reports the flip NOT_EVALUATED.

    ``contingency_pct`` and ``closing_costs_usd`` are SPEC §8.1 inputs with config defaults,
    and they are here rather than read straight from config because both stages size on the
    same two numbers: ``rehab_adj`` and the LTC denominator. None means the config default.

    ``loan_purchase_portion`` / ``loan_rehab_portion`` are the team's own division of the
    loan requested, for the split products only (SPEC §8.2). Both None means the split has
    not been entered yet, which is the state a borrower-channel intake is in: the screen
    sizes on the loan requested and reports no split, and the underwrite refuses (SPEC §8.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: Product
    purchase_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    rehab_costs: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    estimated_sale_price: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    estimated_sale_price_source: ValueSource | None = None
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @model_validator(mode="before")
    @classmethod
    def _unstated_source_is_an_adapter(cls, data: Any) -> Any:
        """A valuation with no stated source came from enrichment; the team names itself."""
        if not isinstance(data, dict):
            return data
        if (
            data.get("estimated_sale_price") is not None
            and data.get("estimated_sale_price_source") is None
        ):
            data = {**data, "estimated_sale_price_source": ValueSource.ADAPTER}
        return data

    @model_validator(mode="after")
    def _source_needs_a_value(self) -> SizingInputs:
        if self.estimated_sale_price_source is not None and self.estimated_sale_price is None:
            raise ValueError("a valuation source cannot be recorded without its value")
        return self

    @model_validator(mode="after")
    def _loan_split_is_coherent(self) -> SizingInputs:
        validate_loan_split(
            self.product, self.loan_requested, self.loan_purchase_portion, self.loan_rehab_portion
        )
        return self

    @property
    def loan_split(self) -> tuple[Decimal, Decimal] | None:
        """The entered split, or None when the team has not divided the loan yet."""
        if self.loan_purchase_portion is None or self.loan_rehab_portion is None:
            return None
        return self.loan_purchase_portion, self.loan_rehab_portion


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


# --- Engine outputs (SPEC §7.5, §8.2) ---------------------------------------------------------


class MetricCheck(BaseModel):
    """One leverage metric vs. its cap: actual, cap, pass/fail, tolerance status.  # SPEC §8.2"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: LeverageMetric
    actual: Decimal | None  # None when the denominator is unavailable
    cap: Decimal
    tolerance_band: Decimal
    status: CapStatus
    passed: bool  # status is PASS


class CommitmentSplit(BaseModel):
    """Purchase / rehab split of a split product, as the team entered it.  # SPEC §8.2

    SPLIT_DRAW: ``rehab_portion`` is the holdback on the single note.
    SPLIT_PRINCIPAL: ``purchase_portion`` is the Principal Note, ``rehab_portion`` Tranche A.

    ``rehab_portion`` is what the lender actually funds, which is the entered portion capped
    at the contingency-adjusted rehab cost (``engine.sizing.commitment_split``);
    ``rehab_portion_capped`` says whether that cap bit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purchase_portion: Decimal
    rehab_portion: Decimal
    rehab_portion_requested: Decimal
    rehab_portion_capped: bool


class SizingResult(BaseModel):
    """Implied leverage and commitment for one caps cell.  # SPEC §7.4, §8.2"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: Product
    credit_tranche: Tranche
    experience_tier: ExperienceTier
    purchase_price: Decimal
    rehab_costs: Decimal
    contingency_pct: Decimal  # the input in force, or the config default
    rehab_adj: Decimal  # rehab_costs x (1 + contingency_pct)
    closing_costs: Decimal  # the lender's closing costs, inside the LTC denominator
    total_cost: Decimal
    loan_requested: Decimal
    commitment: Decimal
    funded_at_close: Decimal
    split: CommitmentSplit | None
    estimated_sale_price_source: ValueSource | None  # None when no price was available
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


# --- Underwrite inputs (SPEC §8.1) -------------------------------------------------------------


class UnderwriteInputs(BaseModel):
    """Everything the underwrite needs; the caller assembles it from deal + enrichment.  # SPEC §8.1

    ``deal`` carries the estimated sale price and, on a split product, the team's advance /
    rehab split. ``borrower`` carries the verified credit score and deal count when known; the
    underwrite derives the caps cell from them exactly as the screen does.

    ``closing_date``, ``term_months`` and ``interest_rate`` are the three the ledger cannot
    be laid out without, and all three are required here. ``payoff_date`` is not a field: it
    is ``closing_date`` plus the term (``schema/dates.py``), and the property below is the
    single place it is worked out.

    ``origination_fee_pct`` and ``holding_costs_total_usd`` are SPEC §8.1 inputs with config
    defaults, so ``None`` means "use the default" rather than "zero". ``monthly_rent`` has no
    default - nothing stands in for what a property lets for - so ``None`` leaves the Rental
    and Take-Back analyses NOT_EVALUATED (SPEC §8.5, §8.6) rather than computed on a zero.

    ``flip_analysis`` and ``rental_analysis`` are the SPEC §8.1 toggles. ``None`` leaves the
    default the §3 exit implies; a bool is the team overriding it either way.

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
    closing_date: date
    term_months: int = Field(ge=1, le=60)
    interest_rate: Decimal = Field(ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    holding_costs_total_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    loan_purpose: LoanPurpose | None = None
    asset_type: AssetType | None = None
    stated_exit: StatedExit = StatedExit.UNKNOWN
    court_records: CourtRecordInputs | None = None

    @model_validator(mode="after")
    def _a_split_product_carries_its_split(self) -> UnderwriteInputs:
        """SPEC §8.2 prices the two portions; a split product without them cannot be priced.

        The screen can size one: it reports the commitment and no split. The underwrite
        cannot - the ledger's draw schedule is the rehab portion (SPEC §8.3), and what is
        advanced at close is the purchase portion - so the team enters the split before a
        deal is priced.
        """
        if self.deal.product in SPLIT_PRODUCTS and self.deal.loan_split is None:
            raise ValueError(
                f"underwrite requires the loan split on a {self.deal.product.value} deal: "
                "loan_purchase_portion and loan_rehab_portion"
            )
        return self

    @property
    def payoff_date(self) -> date:
        """``closing_date`` + ``term_months`` calendar months.  # SPEC §8.1"""
        return payoff_date_for(self.closing_date, self.term_months)


# --- Underwrite outputs (SPEC §8.3-8.8) --------------------------------------------------------


class AnalysisStatus(StrEnum):
    """Whether one of the three §8 analyses produced numbers, and why not.  # SPEC §8.4-§8.6

    ``OFF`` is a team decision: the toggle is off, so the analysis was not asked for.
    ``NOT_EVALUATED`` is a missing input: the Rental and Take-Back analyses are computed on a
    monthly rent, and nothing stands in for what a property lets for, so a deal without one
    gets no DSCR rather than a DSCR of zero - which would report a shortfall the deal has not
    been shown to have. Everything a figure depends on is null in both cases, and the cost
    side, which depends on neither, is reported regardless.
    """

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    OFF = "OFF"


class ExitInference(BaseModel):
    """The §3 exit and the two analysis toggles it defaulted.  # SPEC §3, §8.1"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: StatedExit  # stated by the team, else inferred from term x asset type
    exit_source: ExitSource  # STATED when the team set it, INFERRED when the engine did
    flip_analysis: bool  # the toggle in force
    rental_analysis: bool
    flip_analysis_default: bool  # what the exit implied, before any team override
    rental_analysis_default: bool


class DealEconomics(BaseModel):
    """The §8.1 Deal Economics group, resolved: inputs in force and what they imply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purchase_price: Decimal
    rehab_costs: Decimal
    contingency_pct: Decimal
    contingency: Decimal  # rehab_costs x contingency_pct
    rehab_adj: Decimal  # rehab_costs + contingency
    closing_costs: Decimal
    holding_costs_total: Decimal
    holding_costs_monthly: Decimal  # holding_costs_total / term_months
    origination_fee_pct: Decimal
    origination_at_close: Decimal  # half the fee, on the commitment
    origination_at_payoff: Decimal
    interest_rate: Decimal
    loan_requested: Decimal
    commitment: Decimal
    funded_at_close: Decimal
    loan_purchase_portion: Decimal | None
    loan_rehab_portion: Decimal | None


class LedgerEntry(BaseModel):
    """One month of the lender's cash flows.  # SPEC §8.3

    Signs are the lender's: ``funding`` and ``draws`` are money out and are negative or zero;
    ``interest``, ``fees`` and ``payoff`` are money in and are zero or positive. ``net`` is
    their sum, and it is the column the XIRR runs on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    date: date
    funding: Decimal
    draws: Decimal
    interest: Decimal
    fees: Decimal
    payoff: Decimal
    net: Decimal


class ReturnOverview(BaseModel):
    """The lender's dated monthly ledger and its XIRR.  # SPEC §8.3

    ``total_profit`` is the sum of the ``net`` column, which is also ``total_interest +
    total_fees``: every dollar funded comes back in the payoff, so the two cancel.

    ``irr`` is None only on a ledger with no sign change, which a positive amount funded at
    close makes unreachable in practice; it is None rather than zero because a rate that does
    not exist is not a rate of zero.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: list[LedgerEntry]
    total_funding: Decimal  # negative
    total_draws: Decimal  # negative
    total_interest: Decimal
    total_fees: Decimal
    total_payoff: Decimal
    total_profit: Decimal
    irr: Decimal | None


class FlipAnalysis(BaseModel):
    """The project's margin if the property is sold.  # SPEC §8.4

    The cost stack does not depend on the sale price, so it is reported whatever the status.
    ``profit_yield`` is profit over costs - a project margin, not an annualized return - and
    is labelled "Yield (Profit / Costs)" wherever it is shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    purchase_price: Decimal
    closing_costs: Decimal
    holding_costs_total: Decimal
    rehab_costs: Decimal
    contingency: Decimal
    financing_costs: Decimal  # total interest + both origination halves
    total_costs: Decimal
    broker_selling_pct: Decimal
    estimated_sale_price: Decimal | None
    broker_costs: Decimal | None
    net_profit: Decimal | None
    profit_yield: Decimal | None

    @model_validator(mode="after")
    def _evaluated_means_every_sale_figure_is_there(self) -> FlipAnalysis:
        return _status_agrees_with_figures(
            self.status,
            (self.estimated_sale_price, self.broker_costs, self.net_profit, self.profit_yield),
            "the estimated sale price feeds",
            self,
        )


class RentalAnalysis(BaseModel):
    """Whether the rent carries a takeout loan on the commitment.  # SPEC §8.5"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    expenses_pct: Decimal
    holding_costs_monthly: Decimal
    loan_amount: Decimal  # the commitment
    takeout_rate: Decimal
    amortization_years: int
    debt_service_monthly: Decimal
    dscr_floor: Decimal
    monthly_rent: Decimal | None
    expenses: Decimal | None
    net_monthly_income: Decimal | None
    dscr: Decimal | None
    passed: bool | None  # None when there was no DSCR to test

    @model_validator(mode="after")
    def _evaluated_means_every_rent_figure_is_there(self) -> RentalAnalysis:
        return _status_agrees_with_figures(
            self.status,
            (self.monthly_rent, self.expenses, self.net_monthly_income, self.dscr, self.passed),
            "the monthly rent feeds",
            self,
        )


class TakeBackAnalysis(BaseModel):
    """Whether the rent carries what the loan cost GLENWOOD, if it takes the property back.

    # SPEC §8.6. Always run: it is not a toggle, and the Rental toggle does not gate it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnalysisStatus
    loan_amount: Decimal  # the commitment
    interest_rate: Decimal
    lost_interest_months: int
    lost_interest: Decimal
    legal_costs: Decimal
    total_cost: Decimal  # loan_amount + lost_interest + legal_costs
    amortization_years: int
    debt_service_monthly: Decimal  # on total_cost, at the deal's own rate
    dscr_floor: Decimal
    net_monthly_income: Decimal | None
    dscr: Decimal | None  # dscr_at_loan_cost
    passed: bool | None

    @model_validator(mode="after")
    def _evaluated_means_every_rent_figure_is_there(self) -> TakeBackAnalysis:
        return _status_agrees_with_figures(
            self.status,
            (self.net_monthly_income, self.dscr, self.passed),
            "the monthly rent feeds",
            self,
        )


def _status_agrees_with_figures[T](
    status: AnalysisStatus, figures: tuple[object | None, ...], what: str, model: T
) -> T:
    """An analysis and its numbers cannot disagree about whether it was computed."""
    evaluated = status is AnalysisStatus.EVALUATED
    if evaluated and any(figure is None for figure in figures):
        raise ValueError(f"an EVALUATED analysis carries every figure {what}")
    if not evaluated and any(figure is not None for figure in figures):
        raise ValueError(f"a {status.value} analysis carries none of the figures {what}")
    return model


class UnderwriteResult(BaseModel):
    """Full underwrite output; stored whole on ``underwrites.outputs``.  # SPEC §8.7"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_version: str
    config_hash: str
    loan_purpose: LoanPurpose | None
    closing_date: date
    payoff_date: date
    term_months: int
    rehab_months: int
    exit: ExitInference
    sizing: SizingResult
    economics: DealEconomics
    return_overview: ReturnOverview
    flip: FlipAnalysis
    rental: RentalAnalysis
    take_back: TakeBackAnalysis
    flags: list[Flag]
