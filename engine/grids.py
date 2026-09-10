"""Sensitivity grid: lender_yield(m, r) over rates x payoff months.  # SPEC §8.5

One grid. Columns are the configured rate grid (10.0% -> 15.0% in 50 bps) with r* inserted
in rate order when it is not already a column; rows run from the term through
term + ``returns.month_window.after_term``. A cell meets the target when its yield is at or
above ``returns.target_irr``. There is no borrower grid: borrower economics are reported at
(term, r*) only (SPEC §8.6).
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.lender import lender_return
from engine.calc.outstanding import LoanTerms
from schema.models import GridCell, GridRow, YieldGrid


def rate_axis(solved_rate: Decimal, config: Config) -> tuple[list[Decimal], bool]:
    """Configured rate columns, ascending, with r* inserted if absent; (rates, inserted)."""
    grid = config.returns.rate_grid
    steps = int((grid.max - grid.min) / grid.step)  # exact: the loader checks divisibility
    rates = [grid.min + grid.step * k for k in range(steps + 1)]
    inserted = solved_rate not in rates
    if inserted:
        rates = sorted([*rates, solved_rate])
    return rates, inserted


def month_axis(term_months: int, config: Config) -> list[int]:
    """Rows term .. term + after_term; no rows before the term (no early payoff).  # SPEC §8.5"""
    return list(range(term_months, term_months + config.returns.month_window.after_term + 1))


def grid_cell(
    loan: LoanTerms, month: int, rate: Decimal, solved_rate: Decimal, config: Config
) -> GridCell:
    """One cell: lender_yield(month, rate), flagged at or above the target.  # SPEC §8.5"""
    annualized = lender_return(loan, month, rate, config).annualized_yield
    return GridCell(
        month=month,
        rate=rate,
        annualized_yield=annualized,
        meets_target=annualized >= config.returns.target_irr,
        is_solved_rate=rate == solved_rate,
    )


def yield_grid(loan: LoanTerms, solved_rate: Decimal, config: Config) -> YieldGrid:
    """lender_yield(m, r) for every (month, rate); cells >= target flagged.  # SPEC §8.5"""
    target = config.returns.target_irr
    rates, inserted = rate_axis(solved_rate, config)
    months = month_axis(loan.term_months, config)
    rows = [
        GridRow(
            month=month,
            cells=[grid_cell(loan, month, rate, solved_rate, config) for rate in rates],
        )
        for month in months
    ]
    return YieldGrid(
        target=target,
        solved_rate=solved_rate,
        solved_rate_inserted=inserted,
        rates=rates,
        months=months,
        rows=rows,
    )
