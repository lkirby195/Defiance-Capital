"""The pricing matrix: the lowest grid rate that earns the lender its target.  # SPEC §8.10

One row per ``pricing.loan_amount_steps`` entry, each a signed change to the request (0 is
the request, -10,000 is ten thousand less). For each:

    interest_rate  = the lowest rate on the Lender Sensitivity's columns at which the
                     lender's XIRR >= pricing.target_lender_irr
    lender_irr     = the XIRR at that rate
    borrower_*     = the Borrower Sensitivity's profit, cash and cash-on-cash at that amount
                     and that rate (``engine/calc/borrower.py``)

The columns are searched lowest first and the first that reaches the target wins; nothing is
interpolated between them, so the rate a row names is one a lender could quote and every
figure beside it is that rate's own ledger. A row no grid rate gets to the target says so,
and so does a step that leaves no loan to price. The reduction comes off the advance at
closing exactly as a Lender Sensitivity row's does.

Each row carries one plain-English sentence, written here so that the deal page, the
workbook and the CLI cannot word the same row three ways:

    A $140,000 loan at 14.5% returns $21,400 profit (28.2% cash-on-cash).

Pure: ``UnderwriteInputs``, the caps cell, the flip toggle and a ``Config`` in, a
``PricingMatrix`` out.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from config.config import Config
from engine.calc.borrower import BorrowerReturns, borrower_returns, borrower_status
from engine.calc.sensitivity import Grid, rate_columns
from schema.models import (
    AnalysisStatus,
    ExperienceTier,
    PricingMatrix,
    PricingRow,
    Tranche,
    UnderwriteInputs,
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def dollars(amount: Decimal) -> str:
    """Whole dollars, the way the deal page shows money: ``$140,000``, ``-$5,000``."""
    whole = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    sign = "-" if whole < ZERO else ""
    return f"{sign}${abs(whole):,.0f}"


def rate_text(rate: Decimal) -> str:
    """A rate as few digits as it needs: ``12%``, ``14.5%``, ``12.75%``."""
    text = f"{rate * HUNDRED:.3f}".rstrip("0").rstrip(".")
    return f"{text}%"


def ratio_text(value: Decimal) -> str:
    """A ratio to one decimal place of a percent: ``28.2%``."""
    return f"{value:.1%}"


def pricing_sentence(
    loan_amount: Decimal,
    rate: Decimal | None,
    returns: BorrowerReturns | None,
    target: Decimal,
    rates: list[Decimal],
    priced: bool = True,
) -> str:
    """The row in plain English.  # SPEC §8.10"""
    loan = dollars(loan_amount)
    if not priced:
        return f"A {loan} loan cannot be priced: it leaves no commitment to lend."
    if rate is None:
        span = f"{rate_text(rates[0])} to {rate_text(rates[-1])}"
        return (
            f"No rate on the grid ({span}) gets a {loan} loan to the "
            f"{rate_text(target)} lender IRR target."
        )
    if returns is None:
        return (
            f"A {loan} loan at {rate_text(rate)} reaches the {rate_text(target)} lender IRR "
            "target; the borrower's profit is not evaluated (no sale price, or the Flip "
            "Analysis is off)."
        )
    if returns.cash_on_cash is None:
        return (
            f"A {loan} loan at {rate_text(rate)} returns {dollars(returns.net_profit)} profit "
            "(no cash-on-cash: the loan covers every cost)."
        )
    return (
        f"A {loan} loan at {rate_text(rate)} returns {dollars(returns.net_profit)} profit "
        f"({ratio_text(returns.cash_on_cash)} cash-on-cash)."
    )


def unpriced_row(step: Decimal, amount: Decimal, sentence: str) -> PricingRow:
    """A row no grid rate prices: every figure None, the sentence saying why."""
    return PricingRow(
        step=step,
        loan_amount=amount,
        interest_rate=None,
        lender_irr=None,
        borrower_profit=None,
        borrower_cash_invested=None,
        borrower_cash_on_cash=None,
        sentence=sentence,
    )


def pricing_matrix(
    inputs: UnderwriteInputs,
    tranche: Tranche,
    tier: ExperienceTier,
    flip_on: bool,
    config: Config,
    grid: Grid | None = None,
) -> PricingMatrix:
    """One row per loan amount step, priced at the lowest rate that earns the target."""
    grid = grid or Grid(inputs, tranche, tier, config)
    rates = rate_columns(config, inputs.interest_rate)
    target = config.pricing.target_lender_irr
    status = borrower_status(inputs, flip_on)
    rows: list[PricingRow] = []
    for step in config.pricing.loan_amount_steps:
        amount = inputs.deal.loan_requested + step
        if grid.row(-step) is None:
            sentence = pricing_sentence(amount, None, None, target, rates, priced=False)
            rows.append(unpriced_row(step, amount, sentence))
            continue
        for rate in rates:
            point = grid.point(-step, rate)
            assert point is not None  # the row was sized, so every rate on it is priced
            irr = point.overview.irr
            if irr is None or irr < target:
                continue
            returns = (
                borrower_returns(point, config) if status is AnalysisStatus.EVALUATED else None
            )
            rows.append(
                PricingRow(
                    step=step,
                    loan_amount=amount,
                    interest_rate=rate,
                    lender_irr=irr,
                    borrower_profit=returns.net_profit if returns else None,
                    borrower_cash_invested=returns.cash_invested if returns else None,
                    borrower_cash_on_cash=returns.cash_on_cash if returns else None,
                    sentence=pricing_sentence(amount, rate, returns, target, rates),
                )
            )
            break
        else:
            sentence = pricing_sentence(amount, None, None, target, rates)
            rows.append(unpriced_row(step, amount, sentence))
    return PricingMatrix(target_lender_irr=target, rates=rates, borrower_status=status, rows=rows)
