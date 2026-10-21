"""The pricing matrix: the lowest rate that earns the lender its target, solved.  # SPEC §8.10

One row per ``pricing.loan_amount_steps`` entry, each a signed change to the request (0 is
the request, -10,000 is ten thousand less). For each:

    interest_rate  = the lowest rate, in steps of RATE_PRECISION (0.01%), at which the
                     lender's XIRR >= pricing.target_lender_irr
    lender_irr     = the XIRR at that rate
    borrower_*     = the Borrower Sensitivity's profit, cash and cash-on-cash at that amount
                     and that rate (``engine/calc/borrower.py``)

**The rate is solved, not looked up.** Every candidate is a full ledger re-run at that loan
amount and that rate, exactly as a Lender Sensitivity cell is, and the search bisects on
the whole number of 0.01% steps: the lowest step whose ledger clears the target is the
answer, so the rate a row names is the exact minimum to two decimals of a percent - rounded
up, never down, because a rate rounded down to the nearest 0.01% would no longer clear the
target it was solved for - and every figure beside it is that rate's own ledger.

The search is a bisection because the lender's IRR only rises with the rate: the advance,
the draws, the fee and the payoff do not depend on it, and every month's interest grows with
it. The Lender Sensitivity's columns are already laid out for the same rows, so they set the
starting bracket - the highest column that falls short below, the lowest that clears above -
and the bisection only re-runs the ledgers between them (``engine/calc/sensitivity.Grid``
keeps every one it lays out). A row the columns never clear is searched up to
``RATE_CEILING``, the highest rate the model accepts; one that does not clear even there says
so, and so does a step that leaves no loan to price. The reduction comes off the advance at
closing exactly as a Lender Sensitivity row's does.

**A rate above ``pricing.max_quoted_rate`` is not a quote.** The solve still finds it, and
the row says so in its place - no rate, no IRR, no borrower figures, ``rate_needed`` set and
the sentence "exceeds max quoted rate (20.15% needed)" - so a reader sees how far the loan is
from quotable rather than a number nobody would offer. A rate equal to the maximum is quoted.

Each row carries one plain-English sentence, written here so that the deal page, the
workbook and the CLI cannot word the same row three ways:

    A $140,000 loan at 14.37% returns $21,400 profit (28.2% cash-on-cash).
    A $150,000 loan exceeds max quoted rate (20.15% needed).

Pure: ``UnderwriteInputs``, the caps cell, the flip toggle and a ``Config`` in, a
``PricingMatrix`` out.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from config.config import Config
from engine.calc.borrower import BorrowerReturns, borrower_returns, borrower_status
from engine.calc.sensitivity import Grid, Point, rate_columns
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

# The solve's resolution: a hundredth of a percentage point, so a solved rate has two
# decimals as a percent (SPEC §8.10). Not a lender's tunable - it is how finely the answer is
# written down, the way a rate is quoted.
RATE_PRECISION = Decimal("0.0001")
# The highest rate the search tries: the most ``UnderwriteInputs.interest_rate`` accepts
# (``le=1``). A bound on the arithmetic rather than a cap a lender sets - a row that needs
# more than this to reach the target is reported as one no rate prices.
RATE_CEILING = Decimal(1)


def dollars(amount: Decimal) -> str:
    """Whole dollars, the way the deal page shows money: ``$140,000``, ``-$5,000``."""
    whole = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    sign = "-" if whole < ZERO else ""
    return f"{sign}${abs(whole):,.0f}"


def rate_text(rate: Decimal) -> str:
    """A rate as few digits as it needs: ``12%``, ``14.5%``, ``14.37%``."""
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
    ceiling: Decimal = RATE_CEILING,
    priced: bool = True,
) -> str:
    """The row in plain English.  # SPEC §8.10"""
    loan = dollars(loan_amount)
    if not priced:
        return f"A {loan} loan cannot be priced: it leaves no commitment to lend."
    if rate is None:
        return (
            f"No rate up to {rate_text(ceiling)} gets a {loan} loan to the "
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


def over_max_sentence(loan_amount: Decimal, rate: Decimal) -> str:
    """The row whose solved rate is above ``pricing.max_quoted_rate``.  # SPEC §8.10"""
    return f"A {dollars(loan_amount)} loan exceeds max quoted rate ({rate_text(rate)} needed)."


def unpriced_row(
    step: Decimal, amount: Decimal, sentence: str, rate_needed: Decimal | None = None
) -> PricingRow:
    """A row no rate is quoted on: every figure None, the sentence saying why.

    ``rate_needed`` is the solved rate when the reason is that it is above the maximum quote.
    """
    return PricingRow(
        step=step,
        loan_amount=amount,
        interest_rate=None,
        lender_irr=None,
        borrower_profit=None,
        borrower_cash_invested=None,
        borrower_cash_on_cash=None,
        sentence=sentence,
        rate_needed=rate_needed,
    )


def clears(point: Point, target: Decimal) -> bool:
    """Whether one ledger's XIRR reaches the target; a ledger with no XIRR does not."""
    irr = point.overview.irr
    return irr is not None and irr >= target


def solve_rate(
    grid: Grid,
    reduction: Decimal,
    target: Decimal,
    seeds: list[Decimal],
    ceiling: Decimal = RATE_CEILING,
    precision: Decimal = RATE_PRECISION,
) -> tuple[Decimal, Point] | None:
    """The lowest multiple of ``precision`` whose ledger clears ``target``, and that ledger.

    # SPEC §8.10. Bisection on the whole number of steps ``k`` (the rate is ``k x
    precision``), holding the largest step known to fall short and the smallest known to
    clear until they are adjacent; the one that clears is the answer. ``seeds`` - the Lender
    Sensitivity's columns, already on the grid - narrow the bracket first; a seed that is not
    a whole number of steps is skipped, since the answer has to be one. -1 stands for "below
    every rate" until something falls short, so a deal whose fee alone clears the target is
    priced at 0%. None when not even ``ceiling`` clears it, or the row lends nothing.
    """

    def at(steps: int) -> Point | None:
        return grid.point(reduction, precision * steps)

    top = int(ceiling / precision)
    short, clear = -1, None
    for rate in sorted(seeds):
        if rate % precision or rate > ceiling:
            continue
        steps = int(rate / precision)
        point = at(steps)
        if point is None:
            return None
        if clears(point, target):
            clear = steps
            break
        short = steps
    if clear is None:
        point = at(top)
        if point is None or not clears(point, target):
            return None
        clear = top
    while clear - short > 1:
        middle = (short + clear) // 2
        point = at(middle)
        assert point is not None  # the row was sized, so every rate on it is priced
        if clears(point, target):
            clear = middle
        else:
            short = middle
    found = at(clear)
    assert found is not None
    return precision * clear, found


def pricing_matrix(
    inputs: UnderwriteInputs,
    tranche: Tranche,
    tier: ExperienceTier,
    flip_on: bool,
    config: Config,
    grid: Grid | None = None,
    ceiling: Decimal = RATE_CEILING,
) -> PricingMatrix:
    """One row per loan amount step, priced at the lowest rate that earns the target.

    ``ceiling`` is the highest rate the solve tries; it is ``RATE_CEILING`` on every
    underwrite and lower only where a test wants a row nothing prices.
    """
    grid = grid or Grid(inputs, tranche, tier, config)
    seeds = rate_columns(config, inputs.interest_rate)
    target = config.pricing.target_lender_irr
    status = borrower_status(inputs, flip_on)
    rows: list[PricingRow] = []
    for step in config.pricing.loan_amount_steps:
        amount = inputs.deal.loan_requested + step
        if grid.row(-step) is None:
            sentence = pricing_sentence(amount, None, None, target, priced=False)
            rows.append(unpriced_row(step, amount, sentence))
            continue
        solved = solve_rate(grid, -step, target, seeds, ceiling)
        if solved is None:
            sentence = pricing_sentence(amount, None, None, target, ceiling)
            rows.append(unpriced_row(step, amount, sentence))
            continue
        rate, point = solved
        if rate > config.pricing.max_quoted_rate:
            rows.append(unpriced_row(step, amount, over_max_sentence(amount, rate), rate))
            continue
        returns = borrower_returns(point, config) if status is AnalysisStatus.EVALUATED else None
        rows.append(
            PricingRow(
                step=step,
                loan_amount=amount,
                interest_rate=rate,
                lender_irr=point.overview.irr,
                borrower_profit=returns.net_profit if returns else None,
                borrower_cash_invested=returns.cash_invested if returns else None,
                borrower_cash_on_cash=returns.cash_on_cash if returns else None,
                sentence=pricing_sentence(amount, rate, returns, target),
            )
        )
    return PricingMatrix(
        target_lender_irr=target,
        borrower_status=status,
        rows=rows,
        rate_precision=RATE_PRECISION,
        rate_ceiling=ceiling,
        max_quoted_rate=config.pricing.max_quoted_rate,
    )
