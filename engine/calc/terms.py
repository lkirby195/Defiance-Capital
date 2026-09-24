"""The sized loan plus every §8.1 economic the ledger and the three analyses run on.

# SPEC §8.1, §8.3

``LoanTerms`` is resolved once per underwrite and handed to everything downstream, so the
four inputs that carry a config default (``contingency_pct`` and ``closing_costs_usd``, both
resolved in ``engine.sizing``; ``origination_fee_pct`` and ``holding_costs_pct_of_cost``,
resolved here) are each read from config in exactly one place. A deal therefore carries one
origination fee and one holding-cost figure everywhere - the ledger's fee rows, the flip's
financing cost, and the rental's monthly carry all read the same numbers.

The term is whole monthly periods plus a stub (``schema/dates.py``). ``term_months`` is the
count of full periods and is what the draw schedule and the rehab period are measured in;
``stub_days`` is whatever a payoff date between two anchors leaves over, and it accrues
interest prorated over ``interest.day_count_basis``. ``term_months_decimal`` is the two as one
number, and it is what the monthly holding cost divides by - a hold that runs nine months and
eleven days carries 9.3667 months of costs, not nine.

The origination split is not a tunable: it is half at close and half at payoff (SPEC §3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from schema.dates import Term, describe, payoff_date_for
from schema.models import Product, SizingResult, UnderwriteInputs

ZERO = Decimal(0)
TWO = Decimal(2)
TWELVE = Decimal(12)


def rehab_months(term_months: int, config: Config) -> int:
    """rehab_months = max(0, term_months - listing_months).  # SPEC §8.3

    The last ``draws.listing_months`` of the term are listing and sale, so a term at or
    inside that period leaves no rehab period at all - and the rehab money is advanced at
    close instead of drawn (``NO_REHAB_PERIOD``, SPEC §8.8). Whole periods only: a stub is
    days at the end of the term and no part of it is a month anybody draws over.
    """
    if term_months < 0:
        raise ValueError(f"term_months cannot be negative, got {term_months}")
    return max(0, term_months - config.draws.listing_months)


def origination_fee_pct(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """The origination fee in force: the team's own, else the config default.  # SPEC §8.1"""
    if inputs.origination_fee_pct is not None:
        return inputs.origination_fee_pct
    return config.fees.origination_default_pct


def holding_costs_pct(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """The holding-cost percentage in force: the team's own, else the config default.

    # SPEC §8.1. The input is a percentage of ``purchase_price + rehab_costs`` - what the
    project costs before the lender's fees - rather than a dollar figure, because what a
    house costs to sit on tracks the work being done to it, and a percentage goes on being
    true when the price or the rehab budget moves.
    """
    if inputs.holding_costs_pct_of_cost is not None:
        return inputs.holding_costs_pct_of_cost
    return config.fees.holding_costs_default_pct_of_cost


def holding_costs_basis(inputs: UnderwriteInputs) -> Decimal:
    """What the holding-cost percentage is a percentage of: price plus rehab.  # SPEC §8.1"""
    return inputs.deal.purchase_price + inputs.deal.rehab_costs


def holding_costs_total(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """Holding costs over the whole hold, in dollars.  # SPEC §8.1"""
    return holding_costs_basis(inputs) * holding_costs_pct(inputs, config)


class LoanTerms(NamedTuple):
    """The sized loan and the term facts every §8 calculation needs.  # SPEC §8.1, §8.3"""

    sizing: SizingResult
    closing_date: date
    term_months: int  # whole monthly periods
    stub_days: int  # days past the last anchor; 0 on a whole-month term
    day_count_basis: int  # days a whole period counts as, for the stub (SPEC §8.3)
    rehab_months: int
    interest_rate: Decimal
    origination_fee_pct: Decimal
    origination_at_close: Decimal
    origination_at_payoff: Decimal
    holding_costs_pct_of_cost: Decimal
    holding_costs_basis: Decimal  # purchase_price + rehab_costs
    holding_costs_total: Decimal

    @property
    def commitment(self) -> Decimal:
        return self.sizing.commitment

    @property
    def product(self) -> Product:
        return self.sizing.product

    @property
    def term(self) -> Term:
        return Term(self.term_months, self.stub_days)

    @property
    def has_stub(self) -> bool:
        """True when the payoff date falls between two anchors.  # SPEC §8.1"""
        return self.stub_days > 0

    @property
    def payoff_date(self) -> date:
        """The last row of the ledger: the term's anchor, plus the stub days."""
        return payoff_date_for(self.closing_date, self.term_months, self.stub_days)

    @property
    def term_months_decimal(self) -> Decimal:
        """The term as one number of months: full periods plus the stub's share of one.

        # SPEC §8.1, §8.5. The stub is a fraction of a period on the same day-count basis its
        interest is prorated on, so the monthly holding cost and the stub's interest agree
        about how much of a month those days are.
        """
        return Decimal(self.term_months) + Decimal(self.stub_days) / Decimal(self.day_count_basis)

    @property
    def term_description(self) -> str:
        """The term as a person reads it: ``9 month(s) and 11 day(s)``."""
        return describe(self.term)

    @property
    def rehab_portion(self) -> Decimal:
        """The rehab side of the commitment; zero on a product that has no split."""
        return ZERO if self.sizing.split is None else self.sizing.split.rehab_portion

    @property
    def purchase_portion(self) -> Decimal:
        """The purchase side of the commitment; the whole of it on a product with no split."""
        if self.sizing.split is None:
            return self.commitment
        return self.sizing.split.purchase_portion

    @property
    def drawn_over_time(self) -> bool:
        """True when there is rehab money and a rehab period to draw it over.  # SPEC §8.3"""
        return self.rehab_months > 0 and self.rehab_portion > ZERO

    @property
    def funded_at_close(self) -> Decimal:
        """Dollars out the door at closing.  # SPEC §8.3

        The sized ``funded_at_close`` - the purchase portion on a split product, the whole
        commitment on the other two - unless there is no rehab period to draw over, in which
        case the rehab money goes out at close as well.
        """
        if self.drawn_over_time:
            return self.sizing.funded_at_close
        return self.sizing.funded_at_close + self.rehab_portion

    @property
    def holding_costs_monthly(self) -> Decimal:
        """The hold's total carry spread over the term.  # SPEC §8.1, §8.5

        Over ``term_months_decimal``, not over the whole periods: a hold that runs eleven days
        past its last anchor carries eleven days more cost, and dividing by the whole months
        alone would report a monthly figure the deal never pays.
        """
        return self.holding_costs_total / self.term_months_decimal

    def monthly_interest(self, balance: Decimal) -> Decimal:
        """One whole period of interest on ``balance`` at the note rate.  # SPEC §8.3"""
        return balance * self.interest_rate / TWELVE

    def stub_interest(self, balance: Decimal) -> Decimal:
        """The final stub period's interest, prorated by its days.  # SPEC §8.3

        One month's interest scaled by ``stub_days / day_count_basis``: on the placeholder 30
        an eleven-day stub accrues eleven thirtieths of a month.
        """
        days = Decimal(self.stub_days) / Decimal(self.day_count_basis)
        return self.monthly_interest(balance) * days


def loan_terms(sizing: SizingResult, inputs: UnderwriteInputs, config: Config) -> LoanTerms:
    """Resolve the §8.1 economics against config once, for everything downstream."""
    if sizing.commitment <= ZERO:
        raise ValueError(
            "a deal with no commitment cannot be priced: every §8 figure divides by it "
            f"(product {sizing.product.value}, loan requested {sizing.loan_requested})"
        )
    term = Term(inputs.term_months, inputs.term_stub_days)
    if not term.is_positive:
        raise ValueError(
            "a deal with no term cannot be priced: the ledger runs from the closing date to "
            "the payoff date, and this deal pays off on the day it closes"
        )
    fee_pct = origination_fee_pct(inputs, config)
    half = sizing.commitment * fee_pct / TWO
    pct = holding_costs_pct(inputs, config)
    basis = holding_costs_basis(inputs)
    return LoanTerms(
        sizing=sizing,
        closing_date=inputs.closing_date,
        term_months=term.full_months,
        stub_days=term.stub_days,
        day_count_basis=config.interest.day_count_basis,
        rehab_months=rehab_months(term.full_months, config),
        interest_rate=inputs.interest_rate,
        origination_fee_pct=fee_pct,
        origination_at_close=half,
        origination_at_payoff=half,
        holding_costs_pct_of_cost=pct,
        holding_costs_basis=basis,
        holding_costs_total=basis * pct,
    )
