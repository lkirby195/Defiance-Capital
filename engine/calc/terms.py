"""The sized loan plus every §8.1 economic the ledger and the three analyses run on.

# SPEC §8.1, §8.3

``LoanTerms`` is resolved once per underwrite and handed to everything downstream, so the
four inputs that carry a config default (``contingency_pct`` and ``closing_costs_usd``, both
resolved in ``engine.sizing``; ``origination_fee_pct`` and ``holding_costs_total_usd``,
resolved here) are each read from config in exactly one place. A deal therefore carries one
origination fee and one holding-cost figure everywhere - the ledger's fee rows, the flip's
financing cost, and the rental's monthly carry all read the same numbers.

The origination split is not a tunable: it is half at close and half at payoff (SPEC §3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from schema.dates import payoff_date_for
from schema.models import Product, SizingResult, UnderwriteInputs

ZERO = Decimal(0)
TWO = Decimal(2)
TWELVE = Decimal(12)


def rehab_months(term_months: int, config: Config) -> int:
    """rehab_months = max(0, term_months - listing_months).  # SPEC §8.3

    The last ``draws.listing_months`` of the term are listing and sale, so a term at or
    inside that period leaves no rehab period at all - and the rehab money is advanced at
    close instead of drawn (``NO_REHAB_PERIOD``, SPEC §8.8).
    """
    if term_months < 1:
        raise ValueError(f"term_months must be at least 1, got {term_months}")
    return max(0, term_months - config.draws.listing_months)


def origination_fee_pct(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """The origination fee in force: the team's own, else the config default.  # SPEC §8.1"""
    if inputs.origination_fee_pct is not None:
        return inputs.origination_fee_pct
    return config.fees.origination_default_pct


def holding_costs_total(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """Holding costs over the whole hold: the team's own, else the config percentage.

    # SPEC §8.1. The default is a percentage of ``purchase_price + rehab_costs`` - what the
    project costs before the lender's fees - rather than of a valuation, because what a house
    costs to sit on tracks the work being done to it, not what somebody thinks it is worth.
    """
    if inputs.holding_costs_total_usd is not None:
        return inputs.holding_costs_total_usd
    cost = inputs.deal.purchase_price + inputs.deal.rehab_costs
    return cost * config.fees.holding_costs_default_pct_of_cost


class LoanTerms(NamedTuple):
    """The sized loan and the term facts every §8 calculation needs.  # SPEC §8.1, §8.3"""

    sizing: SizingResult
    closing_date: date
    term_months: int
    rehab_months: int
    interest_rate: Decimal
    origination_fee_pct: Decimal
    origination_at_close: Decimal
    origination_at_payoff: Decimal
    holding_costs_total: Decimal

    @property
    def commitment(self) -> Decimal:
        return self.sizing.commitment

    @property
    def product(self) -> Product:
        return self.sizing.product

    @property
    def payoff_date(self) -> date:
        """The last row of the ledger: closing plus the term in calendar months."""
        return payoff_date_for(self.closing_date, self.term_months)

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
        """The hold's total carry spread over the term.  # SPEC §8.1, §8.5"""
        return self.holding_costs_total / Decimal(self.term_months)

    def monthly_interest(self, balance: Decimal) -> Decimal:
        """One month of interest on ``balance`` at the note rate.  # SPEC §8.3"""
        return balance * self.interest_rate / TWELVE


def loan_terms(sizing: SizingResult, inputs: UnderwriteInputs, config: Config) -> LoanTerms:
    """Resolve the §8.1 economics against config once, for everything downstream."""
    if sizing.commitment <= ZERO:
        raise ValueError(
            "a deal with no commitment cannot be priced: every §8 figure divides by it "
            f"(product {sizing.product.value}, loan requested {sizing.loan_requested})"
        )
    fee_pct = origination_fee_pct(inputs, config)
    half = sizing.commitment * fee_pct / TWO
    return LoanTerms(
        sizing=sizing,
        closing_date=inputs.closing_date,
        term_months=inputs.term_months,
        rehab_months=rehab_months(inputs.term_months, config),
        interest_rate=inputs.interest_rate,
        origination_fee_pct=fee_pct,
        origination_at_close=half,
        origination_at_payoff=half,
        holding_costs_total=holding_costs_total(inputs, config),
    )
