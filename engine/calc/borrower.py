"""Borrower Sensitivity - the borrower's profit and cash-on-cash by loan amount and rate.

# SPEC §8.9

The Lender Sensitivity's rows and columns, seen from the other side of the loan. Every cell
re-sizes the deal and lays the whole ledger out again at that loan amount and that rate, the
same way the lender's cell does (``engine/calc/sensitivity.py``), and then runs the Flip
Analysis on it (``engine/calc/flip.py``):

    net_profit    = the flip's net profit at that amount and rate
    cash_invested = the flip's total project costs - the loan amount
    cash_on_cash  = net_profit / cash_invested          (None when cash_invested <= 0)

The loan amount is the commitment the ledger lent, which is the row's loan amount on every
deal whose split adds up to it. Only the financing costs move from cell to cell - lending
less or charging less costs the borrower less interest and a smaller fee - so a lower loan
amount raises the profit and the cash both, and the cash-on-cash says which wins.

The table is ``NOT_EVALUATED`` when the Flip Analysis is off or there is no sale price: the
profit is the flip's, and there is none to report.

Pure: ``UnderwriteInputs``, the caps cell, the flip toggle and a ``Config`` in, a
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
    """The borrower's profit, the cash they put in, and the one over the other."""

    net_profit: Decimal
    cash_invested: Decimal
    cash_on_cash: Decimal | None


def borrower_status(inputs: UnderwriteInputs, flip_on: bool) -> AnalysisStatus:
    """EVALUATED only when the flip is on and there is a sale price to sell at.  # SPEC §8.9"""
    if flip_on and inputs.deal.estimated_sale_price is not None:
        return AnalysisStatus.EVALUATED
    return AnalysisStatus.NOT_EVALUATED


def borrower_returns(point: Point, config: Config) -> BorrowerReturns | None:
    """Profit, cash and cash-on-cash at one point; None when there is no sale price.

    # SPEC §8.4, §8.9. cash_invested = total project costs - the loan amount; cash_on_cash is
    None when that is zero or less, because the loan then covers every cost.
    """
    flip = flip_analysis(point.inputs, point.loan, point.overview, True, config)
    if flip.net_profit is None:
        return None
    cash = flip.total_costs - point.loan.commitment
    return BorrowerReturns(
        net_profit=flip.net_profit,
        cash_invested=cash,
        cash_on_cash=flip.net_profit / cash if cash > ZERO else None,
    )


def borrower_sensitivity_table(
    inputs: UnderwriteInputs,
    tranche: Tranche,
    tier: ExperienceTier,
    flip_on: bool,
    config: Config,
    grid: Grid | None = None,
) -> BorrowerSensitivityTable:
    """The whole grid from the borrower's side, every cell a ledger re-run.  # SPEC §8.9

    The rows, the columns and the rows left out are exactly the Lender Sensitivity's, so the
    two tables line up cell for cell on the page and in the workbook.
    """
    grid = grid or Grid(inputs, tranche, tier, config)
    rates = rate_columns(config)
    status = borrower_status(inputs, flip_on)
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
