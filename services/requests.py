"""What the team supplies by hand, in the two shapes the queue collects it.  # SPEC §6.1, §8.1

``UnderwriteRequest`` is the SPEC §8.1 inputs at the moment of a run; ``TeamOverrides`` is
the standing block on the deal that the review queue edits.

Everything here comes from paid pulls, the valuation, or the team at the moment they
advance a deal: it is not intake, so most of it is not on ``deals``. Each optional value
resolves the same way - this request, then what intake already stored on the deal, then the
engine default:

    estimated_sale_price          request -> deal.estimated_sale_price_team -> no LTV and no
                                  flip (§7.4, §8.4)
    closing_date                  request -> deal.closing_date -> not ready (§8.1)
    term months / payoff date     request -> deal.term_months + term_stub_days -> not ready
    interest_rate                 request -> deal.interest_rate -> not ready (§8.1)
    contingency / closing costs   request -> deal -> the config default (§8.1)
    holding costs / origination   request -> deal -> the config default (§8.1)
    monthly_rent                  request -> deal.monthly_rent -> no DSCR at all (§8.5, §8.6)
    flip / rental toggles         request -> deal -> what the §3 exit implies (§8.1)
    loan purpose, asset type, exit   request -> deal -> unknown (§3)

Three have no third step and stop the run: the closing date, the term and the interest rate.
The ledger is dated months of interest (SPEC §8.3) and there is no defensible stand-in for
any of them - a deal priced at a rate nobody chose is not a priced deal.

The monthly rent is the one with a third step that is deliberately *nothing*. A percentage of
a value stands in for a cost the property incurs whatever it is worth, but nothing stands in
for what it lets for, and a zero rent would not be neutral: it would report a DSCR shortfall
on every deal whose rent nobody happened to look up. So a deal without one is underwritten
with the Rental and Take-Back analyses NOT_EVALUATED and an INFO flag saying so.

The loan split is not here at all. It is two columns on the deal, entered on the team-entry
form (SPEC §8.2), so there is one place it lives and one place it is edited. Nor is the
payoff date a column: it is the closing date plus the term, whole months and stub days
together (``schema/dates.py``), and both models below take a payoff date as an alternative
way of saying that term.

An adapter value, when one exists, wins over every step of that (``services/enrichment.py``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schema.dates import Term, term_from_dates
from schema.models import (
    AssetType,
    CourtRecordsStatus,
    LoanPurpose,
    Product,
    RepeatBorrowerStatus,
    StatedExit,
    TeamCourtRecord,
    validate_court_records,
)


class UnderwriteRequest(BaseModel):
    """SPEC §8.1 inputs supplied at underwrite time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Valuation (RicherValues or team override). Optional here because the team may
    # already have entered one on the deal.
    estimated_sale_price: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    # Verified borrower facts; they replace the self-reported tranche and bucket.
    verified_credit_score: int | None = Field(default=None, ge=300, le=850)
    verified_deals_36mo: int | None = Field(default=None, ge=0)
    repeat_borrower_verified: RepeatBorrowerStatus | None = None
    # The ledger's dates and rate (SPEC §8.3). Optional here because the team may already
    # have entered them on the deal; the underwrite still needs all three from somewhere.
    closing_date: date | None = None
    term_months: int | None = Field(default=None, ge=1, le=60)
    payoff_date: date | None = None
    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)
    # The §8.1 economics that carry a config default.
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    holding_costs_pct_of_cost: Decimal | None = Field(default=None, ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    # The Rental and Take-Back analyses (SPEC §8.5, §8.6).
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Overrides of what intake captured; None leaves the deal's own value in force.
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    loan_purpose: LoanPurpose | None = None
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None

    @model_validator(mode="after")
    def _term_and_payoff_agree(self) -> UnderwriteRequest:
        """A payoff date that cannot be reconciled with the term beside it says so here."""
        _ = self.requested_term
        return self

    @property
    def requested_term(self) -> Term | None:
        """The term this request names: the one typed, else the payoff date's.  # SPEC §8.1"""
        return term_from_dates(self.closing_date, self.term_months, self.payoff_date)


class TeamOverrides(BaseModel):
    """What the review queue lets a team member enter by hand on a deal.  # SPEC §6.1, §8.1

    The interim source, and the standing one. Until the Phase 3 adapters land the team is
    where the valuation and the court search come from; the rent, the rate, the dates and the
    §8.1 economics have no adapter planned at all, and the queue's override block is where
    all of it is typed. What it does not carry is the Property Overview and the borrower's
    own details - those are intake, and they are edited on the intake form.

    A submission replaces the whole block rather than patching it: the form is rendered with
    the deal's current values in it, so what comes back is the state the team means the deal
    to be in, and a field left blank means the deal should not carry that value. The audit row
    records the before and after of every field that actually moved.

    ``product`` is the exception. It can be inferred (SPEC §3), and ``product_source`` records
    which - so a product equal to the one already on the deal leaves both columns untouched
    rather than relabelling an inferred product as entered, and a blank leaves the inferred
    product in place. The form cannot un-set a product; nothing about a deal makes one stop
    being known.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Overview (SPEC §8.1)
    loan_purpose: LoanPurpose | None = None
    product: Product | None = None
    closing_date: date | None = None
    # Enter either; the other derives. ``payoff_date`` is never stored as a date
    # (``schema/dates.py``): it is the term's whole months plus its stub days.
    term_months: int | None = Field(default=None, ge=1, le=60)
    payoff_date: date | None = None
    # Deal economics (SPEC §8.1). Blank means the config default, except the rate, which has
    # none and without which the deal cannot be priced at all.
    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    holding_costs_pct_of_cost: Decimal | None = Field(default=None, ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    # Valuation and rent (SPEC §6.1); an adapter value still wins over either of these.
    estimated_sale_price_team: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Structure (SPEC §3): asset type and the stated exit drive the exit inference, which
    # defaults the two analysis toggles beside them.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    # The team's own court search (SPEC §7.2), one typed matter per entry.
    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> TeamOverrides:
        """The same check ``DealInfo`` makes, so the queue answers with a message, not a 500."""
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self

    @model_validator(mode="after")
    def _term_and_payoff_agree(self) -> TeamOverrides:
        """A payoff date that contradicts the term beside it says so at the door."""
        _ = self.requested_term
        return self

    @property
    def requested_term(self) -> Term | None:
        """The term this block names: the one typed, else the payoff date's.  # SPEC §8.1

        A payoff date that falls between two monthly anchors gives a term with a stub, which
        is a term the ledger prices like any other (SPEC §8.3).
        """
        return term_from_dates(self.closing_date, self.term_months, self.payoff_date)
