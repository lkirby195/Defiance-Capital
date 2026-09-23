"""Rental and Take-Back: the two DSCR tests.  # SPEC §8.5, §8.6

Both ask whether the rent covers a level monthly payment, and they differ in what that
payment is on:

* **Rental** (§8.5) - a takeout lender refinancing GLENWOOD out: the commitment, at
  ``rental.takeout_rate`` over ``rental.amortization_years``.
* **Take-Back** (§8.6) - GLENWOOD carrying the property itself: the commitment plus the
  interest it stopped collecting plus the legal bill, at the deal's **own** note rate. That
  is the point of the second test - it is GLENWOOD's money at GLENWOOD's price, not a
  borrower's at somebody else's.

They share one ``net_monthly_income``:

    expenses           = monthly_rent x rental.expenses_pct_of_rent
    net_monthly_income = monthly_rent - expenses - holding_costs_total / term_months

Take-Back is not gated by the Rental toggle: a flip deal with a rent on it still gets one.
Neither can be computed without a rent - nothing stands in for what a property lets for - so
without one both report NOT_EVALUATED and the underwrite raises MONTHLY_RENT_MISSING rather
than reporting a DSCR of zero, which would be a shortfall the deal has not been shown to have.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.terms import LoanTerms
from schema.models import (
    AnalysisStatus,
    RentalAnalysis,
    TakeBackAnalysis,
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


def net_monthly_income(monthly_rent: Decimal, loan: LoanTerms, config: Config) -> Decimal:
    """Rent less operating expenses less the monthly holding cost.  # SPEC §8.5"""
    expenses = monthly_rent * config.rental.expenses_pct_of_rent
    return monthly_rent - expenses - loan.holding_costs_monthly


def _dscr(income: Decimal, debt_service: Decimal) -> Decimal:
    """Income over debt service. ``loan_terms`` has already refused a zero commitment."""
    if debt_service <= ZERO:  # pragma: no cover - a positive commitment always amortizes
        raise ValueError(f"debt service must be positive to compute a DSCR, got {debt_service}")
    return income / debt_service


def rental_analysis(
    inputs: UnderwriteInputs, loan: LoanTerms, on: bool, config: Config
) -> RentalAnalysis:
    """Whether the rent carries a takeout loan on the commitment.  # SPEC §8.5"""
    rental = config.rental
    debt_service = monthly_payment(loan.commitment, rental.takeout_rate, rental.amortization_years)
    common = {
        "expenses_pct": rental.expenses_pct_of_rent,
        "holding_costs_monthly": loan.holding_costs_monthly,
        "loan_amount": loan.commitment,
        "takeout_rate": rental.takeout_rate,
        "amortization_years": rental.amortization_years,
        "debt_service_monthly": debt_service,
        "dscr_floor": rental.dscr_floor,
    }
    rent = inputs.monthly_rent
    if not on or rent is None:
        return RentalAnalysis(
            status=AnalysisStatus.OFF if not on else AnalysisStatus.NOT_EVALUATED,
            monthly_rent=None,
            expenses=None,
            net_monthly_income=None,
            dscr=None,
            passed=None,
            **common,
        )
    income = net_monthly_income(rent, loan, config)
    dscr = _dscr(income, debt_service)
    return RentalAnalysis(
        status=AnalysisStatus.EVALUATED,
        monthly_rent=rent,
        expenses=rent * rental.expenses_pct_of_rent,
        net_monthly_income=income,
        dscr=dscr,
        passed=dscr >= rental.dscr_floor,
        **common,
    )


def take_back_analysis(
    inputs: UnderwriteInputs, loan: LoanTerms, config: Config
) -> TakeBackAnalysis:
    """Whether the rent carries what the loan cost GLENWOOD, if it takes the property back.

    # SPEC §8.6
    """
    take_back = config.take_back
    lost_months = take_back.lost_interest_months
    lost_interest = loan.monthly_interest(loan.commitment) * Decimal(lost_months)
    total_cost = loan.commitment + lost_interest + take_back.legal_costs_usd
    debt_service = monthly_payment(total_cost, loan.interest_rate, take_back.amortization_years)
    common = {
        "loan_amount": loan.commitment,
        "interest_rate": loan.interest_rate,
        "lost_interest_months": lost_months,
        "lost_interest": lost_interest,
        "legal_costs": take_back.legal_costs_usd,
        "total_cost": total_cost,
        "amortization_years": take_back.amortization_years,
        "debt_service_monthly": debt_service,
        "dscr_floor": take_back.dscr_floor,
    }
    rent = inputs.monthly_rent
    if rent is None:
        return TakeBackAnalysis(
            status=AnalysisStatus.NOT_EVALUATED,
            net_monthly_income=None,
            dscr=None,
            passed=None,
            **common,
        )
    income = net_monthly_income(rent, loan, config)
    dscr = _dscr(income, debt_service)
    return TakeBackAnalysis(
        status=AnalysisStatus.EVALUATED,
        net_monthly_income=income,
        dscr=dscr,
        passed=dscr >= take_back.dscr_floor,
        **common,
    )
