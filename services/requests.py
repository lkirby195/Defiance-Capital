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
    the five analysis assumptions request -> deal -> the config value (§8.4, §8.5, §8.6)
    monthly_rent                  request -> deal.monthly_rent -> no DSCR at all (§8.5, §8.6)
    flip / rental toggles         request -> deal -> on when the price / the rent is there (§8.1)
    loan purpose                  request -> deal -> unstated

``UnderwritingAssumptions`` is the deal page's Underwriting Assumptions panel: the sixteen
boxes a person edits and saves with one button, and nothing else - not the dates, the term,
the product or the court search, which the intake form and the court section own.
``CourtSearch`` is that section. Each replaces exactly the columns it renders, so a save of
one cannot clear a value the other owns.

Three have no third step and stop the run: the closing date, the term and the interest rate.
The ledger is dated months of interest (SPEC §8.3) and there is no defensible stand-in for
any of them - a deal priced at a rate nobody chose is not a priced deal.

The monthly rent is the one with a third step that is deliberately *nothing*. A percentage of
a value stands in for a cost the property incurs whatever it is worth, but nothing stands in
for what it lets for, and a zero rent would not be neutral: it would report a DSCR shortfall
on every deal whose rent nobody happened to look up. So a deal without one is underwritten
with the Rental and Take-Back analyses NOT_EVALUATED and an INFO flag saying so.

The loan split is on ``TeamOverrides`` and not on ``UnderwriteRequest``: it is two columns
on the deal (SPEC §8.2), entered on the team-entry form or the override block and defaulted
by the §8.2 formula when neither has, and a run reads it off the deal rather than being
handed one. The payoff date is not a column anywhere: it is the end of the month that is the
closing month plus the term, plus the stub days (``schema/dates.py``). ``UnderwriteRequest``
still takes a payoff date - the JSON route is where an actual payoff will one day be entered,
and the stub arithmetic stays in the engine for it - and ``TeamOverrides`` does not: the
override block takes the term in months and shows the payoff date it derives, read-only.

An adapter value, when one exists, wins over every step of that (``services/enrichment.py``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schema.dates import Term, term_from_dates
from schema.models import (
    CourtRecordsStatus,
    LoanPurpose,
    Product,
    RepeatBorrowerStatus,
    TeamCourtRecord,
    validate_court_records,
    validate_loan_split,
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
    # The §8.4-§8.6 analysis assumptions, each with the config value behind it.
    broker_selling_pct: Decimal | None = Field(default=None, ge=0, le=1)
    rental_expenses_pct_of_rent: Decimal | None = Field(default=None, ge=0, le=1)
    rental_takeout_rate: Decimal | None = Field(default=None, ge=0, le=1)
    take_back_legal_costs_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    take_back_lost_interest_months: int | None = Field(default=None, ge=0, le=60)
    # The Rental and Take-Back analyses (SPEC §8.5, §8.6).
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Overrides of what intake captured; None leaves the deal's own value in force.
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None
    loan_purpose: LoanPurpose | None = None

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

    # Deal economics (SPEC §8.1): the loan's own terms. The closing date is any date; the
    # ledger anchors to the end of its month. The term is months, and the payoff date is
    # derived from the two and shown read-only (``schema/dates.py``).
    loan_purpose: LoanPurpose | None = None
    product: Product | None = None
    closing_date: date | None = None
    term_months: int | None = Field(default=None, ge=1, le=60)
    # Deal economics (SPEC §8.1). Blank means the config default, except the rate, which has
    # none and without which the deal cannot be priced at all.
    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    holding_costs_pct_of_cost: Decimal | None = Field(default=None, ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    # The loan split on a split product (SPEC §8.2): both or neither. Neither means the
    # §8.2 formula split stands in (``services/defaults.py``); whether the two add up to the
    # loan amount is checked against the deal when the block is applied, because the block
    # does not carry the loan amount.
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Valuation and rent (SPEC §6.1); an adapter value still wins over either of these.
    estimated_sale_price_team: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # The §8.4-§8.6 analysis assumptions (Phase 7c). Blank means the config value.
    broker_selling_pct: Decimal | None = Field(default=None, ge=0, le=1)
    rental_expenses_pct_of_rent: Decimal | None = Field(default=None, ge=0, le=1)
    rental_takeout_rate: Decimal | None = Field(default=None, ge=0, le=1)
    take_back_legal_costs_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    take_back_lost_interest_months: int | None = Field(default=None, ge=0, le=60)
    # The two analysis toggles (SPEC §8.1): blank is the default - on when the sale price,
    # or the rent, is on the deal - and On / Off is a person overriding it.
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
    def _split_is_both_or_neither(self) -> TeamOverrides:
        """Half a split is refused here; the rest of the rule needs the deal (SPEC §8.2)."""
        validate_loan_split(None, None, self.loan_purchase_portion, self.loan_rehab_portion)
        return self

    @property
    def requested_term(self) -> Term | None:
        """The term this block names, whole months and no stub; None when the box was blank."""
        return None if self.term_months is None else Term(self.term_months, 0)


class UnderwritingAssumptions(BaseModel):
    """The deal page's Underwriting Assumptions panel.  # SPEC §8.1, §8.4, §8.5, §8.6

    Sixteen boxes, two columns, one Save & Run: the five §8.1 economics with a config
    default, the loan split on a split product, the valuation and the rent, the five
    analysis assumptions, and the two toggles. A submission replaces exactly these columns
    - the form is rendered with the deal's current values in it, so what comes back is the
    state the team means - and a blank on anything with a default is the default again
    (``services/defaults.py``); a blank sale price or rent is no value, because nothing
    stands in for either. The dates, the term and the product are the intake form's; the
    court search is its own section (``CourtSearch``). Neither is touched by a save here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    interest_rate: Decimal | None = Field(default=None, ge=0, le=1)
    origination_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    contingency_pct: Decimal | None = Field(default=None, ge=0, le=1)
    closing_costs_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    holding_costs_pct_of_cost: Decimal | None = Field(default=None, ge=0, le=1)
    loan_purchase_portion: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    loan_rehab_portion: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    estimated_sale_price_team: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    broker_selling_pct: Decimal | None = Field(default=None, ge=0, le=1)
    rental_expenses_pct_of_rent: Decimal | None = Field(default=None, ge=0, le=1)
    rental_takeout_rate: Decimal | None = Field(default=None, ge=0, le=1)
    take_back_legal_costs_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    take_back_lost_interest_months: int | None = Field(default=None, ge=0, le=60)
    flip_analysis: bool | None = None
    rental_analysis: bool | None = None

    @model_validator(mode="after")
    def _split_is_both_or_neither(self) -> UnderwritingAssumptions:
        """Half a split is refused here; the rest of the rule needs the deal (SPEC §8.2)."""
        validate_loan_split(None, None, self.loan_purchase_portion, self.loan_rehab_portion)
        return self


class CourtSearch(BaseModel):
    """The deal page's court search section: the team's own search (SPEC §7.2), by hand.

    The outcome, the day it was searched, and one typed matter per entry; the same coherence
    rule ``DealInfo`` applies, so the page answers with a message rather than a 500. A save
    replaces the three columns and nothing else.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> CourtSearch:
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self
