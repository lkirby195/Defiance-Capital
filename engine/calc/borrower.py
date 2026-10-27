"""Borrower Sensitivity - the borrower's profit and project yield by loan amount and rate.

# SPEC §8.9

The Lender Sensitivity's rows and columns, seen from the other side of the loan. Every cell
re-sizes the deal and lays the whole ledger out again at that loan amount and that rate, the
same way the lender's cell does (``engine/calc/sensitivity.py``), and then runs the Flip
Analysis on it (``engine/calc/flip.py``):

    net_profit    = the flip's net profit at that amount and rate
    total_costs   = the flip's total deal cost there
    project_yield = net_profit / total_costs                  # what the cell shows
    cash_invested = total_costs - the loan amount              # the borrower's equity
    cash_on_cash  = net_profit / cash_invested          (None when cash_invested <= 0)

The loan amount is the commitment the ledger lent, which is the row's loan amount on every
deal whose split adds up to it. Only the financing costs move from cell to cell - lending
less or charging less costs the borrower less interest and a smaller fee - so the cell shows
the project yield, which reads the same project at every loan amount; the equity and the
cash-on-cash are recorded beside it (engine 2.0.0; the cell showed the cash-on-cash before).

The table is ``NOT_EVALUATED`` when there is no sale price: the profit is the flip's, and
there is none to report.

Pure: ``UnderwriteInputs``, the caps cell and a ``Config`` in, a
``BorrowerSensitivityTable`` out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from engine.calc.flip import flip_analysis
from engine.calc.sensitivity import Grid, Point, rate_columns, reductions
from schema.models import (
    AnalysisStatus,
    BorrowerSensitivityCell,
    BorrowerSensitivityRow,
    BorrowerSensitivityTable,
    ExperienceTier,
    Tranche,
    UnderwriteInputs,
)

ZERO = Decimal(0)


class BorrowerReturns(NamedTuple):
    """The borrower's profit, the deal's cost and the cash they put in, and the ratios."""

    net_profit: Decimal
    total_costs: Decimal
    project_yield: Decimal
    cash_invested: Decimal
    cash_on_cash: Decimal | None


def borrower_status(inputs: UnderwriteInputs) -> AnalysisStatus:
    """EVALUATED only when there is a sale price to sell at.  # SPEC §8.9"""
    if inputs.deal.estimated_sale_price is not None:
        return AnalysisStatus.EVALUATED
    return AnalysisStatus.NOT_EVALUATED


def borrower_equity(point: Point, config: Config) -> Decimal:
    """The total deal cost less the loan amount at one point; no sale price needed.

    # SPEC §8.4, §8.10
    """
    equity = flip_analysis(point.inputs, point.loan, point.overview, config).borrower_equity
    assert equity is not None  # every 2.0.0 flip carries it
    return equity


def borrower_returns(point: Point, config: Config) -> BorrowerReturns | None:
    """Profit, cost, equity and the two ratios at one point; None with no sale price.

    # SPEC §8.4, §8.9. cash_invested is the borrower's equity, total deal cost - the loan
    amount; cash_on_cash is None when that is zero or less, because the loan then covers
    every cost.
    """
    flip = flip_analysis(point.inputs, point.loan, point.overview, config)
    if flip.net_profit is None or flip.project_yield is None:
        return None
    assert flip.borrower_equity is not None  # every 2.0.0 flip carries it
    return BorrowerReturns(
        net_profit=flip.net_profit,
        total_costs=flip.total_costs,
        project_yield=flip.project_yield,
        cash_invested=flip.borrower_equity,
        cash_on_cash=flip.cash_on_cash,
    )


def borrower_sensitivity_table(
    inputs: UnderwriteInputs,
    tranche: Tranche,
    tier: ExperienceTier,
    config: Config,
    grid: Grid | None = None,
) -> BorrowerSensitivityTable:
    """The whole grid from the borrower's side, every cell a ledger re-run.  # SPEC §8.9

    The rows, the columns and the rows left out are exactly the Lender Sensitivity's, so the
    two tables line up cell for cell on the page and in the workbook.
    """
    grid = grid or Grid(inputs, tranche, tier, config)
    rates = rate_columns(config)
    status = borrower_status(inputs)
    rows: list[BorrowerSensitivityRow] = []
    if status is AnalysisStatus.EVALUATED:
        for reduction in reductions(config):
            row = grid.row(reduction)
            if row is None:
                continue
            deal, _ = row
            cells: list[BorrowerSensitivityCell] = []
            for rate in rates:
                point = grid.point(reduction, rate)
                returns = None if point is None else borrower_returns(point, config)
                if returns is None:  # pragma: no cover - the status check rules this out
                    raise AssertionError("an evaluated borrower table lost its sale price")
                cells.append(
                    BorrowerSensitivityCell(
                        interest_rate=rate,
                        net_profit=returns.net_profit,
                        cash_invested=returns.cash_invested,
                        cash_on_cash=returns.cash_on_cash,
                        is_deal=reduction == ZERO and rate == inputs.interest_rate,
                        total_costs=returns.total_costs,
                        project_yield=returns.project_yield,
                    )
                )
            rows.append(
                BorrowerSensitivityRow(
                    loan_amount=deal.loan_requested, reduction=reduction, cells=cells
                )
            )
    return BorrowerSensitivityTable(
        status=status,
        loan_requested=inputs.deal.loan_requested,
        interest_rate=inputs.interest_rate,
        rates=rates,
        rows=rows,
    )
