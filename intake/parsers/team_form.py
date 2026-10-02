"""Team-entry form: the borrower's intake fields plus the team-only extras.  # SPEC §4.1, §4.2

The TEAM channel is "Direct": nothing to extract, the form maps straight onto
``ParsedIntake``. Any field may be omitted so a team member can capture a partial
inquiry from a call; the normalizer reports what is still missing.

The fields are grouped as SPEC §8.1 groups them - Overview, Property Overview, Deal
Economics - because that is the order the page asks for them in and the order every output
shows them back. The Overview is who the borrower is; the loan's own terms - its purpose, its
type, its closing date and its term - are Deal Economics, beside the money they describe.

The HTML form (``api/intake_form.py``) renders a subset of this model: the Overview, the
Property Overview, and seven Deal Economics boxes. The §8.1 economics with a config default,
the loan split, the valuation, the rent, the analysis toggles and the court search have no
box on it - they populate from defaults and are edited on the deal page - and stay on the
model for the JSON half of ``POST /intake/team`` and for the fixtures the CLI runs.

The team form asks for the term in months and not as the SPEC §4.1 bucket. The bucket is the
borrower's own answer to "how long do you need the loan?", which is a question the borrower
channels ask and a person with the whole deal in front of them does not: they know the term,
and entering a bucket beside it would be a second number to keep in step with the first. Nor
does it take a payoff date: that is derived, the last day of the month that is the closing
month plus the term (``schema/dates.py``), and shown read-only on the deal page.

The team-only block also carries the stand-ins for enrichment (SPEC §6): a valuation, a rent
and a court search the team did by hand. They are used only where no adapter has produced the
same value, and the model validates them the same way the engine would - a court search with
findings has to say what it found and when.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intake.normalize import ParsedIntake, infer_product
from schema.dates import Term
from schema.models import (
    BorrowerInfo,
    CourtRecordsStatus,
    DealInfo,
    ExperienceBucket,
    LoanPurpose,
    Product,
    ProductSource,
    PropertyInfo,
    State,
    StateSource,
    TeamCourtRecord,
    Tranche,
    validate_court_records,
    validate_loan_split,
)


class TeamEntryForm(BaseModel):
    """Flat form payload posted by the review-queue page.  # SPEC §4.1, §8.1"""

    model_config = ConfigDict(extra="forbid")

    # Overview (SPEC §8.1): who the borrower is. ``borrower_name`` is the name on the
    # guarantee - the box is captioned "Guarantor Name" - and is the only name a deal has.
    entity_name: str | None = None
    borrower_name: str | None = None
    borrower_phone: str | None = None  # optional; the match key when there is one (SPEC §5)
    borrower_email: str | None = None
    credit_range: Tranche | None = None
    experience_bucket: ExperienceBucket | None = None
    repeat_borrower: bool | None = None
    # Property Overview (SPEC §8.1)
    address: str | None = None
    listing_url: str | None = None
    city: str | None = None
    state: State | None = None
    units: int | None = Field(default=None, ge=0)
    structures: int | None = Field(default=None, ge=0)
    sf: int | None = Field(default=None, ge=0)
    year_built: int | None = Field(default=None, ge=1600, le=2200)
    year_renovated: int | None = Field(default=None, ge=1600, le=2200)
    beds: int | None = Field(default=None, ge=0)
    baths: Decimal | None = Field(default=None, ge=0, max_digits=4, decimal_places=1)
    garage_spaces: int | None = Field(default=None, ge=0)
    # Deal Economics (SPEC §8.1). Everything but the price, the costs, the loan and the rate
    # falls back to a config default when the box is left blank.
    loan_purpose: LoanPurpose | None = None
    product: Product | None = None  # "Loan Type"; inferred from the rehab costs when omitted
    closing_date: date | None = None  # any date; the ledger's month 0 is its month end (§8.3)
    # Whole months; the payoff date derives (SPEC §8.1). The form requires it
    # (``api/intake_form.py``); a JSON post of a partial intake may leave it out.
    term_months: int | None = Field(default=None, ge=1, le=60)
    purchase_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rehab_costs: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # The split of the loan requested, on the two split products only (SPEC §8.2). No box
    # on the form: the deal is populated with the §8.2 formula split and the deal page edits
    # it. A JSON post may carry one.
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # A share of purchase_price + rehab_costs over the whole hold (SPEC §8.1); the dollar
    # figure is computed from it and is never entered.
    holding_costs_pct_of_cost: Decimal | None = Field(default=None, ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    # The two analysis toggles (SPEC §8.1); blank leaves the default in force.
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    # Team-supplied valuation, rent and court search, used until the Phase 3 adapters land
    # (SPEC §6). An adapter value always wins over any of these.
    estimated_sale_price_team: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None  # the day the team searched; lookbacks run from it
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @property
    def effective_product(self) -> Product | None:
        """The product this form produces: the one chosen, else the inferred one.  # SPEC §3

        The product box may be left blank, in which case the normalizer infers it from the
        rehab costs. The split rules are about the product the deal will end up with, so
        they are tested against that rather than against an empty box.
        """
        if self.product is not None:
            return self.product
        if self.rehab_costs is None:
            return None
        return infer_product(self.rehab_costs)

    @property
    def effective_term(self) -> Term | None:
        """The term this form produces: whole months, no stub.  # SPEC §8.1

        ``None`` when the box was left blank, which the HTML form refuses
        (``api/intake_form.py``): a deal nobody has said the length of cannot be priced.
        """
        return None if self.term_months is None else Term(self.term_months, 0)

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
    term = form.effective_term
    return ParsedIntake(
        borrower=BorrowerInfo(
            name=form.borrower_name,  # the guarantor (SPEC §8.1)
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
            city=form.city,
            state=form.state or State.OTHER,
            state_source=StateSource.ENTERED if form.state is not None else StateSource.INFERRED,
            units=form.units,
            structures=form.structures,
            sf=form.sf,
            year_built=form.year_built,
            year_renovated=form.year_renovated,
            beds=form.beds,
            baths=form.baths,
            garage_spaces=form.garage_spaces,
        ),
        deal=DealInfo(
            loan_purpose=form.loan_purpose,
            closing_date=form.closing_date,
            purchase_price=form.purchase_price,
            rehab_costs=form.rehab_costs,
            loan_requested=form.loan_requested,
            loan_purchase_portion=form.loan_purchase_portion,
            loan_rehab_portion=form.loan_rehab_portion,
            term_months=term.full_months if term is not None else None,
            # NULL rather than 0: a term said in months has no stub on the end of it.
            term_stub_days=None,
            interest_rate=form.interest_rate,
            contingency_pct=form.contingency_pct,
            closing_costs_usd=form.closing_costs_usd,
            holding_costs_pct_of_cost=form.holding_costs_pct_of_cost,
            origination_fee_pct=form.origination_fee_pct,
            product=form.product,
            product_source=ProductSource.ENTERED if form.product is not None else None,
            flip_analysis=form.flip_analysis,
            rental_analysis=form.rental_analysis,
            monthly_rent=form.monthly_rent,
            estimated_sale_price_team=form.estimated_sale_price_team,
            court_records_status=form.court_records_status,
            court_records_as_of=form.court_records_as_of,
            court_records_team=list(form.court_records_team),
        ),
    )
