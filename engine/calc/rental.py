"""Rental analysis: the takeout - what the property has to let for to refinance us out.

# SPEC §8.5 (engine 2.1.0)

A takeout lender lends against the property's value once the work is done, not against our
loan. So the question is sized from the exit value down to the rent:

    takeout loan              = exit value x rental.takeout_ltv
    debt service, amortizing  = level monthly payment on the takeout loan at the takeout
                                rate over rental.amortization_years
    debt service, interest-only = takeout loan x takeout rate / 12
    required NOI              = amortizing debt service x rental.target_dscr
    required rent             = required NOI / (1 - expenses_pct)

and then set against the market:

    market NOI                = market rent x (1 - expenses_pct)
    gap                       = market rent - required rent
    DSCR (amortizing)         = market NOI / amortizing debt service
    DSCR (interest-only)      = market NOI / interest-only debt service
    spread to loan            = takeout loan - our payoff due

The **exit value** is the deal's own when the team typed one on the Underwriting Assumptions
panel and the estimated sale price otherwise (``exit_value``); the result says which. The
expense ratio and the takeout rate are the deal's own number when it carries one and the
config value otherwise (``engine/calc/terms.py``); the LTV, the amortization and the target
DSCR are config alone. **Our payoff due** is what this loan collects at payoff: the commitment
and the payoff half of the origination fee (``payoff_due``). Holding costs are not in this
analysis: a takeout lender underwrites the rent against its own debt service, and the carry
over our term is the flip's cost, not the rental's.

Two things can be missing. Without an exit value nothing above can be sized and the analysis
is ``NOT_EVALUATED``; the five assumptions are still reported, and so is the market rent when
there is one. Without a **market rent** the takeout, the required rent and the spread still
compute - they do not read the rent - and only the market-rent lines are ``NOT_EVALUATED``
(``market_rent_status``), with ``MONTHLY_RENT_MISSING`` saying so. Nothing stands in for what
a property lets for, so a missing rent is never a rent of zero.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.terms import LoanTerms, rental_expenses_pct, rental_takeout_rate
from schema.models import (
    AnalysisStatus,
    ExitValueSource,
    RentalAnalysis,
    UnderwriteInputs,
)

ZERO = Decimal(0)
ONE = Decimal(1)
TWELVE = Decimal(12)


def monthly_payment(principal: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    """Level payment on a fully amortizing loan: P x i / (1 - (1 + i)^-n), i = rate / 12."""
    if years < 1:
        raise ValueError(f"amortization years must be at least 1, got {years}")
    periods = years * 12
    rate = annual_rate / TWELVE
    if rate == ZERO:
        return principal / Decimal(periods)
    return principal * rate / (ONE - (ONE + rate) ** -periods)


def interest_only_payment(principal: Decimal, annual_rate: Decimal) -> Decimal:
    """One month's interest on ``principal`` and nothing else: P x rate / 12.  # SPEC §8.5"""
    return principal * annual_rate / TWELVE


def exit_value(inputs: UnderwriteInputs) -> tuple[Decimal, ExitValueSource] | None:
    """The value the takeout lends against, and where it came from.  # SPEC §8.5

    The deal's own when the team typed one, else the estimated sale price in force; None
    when there is neither, which leaves the analysis NOT_EVALUATED.
    """
    if inputs.rental_exit_value is not None:
        return inputs.rental_exit_value, ExitValueSource.ENTERED
    if inputs.deal.estimated_sale_price is not None:
        return inputs.deal.estimated_sale_price, ExitValueSource.SALE_PRICE
    return None


def payoff_due(loan: LoanTerms) -> Decimal:
    """What this loan collects at payoff: the commitment and the fee's payoff half.  # SPEC §8.3"""
    return loan.commitment + loan.origination_at_payoff


def required_rent(required_noi: Decimal, expenses_pct: Decimal) -> Decimal:
    """The rent that nets ``required_noi`` after ``expenses_pct`` of it goes to expenses."""
    if expenses_pct >= ONE:
        raise ValueError(
            f"an expense ratio of {expenses_pct} leaves no rent that nets anything: no rent is "
            "enough"
        )
    return required_noi / (ONE - expenses_pct)


def rental_analysis(inputs: UnderwriteInputs, loan: LoanTerms, config: Config) -> RentalAnalysis:
    """What the property has to let for to carry a takeout loan on its exit value.  # SPEC §8.5"""
    rental = config.rental
    expenses_pct = rental_expenses_pct(inputs, config)
    takeout_rate = rental_takeout_rate(inputs, config)
    market_rent = inputs.monthly_rent
    assumptions = {
        "expenses_pct": expenses_pct,
        "takeout_ltv": rental.takeout_ltv,
        "takeout_rate": takeout_rate,
        "amortization_years": rental.amortization_years,
        "target_dscr": rental.target_dscr,
        "market_rent": market_rent,
    }
    not_evaluated = {
        "market_noi": None,
        "rent_gap": None,
        "dscr_amortizing": None,
        "dscr_interest_only": None,
        "rent_passed": None,
    }
    resolved = exit_value(inputs)
    if resolved is None:
        return RentalAnalysis(
            status=AnalysisStatus.NOT_EVALUATED,
            market_rent_status=AnalysisStatus.NOT_EVALUATED,
            exit_value=None,
            exit_value_source=None,
            takeout_loan=None,
            debt_service_amortizing=None,
            debt_service_interest_only=None,
            required_noi=None,
            required_rent=None,
            payoff_due=None,
            spread_to_loan=None,
            spread_passed=None,
            **assumptions,
            **not_evaluated,
        )
    value, source = resolved
    takeout_loan = value * rental.takeout_ltv
    amortizing = monthly_payment(takeout_loan, takeout_rate, rental.amortization_years)
    interest_only = interest_only_payment(takeout_loan, takeout_rate)
    noi = amortizing * rental.target_dscr
    rent = required_rent(noi, expenses_pct)
    due = payoff_due(loan)
    spread = takeout_loan - due
    sized = {
        "status": AnalysisStatus.EVALUATED,
        "exit_value": value,
        "exit_value_source": source,
        "takeout_loan": takeout_loan,
        "debt_service_amortizing": amortizing,
        "debt_service_interest_only": interest_only,
        "required_noi": noi,
        "required_rent": rent,
        "payoff_due": due,
        "spread_to_loan": spread,
        "spread_passed": spread >= ZERO,
    }
    if market_rent is None:
        return RentalAnalysis(
            market_rent_status=AnalysisStatus.NOT_EVALUATED,
            **sized,
            **assumptions,
            **not_evaluated,
        )
    market_noi = market_rent * (ONE - expenses_pct)
    return RentalAnalysis(
        market_rent_status=AnalysisStatus.EVALUATED,
        market_noi=market_noi,
        rent_gap=market_rent - rent,
        dscr_amortizing=market_noi / amortizing if amortizing > ZERO else None,
        dscr_interest_only=market_noi / interest_only if interest_only > ZERO else None,
        rent_passed=market_rent >= rent,
        **sized,
        **assumptions,
    )
