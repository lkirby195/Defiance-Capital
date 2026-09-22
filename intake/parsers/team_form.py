"""Team-entry form: the borrower's intake fields plus the team-only extras.  # SPEC §4.1, §4.2

The TEAM channel is "Direct": nothing to extract, the form maps straight onto
``ParsedIntake``. Any field may be omitted so a team member can capture a partial
inquiry from a call; the normalizer reports what is still missing.

The team-only block also carries the stand-ins for enrichment (SPEC §6): a valuation and a
court search the team did by hand. They are used only where no adapter has produced the
same value, and the form validates them the same way the engine would - a court search with
findings has to say what it found and when.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intake.normalize import ParsedIntake, infer_product
from schema.models import (
    AssetType,
    BorrowerInfo,
    CourtRecordsStatus,
    DealInfo,
    ExperienceBucket,
    Product,
    ProductSource,
    PropertyInfo,
    State,
    StatedExit,
    StateSource,
    TeamCourtRecord,
    TermBucket,
    Tranche,
    validate_court_records,
    validate_loan_split,
    validate_term_months,
)


class TeamEntryForm(BaseModel):
    """Flat form payload posted by the review-queue page.  # SPEC §4.1"""

    model_config = ConfigDict(extra="forbid")

    # 6. Who you are
    borrower_name: str | None = None
    borrower_phone: str | None = None
    borrower_email: str | None = None
    entity_name: str | None = None
    credit_range: Tranche | None = None
    experience_bucket: ExperienceBucket | None = None
    repeat_borrower: bool | None = None
    # 1. Property
    address: str | None = None
    listing_url: str | None = None
    county: str | None = None
    state: State | None = None
    # 2-5. Deal
    purchase_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rehab_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # The split of the loan requested, on the two split products only (SPEC §8.2). The web
    # form requires both when the product is one of those (``api/intake_form.py``); a JSON
    # post of a partial intake may carry neither, which is a deal nobody has divided yet.
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    term_bucket: TermBucket | None = None
    # Derived from the bucket and shown read-only for every bucket that names a number of
    # months; typed by the team for 12_PLUS, which names none (SPEC §8.1).
    term_months: int | None = Field(default=None, ge=1, le=60)
    # Team-only extras (SPEC §4.2 "extra ones unlocked")
    asset_type: AssetType | None = None  # with the term, drives the exit inference (SPEC §3)
    stated_exit: StatedExit | None = None
    product: Product | None = None  # inferred from the rehab budget when omitted (SPEC §3)
    # Actual annual taxes / insurance in USD; override the %-of-value defaults (SPEC §8.6)
    actual_annual_taxes_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    actual_annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    # The other two SPEC §8.1 team inputs; no config default, no adapter (SPEC §6).
    actual_annual_utilities_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    market_rent_monthly: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Team-supplied valuation and court search, used until the Phase 3 adapters land
    # (SPEC §6). An adapter value always wins over either of these.
    as_is_value_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    arv_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None  # the day the team searched; lookbacks run from it
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @property
    def effective_product(self) -> Product | None:
        """The product this form produces: the one chosen, else the inferred one.  # SPEC §3

        The product box may be left blank, in which case the normalizer infers it from the
        rehab budget. The split rules are about the product the deal will end up with, so
        they are tested against that rather than against an empty box.
        """
        if self.product is not None:
            return self.product
        if self.rehab_budget is None:
            return None
        return infer_product(self.rehab_budget)

    @model_validator(mode="after")
    def _term_is_coherent(self) -> TeamEntryForm:
        """The same check ``DealInfo`` makes, so a tampered read-only box says so at the door."""
        validate_term_months(self.term_bucket, self.term_months)
        return self

    @model_validator(mode="after")
    def _loan_split_is_coherent(self) -> TeamEntryForm:
        """The same check ``DealInfo`` makes, against the product the form will produce."""
        validate_loan_split(
            self.effective_product,
            self.loan_requested,
            self.loan_purchase_portion,
            self.loan_rehab_portion,
        )
        return self

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> TeamEntryForm:
        """The same check ``DealInfo`` makes, so the endpoint answers 422 rather than 500."""
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self


def parse_team_form(form: TeamEntryForm) -> ParsedIntake:
    """Map the flat form onto the channel-agnostic ``ParsedIntake``."""
    return ParsedIntake(
        borrower=BorrowerInfo(
            name=form.borrower_name,
            phone=form.borrower_phone,
            email=form.borrower_email,
            entity_name=form.entity_name,
            credit_range=form.credit_range,
            experience_bucket=form.experience_bucket,
            repeat_borrower=form.repeat_borrower,
        ),
        property=PropertyInfo(
            address_raw=form.address,
            listing_url=form.listing_url,
            county=form.county,
            state=form.state or State.OTHER,
            state_source=StateSource.ENTERED if form.state is not None else StateSource.INFERRED,
        ),
        deal=DealInfo(
            purchase_price=form.purchase_price,
            rehab_budget=form.rehab_budget,
            loan_requested=form.loan_requested,
            loan_purchase_portion=form.loan_purchase_portion,
            loan_rehab_portion=form.loan_rehab_portion,
            term_bucket=form.term_bucket,
            term_months=form.term_months,
            asset_type=form.asset_type,
            stated_exit=form.stated_exit,
            product=form.product,
            product_source=ProductSource.ENTERED if form.product is not None else None,
            actual_annual_taxes_usd=form.actual_annual_taxes_usd,
            actual_annual_insurance_usd=form.actual_annual_insurance_usd,
            actual_annual_utilities_usd=form.actual_annual_utilities_usd,
            market_rent_monthly=form.market_rent_monthly,
            as_is_value_team=form.as_is_value_team,
            arv_team=form.arv_team,
            court_records_status=form.court_records_status,
            court_records_as_of=form.court_records_as_of,
            court_records_team=list(form.court_records_team),
        ),
    )
