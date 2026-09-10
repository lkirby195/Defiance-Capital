"""Closed-form rate solve: lender_yield(term, r*) = target_irr.  # SPEC §8.4

lender_yield(term, r) is linear in r (SPEC §8.4):

    (dollar_months(term) x r / 12 + fees(term)) / commitment x 12 / term = target_irr
    r* = (target_irr x commitment x term / 12 - fees(term)) / (dollar_months(term) / 12)

No search, no tolerance: the result is exact to Decimal context precision, and
``lender_yield(term, r*)`` reproduces the target to the cent.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.lender import TWELVE, fee_schedule
from engine.calc.outstanding import LoanTerms, dollar_months


def target_income(loan: LoanTerms, config: Config) -> Decimal:
    """Interest + fees at term that annualize to the target: target x commitment x term / 12."""
    return config.returns.target_irr * loan.commitment * Decimal(loan.term_months) / TWELVE


def required_interest(loan: LoanTerms, config: Config) -> Decimal:
    """Interest at term that, with fees(term), hits the target income.  # SPEC §8.4"""
    return target_income(loan, config) - fee_schedule(loan, loan.term_months, config).total


def solve_rate(loan: LoanTerms, config: Config) -> Decimal:
    """r* such that lender_yield(term, r*) = returns.target_irr.  # SPEC §8.4

    r* = required_interest / (dollar_months(term) / 12). Negative when fees alone exceed the
    target income (very short terms); reported as computed, the grid shows it in place.
    """
    funded = dollar_months(loan, loan.term_months, config)
    if funded <= 0:
        raise ValueError("nothing is funded through the term; no rate can be solved")
    return required_interest(loan, config) / (funded / TWELVE)
