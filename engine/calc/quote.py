"""The borrower's quote: the cushioned rate, and the borrower's figures at it.  # SPEC §4.2

The pricing matrix solves each row's rate to the exact minimum that earns the lender its
target (``engine/calc/pricing.py``, SPEC §8.10). That is the team's number. What the
borrower is shown on the thank-you page is that number with a cushion on it:

    borrower_rate = min(solved_rate + pricing.borrower_rate_cushion
                            rounded UP to the next multiple of pricing.borrower_rate_rounding,
                        pricing.max_quoted_rate)

so a row solved at 16.34% under the placeholders (0.5% and 0.25%) is quoted at 17.00%: 16.84%
rounded up to the quarter point. Rounded up, never to the nearest, for the reason the solve
itself rounds up - a rate rounded down would cut into the cushion the lender set. A cushion
of zero and a step of 0.01% show the solve itself. The maximum quoted rate is the ceiling on
what is shown: a row is quoted only when its solve clears it (the matrix leaves the row's
rate blank otherwise, and ``quoted_rows`` leaves the row out), and the cushion never carries
the rate shown above it - a 17.9% solve under an 18% maximum is quoted at 18.00%, not 18.50%.

Every borrower figure beside the rate is **at the cushioned rate**: the ledger is laid out
again at the row's loan amount and the borrower's rate - exactly as a sensitivity cell is
(``engine/calc/sensitivity.Grid``) - and the Flip Analysis is run on it (``engine/calc/flip``),
so the profit, the project yield and the cash-on-cash the borrower reads are what the loan
costs them at the rate they are quoted, not at the lower one the lender solved for. Only the
financing moves between the two: a higher rate is more interest on the same commitment, and
nothing else in the cost stack reads the rate.

The quote also carries two tables for the first row shown - the request when it is quoted,
else the largest quoted loan - at its cushioned rate, for the thank-you page's Advanced view:

    cash flows      the borrower's own, by month: purchase (the price and the closing costs,
                    month 0), rehab (the budget and its contingency, straight-line over the
                    rehab period, or at month 0 when there is none), holding (the carry,
                    every period, the stub prorated), financing (that period's interest and
                    fees off the ledger), the sale (net of the broker, in the payoff period;
                    blank with no sale price), and the loan itself as the borrower sees it:
                    the advance at closing and each draw as money in, the payoff - the
                    principal going back - as money out, each read off the same ledger with
                    the lender's signs turned around. Costs and the payoff are negative, the
                    sale, the advance and the draws positive, and the net cash column is
                    what the borrower puts in or takes out that month; it adds up to the
                    flip's net profit to the cent, because every dollar advanced is paid off.
    sources & uses  ``engine/calc/sources_uses`` at the cushioned rate: the interest and the
                    borrower's equity move with the rate, the rest stands.

Pure: ``UnderwriteInputs``, the stored ``PricingMatrix``, the caps cell and a ``Config`` in,
a ``BorrowerQuote`` out. Nothing here is recorded on the underwrite row: the service reads a
version's inputs and matrix back and asks for the quote under the config in force, and the
thank-you page records what it showed (``services/indicative.py``).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from config.config import Config
from engine.calc.flip import flip_analysis
from engine.calc.sensitivity import Grid, Point
from engine.calc.sources_uses import sources_and_uses
from schema.models import (
    AnalysisStatus,
    BorrowerCashFlow,
    BorrowerQuote,
    ExperienceTier,
    FlipAnalysis,
    PricingMatrix,
    PricingRow,
    QuoteRow,
    Tranche,
    UnderwriteInputs,
)

ZERO = Decimal(0)
CENTS = Decimal("0.01")


def borrower_rate(solved: Decimal, config: Config) -> Decimal:
    """The solved rate plus the cushion, rounded up to the next rounding step, and never
    above ``pricing.max_quoted_rate``.  # SPEC §4.2

    ``(solved + cushion) / step`` is taken to the next whole number of steps - up, never to
    the nearest - and multiplied back; a rate already on a step stays on it. The maximum
    quoted rate caps the result: a solve the matrix quoted is at or under it, and what the
    cushion adds on top is cut back to it.
    """
    cushioned = solved + config.pricing.borrower_rate_cushion
    step = config.pricing.borrower_rate_rounding
    steps = (cushioned / step).quantize(Decimal(1), rounding=ROUND_CEILING)
    return min(steps * step, config.pricing.max_quoted_rate)


def quoted_rows(matrix: PricingMatrix) -> list[PricingRow]:
    """The rows the borrower is shown, in their order: the request first, then each smaller
    loan largest to smallest (SPEC §4.2); a row that quotes no rate is left out."""
    rows = [row for row in matrix.rows if row.cleared]
    rows.sort(key=lambda row: (not row.is_request, -row.loan_amount))
    return rows


def spend_schedule(total: Decimal, months: int) -> dict[int, Decimal]:
    """Month -> share of ``total`` spent in it: straight-line over ``months``, to the cent,
    the remainder in the last; all of it in month 0 when there is no period to spread over.
    The shape the ledger's draw schedule has (``engine/calc/ledger.draw_schedule``)."""
    if total == ZERO:
        return {}
    if months <= 0:
        return {0: total}
    each = (total / Decimal(months)).quantize(CENTS, rounding=ROUND_HALF_UP)
    schedule = {month: each for month in range(1, months + 1)}
    schedule[months] = total - each * Decimal(months - 1)
    return schedule


def borrower_cash_flows(point: Point, flip: FlipAnalysis) -> list[BorrowerCashFlow]:
    """The borrower's own cash flows by period, at ``point``'s loan amount and rate.

    # SPEC §4.2. Purchase and closing costs in month 0; the rehab and its contingency over
    the rehab period; the holding cost every period after closing, the stub's prorated and
    the last period taking the rounding remainder so the column adds to the total; the
    financing as each ledger row's interest and fees; the sale, net of the broker, in the
    payoff period; the loan's advance, draws and payoff as the ledger has them, signs
    turned to the borrower's. Costs and the payoff negative, the sale and the loan's money
    in positive; ``net`` is the month's cash in the borrower's hands.
    """
    loan = point.loan
    entries = point.overview.entries
    last = entries[-1].month
    rehab = spend_schedule(flip.rehab_costs + flip.contingency, loan.rehab_months)
    holding_each = loan.holding_costs_monthly
    sale = (
        flip.estimated_sale_price - flip.broker_costs
        if flip.estimated_sale_price is not None and flip.broker_costs is not None
        else None
    )
    rows: list[BorrowerCashFlow] = []
    holding_so_far = ZERO
    for entry in entries:
        purchase = -(flip.purchase_price + flip.closing_costs) if entry.month == 0 else ZERO
        if entry.month == 0:
            holding = ZERO
        elif entry.month == last:
            holding = -(flip.holding_costs_total - holding_so_far)
        elif entry.stub_days:
            holding = -(holding_each * Decimal(entry.stub_days) / Decimal(loan.day_count_basis))
        else:
            holding = -holding_each
        holding_so_far += -holding
        financing = -(entry.interest + entry.fees)
        spent = -rehab.get(entry.month, ZERO)
        sold = sale if entry.month == last and sale is not None else None
        advance, draws, payoff = -entry.funding, -entry.draws, -entry.payoff
        project = purchase + spent + holding + financing + (sold if sold is not None else ZERO)
        rows.append(
            BorrowerCashFlow(
                month=entry.month,
                date=entry.date,
                purchase=purchase,
                rehab=spent,
                holding=holding,
                financing=financing,
                sale=sold,
                advance=advance,
                draws=draws,
                payoff=payoff,
                net=project + advance + draws + payoff,
            )
        )
    return rows


def borrower_quote(
    inputs: UnderwriteInputs,
    matrix: PricingMatrix,
    tranche: Tranche,
    tier: ExperienceTier,
    config: Config,
    grid: Grid | None = None,
) -> BorrowerQuote:
    """Every quoted row at its cushioned rate, and the first row's two tables.  # SPEC §4.2

    ``matrix`` is the stored pricing matrix the rows and their solved rates are read off;
    the ledgers at the cushioned rates are laid out here, on the same grid the matrix was
    priced on, so a row's loan amount is sized exactly as it was then.
    """
    grid = grid or Grid(inputs, tranche, tier, config)
    with_profit = matrix.borrower_status is AnalysisStatus.EVALUATED
    rows: list[QuoteRow] = []
    first: tuple[Point, FlipAnalysis] | None = None
    for row in quoted_rows(matrix):
        assert row.interest_rate is not None  # ``cleared``
        rate = borrower_rate(row.interest_rate, config)
        point = grid.point(-row.step, rate)
        assert point is not None  # the row was priced, so its loan amount is sized
        flip = flip_analysis(point.inputs, point.loan, point.overview, config)
        assert flip.borrower_equity is not None  # every 2.0.0 flip carries it
        if first is None:
            first = (point, flip)
        rows.append(
            QuoteRow(
                pct_step=row.pct_step,
                step=row.step,
                loan_amount=row.loan_amount,
                solved_rate=row.interest_rate,
                borrower_rate=rate,
                profit=flip.net_profit if with_profit else None,
                project_yield=flip.project_yield if with_profit else None,
                cash_on_cash=flip.cash_on_cash if with_profit else None,
                cash_invested=flip.borrower_equity,
            )
        )
    return BorrowerQuote(
        rate_cushion=config.pricing.borrower_rate_cushion,
        rate_rounding=config.pricing.borrower_rate_rounding,
        borrower_status=matrix.borrower_status,
        rows=rows,
        cash_flows=borrower_cash_flows(*first) if first is not None else None,
        sources_and_uses=sources_and_uses(first[0].loan, first[1]) if first is not None else None,
    )
