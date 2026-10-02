"""The public borrower form: what it stores, and how it becomes an intake.  # SPEC §4.1, §4.2

The WEB channel is the borrower typing the deal in themselves at ``/apply``. The form asks
the SPEC §4.1 minimum and nothing a borrower would not know: who they are, the property,
the three numbers, how long they need the money, and how they heard about us. It does not
ask the loan type - the normalizer infers one from the rehab budget, as it does for every
channel, and the team confirms it - and it does not ask anything the team form unlocks
(SPEC §8.1): no rate, no closing costs, no court search.

What a borrower types is not what the deal stores. The page takes ``$185,000`` and
``918-555-0142`` and the model here takes ``185000`` and ``9185550142``; the masks come off at
the HTML boundary (``api/apply_form.py``), exactly as they do on the team form. Every stored
value is the same code or number the team form would have stored - the queue, the engine and
the audit trail cannot tell which page a deal came in on except by its channel.

**The 12+ bucket seeds a 12-month term**, as it does on every channel (SPEC §4.1): a
borrower picking "12+ months" has said the floor of what they need, a deal with no term at
all cannot be priced, and the deal keeps the ``12_PLUS`` bucket beside the seed so the deal
page can say the borrower asked for more and the team can set the real number.

The two numbers a borrower may offer about the exit - the sale price after the rehab, and the
rent if they keep it - land on ``estimated_sale_price_borrower`` and ``monthly_rent_borrower``
(SPEC §4.2). They are the borrower's own estimates and are stored as such: the engine runs on
them only when no adapter and no team member has a number (SPEC §6.1), flags the run
``BORROWER_SOURCED_VALUES`` when it does, and a team entry replaces them as the value in
force without clearing them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intake.normalize import ParsedIntake
from schema.models import (
    BorrowerInfo,
    DealInfo,
    ExperienceBucket,
    PropertyInfo,
    State,
    StateSource,
    TermBucket,
    Tranche,
    months_for_bucket,
)

# The two-letter codes the state box offers; anything else the borrower could mean is OTHER.
STATE_NAMES: dict[State, str] = {State.OK: "Oklahoma", State.CO: "Colorado"}


class WebApplyForm(BaseModel):
    """The public form, in stored units, every required box present.  # SPEC §4.1, §4.2

    Required here is what the page marks Required: this model is what a submission that
    passed the page's own checks (``api/apply_form.py``) is built into, so a blank required
    box never reaches it. ``extra="forbid"`` so a control that drifts out of the page's field
    list is a refusal rather than a value silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    # About you
    borrower_name: str = Field(min_length=1, max_length=200)
    entity_name: str | None = Field(default=None, max_length=200)
    borrower_phone: str = Field(min_length=10, max_length=10, pattern=r"^\d{10}$")
    borrower_email: str = Field(min_length=3, max_length=254)
    credit_range: Tranche
    experience_bucket: ExperienceBucket
    repeat_borrower: bool
    # The property. ``state_entered`` is False when the state was read off the listing link
    # rather than picked, so the property records it as inferred (SPEC §4.5).
    address: str = Field(min_length=1, max_length=300)
    city: str = Field(min_length=1, max_length=100)
    state: State
    state_entered: bool = True
    listing_url: str | None = Field(default=None, max_length=2000)
    # The deal
    purchase_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    rehab_costs: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    loan_requested: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    estimated_sale_price: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Timing
    term_bucket: TermBucket
    closing_date: date | None = None
    # How they heard about us; stored on the deal as its referral note (SPEC §4.2).
    referral_note: str | None = Field(default=None, max_length=200)
    # Consent: given by submitting the form, under the line that says so above the button
    # (``api/i18n.py``); the stored form keeps the fact that it was.
    consent: Literal[True]

    @property
    def address_line(self) -> str:
        """``address_raw`` as the other channels write it: street, city, state code."""
        parts = [self.address, self.city]
        if self.state is not State.OTHER:
            parts.append(self.state.value)
        return ", ".join(parts)


def parse_web_form(form: WebApplyForm) -> ParsedIntake:
    """Map the public form onto the channel-agnostic ``ParsedIntake``."""
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
            address_raw=form.address_line,
            listing_url=form.listing_url,
            city=form.city,
            state=form.state,
            state_source=StateSource.ENTERED if form.state_entered else StateSource.INFERRED,
        ),
        deal=DealInfo(
            purchase_price=form.purchase_price,
            rehab_costs=form.rehab_costs,
            loan_requested=form.loan_requested,
            term_bucket=form.term_bucket,
            term_months=months_for_bucket(form.term_bucket),
            closing_date=form.closing_date,
            estimated_sale_price_borrower=form.estimated_sale_price,
            monthly_rent_borrower=form.monthly_rent,
        ),
    )
