"""DSCR takeout, run on every deal regardless of stated exit.  # SPEC §8.6

    gross_rent   = market_rent x 12
    opex         = gross_rent x (vacancy + management + maintenance)
                   + annual_taxes + annual_insurance
    noi          = gross_rent - opex
    dscr_loan    = loan whose annual debt service at takeout.rate (takeout.amortization_years,
                   level payment) equals noi / takeout.dscr_floor
    max_takeout  = min(arv x takeout.ltv, dscr_loan)
    payoff_due   = commitment + payoff fees
    refi_covers  = max_takeout >= payoff_due
    shortfall    = max(0, payoff_due - max_takeout)

The percentage opex lines apply to gross rent. Taxes and insurance are team actuals or the
config defaults as a percentage of the ARV (``engine.calc.borrower``).
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.borrower import resolve_annual_insurance, resolve_annual_taxes
from engine.calc.lender import TWELVE, payoff_fees
from engine.calc.outstanding import LoanTerms
from schema.models import ExitResult, UnderwriteInputs

ZERO = Decimal(0)
ONE = Decimal(1)


def _periodic(annual_rate: Decimal, years: int) -> tuple[Decimal, int]:
    if years < 1:
        raise ValueError(f"amortization years must be at least 1, got {years}")
    return annual_rate / TWELVE, years * 12


def monthly_payment(principal: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    """Level payment on a fully amortizing loan: P x i / (1 - (1 + i)^-n), i = rate / 12."""
    i, n = _periodic(annual_rate, years)
    if i == ZERO:
        return principal / Decimal(n)
    return principal * i / (ONE - (ONE + i) ** -n)


def loan_for_payment(payment: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    """Principal a level monthly ``payment`` amortizes: payment x (1 - (1 + i)^-n) / i."""
    i, n = _periodic(annual_rate, years)
    if i == ZERO:
        return payment * Decimal(n)
    return payment * (ONE - (ONE + i) ** -n) / i


def annual_opex(
    gross_rent: Decimal, annual_taxes: Decimal, annual_insurance: Decimal, config: Config
) -> Decimal:
    """gross_rent x (vacancy + management + maintenance) + taxes + insurance.  # SPEC §8.6"""
    defaults = config.takeout.opex_defaults
    rent_pct = (
        defaults.vacancy_pct_of_rent
        + defaults.management_pct_of_rent
        + defaults.maintenance_pct_of_rent
    )
    return gross_rent * rent_pct + annual_taxes + annual_insurance


def dscr_loan(noi_annual: Decimal, config: Config) -> Decimal:
    """Loan whose annual debt service at the takeout rate is noi / dscr_floor; 0 when noi <= 0."""
    if noi_annual <= ZERO:
        return ZERO
    payment = noi_annual / config.takeout.dscr_floor / TWELVE
    return loan_for_payment(payment, config.takeout.rate, config.takeout.amortization_years)


def payoff_due(loan: LoanTerms, config: Config) -> Decimal:
    """commitment + payoff fees (the origination portion due at payoff).  # SPEC §8.6"""
    return loan.commitment + payoff_fees(loan, config)


def dscr_at(loan_amount: Decimal, noi_annual: Decimal, config: Config) -> Decimal | None:
    """noi / annual debt service on ``loan_amount``; None when the loan amount is zero."""
    if loan_amount <= ZERO:
        return None
    service = (
        monthly_payment(loan_amount, config.takeout.rate, config.takeout.amortization_years)
        * TWELVE
    )
    return noi_annual / service


def dscr_takeout(inputs: UnderwriteInputs, loan: LoanTerms, config: Config) -> ExitResult:
    """Max takeout loan, whether it covers the payoff, and the shortfall.  # SPEC §8.6"""
    gross_rent = inputs.market_rent_monthly * TWELVE
    taxes, taxes_source = resolve_annual_taxes(inputs, config)
    insurance, insurance_source = resolve_annual_insurance(inputs, config)
    opex = annual_opex(gross_rent, taxes, insurance, config)
    noi = gross_rent - opex
    ltv_takeout = inputs.arv * config.takeout.ltv
    by_dscr = dscr_loan(noi, config)
    max_takeout = min(ltv_takeout, by_dscr)
    due = payoff_due(loan, config)
    return ExitResult(
        type=inputs.stated_exit,
        gross_rent_annual=gross_rent,
        annual_taxes=taxes,
        annual_taxes_source=taxes_source,
        annual_insurance=insurance,
        annual_insurance_source=insurance_source,
        opex_annual=opex,
        noi_annual=noi,
        ltv_takeout=ltv_takeout,
        dscr_takeout=by_dscr,
        max_takeout=max_takeout,
        payoff_due=due,
        dscr_at_payoff=dscr_at(due, noi, config),
        refi_covers=max_takeout >= due,
        shortfall=max(ZERO, due - max_takeout),
    )
