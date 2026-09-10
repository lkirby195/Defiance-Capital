"""Lender return: interest received, fees, annualized yield on the full commitment.  # SPEC §8.4

Interest received is what the borrower pays, on the average outstanding balance (SPEC §8.3).
Fees are on the total commitment: origination split between close and payoff, plus the
extension fee when the payoff month is past the term. There is no rate step-up in extension
and no early payoff (the minimum-interest term is never exercised).

    interest(m, r)     = avg_outstanding(m) x r x m / 12 = dollar_months(m) x r / 12
    fees(m)            = commitment x origination_pct + commitment x extension_fee_pct x [m > term]
    lender_yield(m, r) = (interest(m, r) + fees(m)) / commitment x 12 / m
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.outstanding import LoanTerms, average_outstanding, dollar_months
from schema.models import FeeSchedule, LenderReturn

ZERO = Decimal(0)
TWELVE = Decimal(12)


def payoff_fees(loan: LoanTerms, config: Config) -> Decimal:
    """The origination portion due at payoff: commitment x origination_at_payoff_pct.  # SPEC §3

    These are the "payoff fees" in payoff_due and the "unpaid fees" in the REO exposure
    (SPEC §8.6).
    """
    return loan.commitment * config.fees.origination_at_payoff_pct


def fee_schedule(loan: LoanTerms, month: int, config: Config) -> FeeSchedule:
    """Origination at close and at payoff on the total commitment, plus the extension fee
    (``loan.extension_fee_pct`` of commitment) when ``month`` is past the term.  # SPEC §3, §8.4
    """
    at_close = loan.commitment * config.fees.origination_at_close_pct
    at_payoff = payoff_fees(loan, config)
    extension = loan.commitment * loan.extension_fee_pct if month > loan.term_months else ZERO
    return FeeSchedule(
        origination_at_close=at_close,
        origination_at_payoff=at_payoff,
        extension=extension,
        total=at_close + at_payoff + extension,
    )


def interest(loan: LoanTerms, month: int, rate: Decimal, config: Config) -> Decimal:
    """interest(m, r) = avg_outstanding(m) x r x m / 12 = dollar_months(m) x r / 12.  # SPEC §8.4"""
    return dollar_months(loan, month, config) * rate / TWELVE


def annualized_yield(
    interest_amount: Decimal, fees_total: Decimal, commitment: Decimal, month: int
) -> Decimal:
    """lender_yield(m, r) = (interest + fees) / commitment x 12 / m.  # SPEC §8.4

    The denominator is the full commitment, not the average outstanding balance.
    """
    if commitment <= ZERO:
        raise ValueError(f"commitment must be positive to compute a yield, got {commitment}")
    if month < 1:
        raise ValueError(f"payoff month must be at least 1, got {month}")
    return (interest_amount + fees_total) / commitment * TWELVE / Decimal(month)


def lender_return(loan: LoanTerms, month: int, rate: Decimal, config: Config) -> LenderReturn:
    """Interest, fees, average outstanding, and annualized yield at (month, rate).  # SPEC §8.4"""
    fees = fee_schedule(loan, month, config)
    interest_amount = interest(loan, month, rate, config)
    return LenderReturn(
        month=month,
        rate=rate,
        avg_outstanding=average_outstanding(loan, month, config),
        interest=interest_amount,
        fees=fees,
        annualized_yield=annualized_yield(interest_amount, fees.total, loan.commitment, month),
    )
