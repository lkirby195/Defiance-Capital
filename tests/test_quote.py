"""The borrower's quote: the cushioned rate, and the borrower's figures at it.  # SPEC §4.2

Independent of the engine wherever it can be. The cushion arithmetic is written out by hand
for a handful of rates. Wichita's figures at the cushioned rate come from the same
closed-form arithmetic ``tests/test_borrower_sensitivity.by_hand_with_cost`` holds the
Borrower Sensitivity to - a $150,000 note, all at close, interest for a year at the rate and
a 2% fee - so a row quoted at 17.00% where 16.34% was solved costs the borrower 0.66 points
more, $990 on the request, and nothing else in the stack moves. Denver's holdback checks the
cash flows' shape and that every column adds up to the Flip Analysis's own line.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from config.config import Config
from engine.calc.flip import flip_analysis
from engine.calc.pricing import RATE_CEILING, pricing_matrix
from engine.calc.quote import (
    borrower_cash_flows,
    borrower_quote,
    borrower_rate,
    quoted_rows,
    spend_schedule,
)
from engine.calc.sensitivity import Grid
from engine.screen import credit_check, experience_check
from schema.models import AnalysisStatus, BorrowerQuote, PricingMatrix, UnderwriteInputs
from tests.test_borrower_sensitivity import by_hand_with_cost, no_price, wichita_inputs
from tests.test_pricing import config_with
from tests.test_sensitivity import denver_inputs

CONFIG = Config.load()
D = Decimal
CLOSE = D("1e-15")


def quote_for(
    inputs: UnderwriteInputs, config: Config = CONFIG
) -> tuple[BorrowerQuote, PricingMatrix, Grid]:
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    grid = Grid(inputs, credit.tranche, experience.tier, config)
    matrix = pricing_matrix(inputs, credit.tranche, experience.tier, config, grid)
    return (
        borrower_quote(inputs, matrix, credit.tranche, experience.tier, config, grid),
        matrix,
        grid,
    )


# --- the cushioned rate, by hand ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("solved", "cushion", "step", "expected"),
    [
        ("0.1634", "0.005", "0.0025", "0.1700"),  # 16.84% up to the quarter point
        ("0.1382", "0.005", "0.0025", "0.1450"),  # 14.32% -> 14.50%
        ("0.1372", "0.005", "0.0025", "0.1425"),  # 14.22% -> 14.25%
        ("0.1363", "0.005", "0.0025", "0.1425"),  # 14.13% -> 14.25%
        ("0.1200", "0.005", "0.0025", "0.1250"),  # already on a step: stays there
        ("0.1634", "0", "0.0001", "0.1634"),  # no cushion, the finest step: the solve itself
        ("0.1634", "0", "0.01", "0.17"),  # the rounding alone still rounds up
        ("0.1634", "0.005", "1", "1"),  # a whole-point step: up to 100%
    ],
)
def test_the_cushioned_rate_by_hand(solved: str, cushion: str, step: str, expected: str) -> None:
    config = config_with(borrower_rate_cushion=cushion, borrower_rate_rounding=step)
    assert borrower_rate(D(solved), config) == D(expected)


def test_the_cushioned_rate_never_passes_the_ceiling() -> None:
    config = config_with(borrower_rate_cushion="0.005", borrower_rate_rounding="0.0025")
    assert borrower_rate(D("0.999"), config) == RATE_CEILING
    assert borrower_rate(RATE_CEILING, config) == RATE_CEILING


def test_the_placeholders_load_and_the_loader_refuses_nonsense() -> None:
    assert CONFIG.pricing.borrower_rate_cushion == D("0.005")
    assert CONFIG.pricing.borrower_rate_rounding == D("0.0025")
    for bad in ({"borrower_rate_rounding": "0"}, {"borrower_rate_rounding": "1.5"}):
        with pytest.raises(Exception, match="borrower_rate_rounding"):
            config_with(**bad)
    with pytest.raises(Exception, match="borrower_rate_cushion"):
        config_with(borrower_rate_cushion="-0.01")


# --- Wichita: every row at 17.00%, the figures by the closed-form arithmetic ----------------------


def test_wichita_rows_are_quoted_at_the_cushioned_rate_with_the_figures_there() -> None:
    quote, matrix, _ = quote_for(wichita_inputs())
    assert quote.rate_cushion == D("0.005") and quote.rate_rounding == D("0.0025")
    assert quote.borrower_status is AnalysisStatus.EVALUATED
    # the request first, then each smaller loan largest to smallest (SPEC §4.2)
    assert [row.loan_amount for row in quote.rows] == [D(150000), D(143000), D(135000)]
    assert [row.label for row in quote.rows] == ["Requested", "-5%", "-10%"]
    assert quote.rows[0].is_request and not quote.rows[1].is_request
    for row in quote.rows:
        assert row.solved_rate == D("0.1634") and row.borrower_rate == D("0.17")
        profit, total, equity = by_hand_with_cost(row.loan_amount, D("0.17"))
        assert row.profit is not None and abs(row.profit - profit) < CLOSE
        assert abs(row.cash_invested - equity) < CLOSE
        assert row.project_yield is not None and abs(row.project_yield - profit / total) < CLOSE
        assert row.cash_on_cash is not None and abs(row.cash_on_cash - profit / equity) < CLOSE
    # the cushion costs the borrower exactly the extra year's interest on the loan, and the
    # team's matrix is untouched
    for row, priced in zip(quote.rows, reversed(matrix.rows), strict=True):
        assert priced.loan_amount == row.loan_amount and priced.interest_rate == D("0.1634")
        assert priced.borrower_profit is not None and row.profit is not None
        extra = row.loan_amount * (D("0.17") - D("0.1634"))
        assert abs((priced.borrower_profit - row.profit) - extra) < CLOSE
    assert quote.rows[0].profit is not None
    assert abs((matrix.rows[-1].borrower_profit or 0) - quote.rows[0].profit - D(990)) < CLOSE


def test_no_sale_price_quotes_the_rate_and_nothing_the_price_feeds() -> None:
    quote, _, _ = quote_for(no_price())
    assert quote.borrower_status is AnalysisStatus.NOT_EVALUATED
    assert quote.rows and all(
        row.profit is None and row.project_yield is None and row.cash_on_cash is None
        for row in quote.rows
    )
    assert all(row.borrower_rate == D("0.17") and row.cash_invested > 0 for row in quote.rows)
    assert quote.cash_flows is not None and quote.sources_and_uses is not None
    assert all(row.sale is None for row in quote.cash_flows)
    # with nothing to sell, the net is the whole cost stack, every line of it a cost
    assert all(row.net < 0 for row in quote.cash_flows)
    assert all(
        row.net == row.purchase + row.rehab + row.holding + row.financing
        for row in quote.cash_flows
    )


def test_a_row_the_team_cannot_quote_is_not_quoted_to_the_borrower_either() -> None:
    config = config_with(max_quoted_rate="0.16")  # under Wichita's 16.34%
    quote, matrix, _ = quote_for(wichita_inputs(), config)
    assert all(row.over_max_quote for row in matrix.rows)
    assert quoted_rows(matrix) == [] and quote.rows == []
    assert quote.cash_flows is None and quote.sources_and_uses is None


# --- Denver: the cash flows' shape, and every column adding up ----------------------------------


def test_denver_cash_flows_add_up_to_the_flip_at_the_cushioned_rate() -> None:
    quote, _, grid = quote_for(denver_inputs())
    first = quote.rows[0]
    assert first.is_request and first.borrower_rate == D("0.1450")  # 13.82% + 0.5% -> 14.50%
    point = grid.point(D(0), first.borrower_rate)
    assert point is not None
    flip = flip_analysis(point.inputs, point.loan, point.overview, CONFIG)
    rows = quote.cash_flows
    assert rows is not None and rows == borrower_cash_flows(point, flip)
    assert [row.month for row in rows] == [entry.month for entry in point.overview.entries]
    assert [row.date for row in rows] == [entry.date for entry in point.overview.entries]

    def total(column: str) -> Decimal:
        return sum((getattr(row, column) for row in rows), D(0))

    assert total("purchase") == -(flip.purchase_price + flip.closing_costs)
    assert total("rehab") == -(flip.rehab_costs + flip.contingency)
    assert total("holding") == -flip.holding_costs_total
    assert flip.interest is not None and flip.origination is not None
    assert total("financing") == -(flip.interest + flip.origination)
    assert total("financing") == -(point.overview.total_interest + point.overview.total_fees)
    sold = [row for row in rows if row.sale is not None]
    assert len(sold) == 1 and sold[0] is rows[-1]
    assert flip.estimated_sale_price is not None and flip.broker_costs is not None
    assert sold[0].sale == flip.estimated_sale_price - flip.broker_costs
    assert flip.net_profit is not None and flip.net_profit == first.profit
    assert abs(total("net") - flip.net_profit) < CLOSE  # the same additions, grouped differently
    # the shape: the purchase at closing and nothing of the carry; the rehab over the rehab
    # months only; the carry every period after; each row's net is its own sum
    assert rows[0].purchase < 0 and rows[0].holding == 0 and rows[0].rehab == 0
    assert all(row.purchase == 0 for row in rows[1:])
    months = point.loan.rehab_months
    assert months > 0
    assert [row.month for row in rows if row.rehab != 0] == list(range(1, months + 1))
    assert all(row.holding < 0 for row in rows[1:])
    for row in rows:
        assert row.net == row.purchase + row.rehab + row.holding + row.financing + (row.sale or 0)
    # Sources & Uses at the same rate: balanced, its interest the cushioned ledger's, its
    # equity the row's cash invested
    su = quote.sources_and_uses
    assert su is not None
    assert su.total_sources == su.total_uses
    assert su.interest == point.overview.total_interest
    assert su.borrower_equity == first.cash_invested == flip.borrower_equity
    assert su.advance_at_closing + su.rehab_holdback == first.loan_amount


def test_the_spend_schedule_by_hand() -> None:
    assert spend_schedule(D(1000), 3) == {1: D("333.33"), 2: D("333.33"), 3: D("333.34")}
    assert spend_schedule(D(500), 0) == {0: D(500)}
    assert spend_schedule(D(0), 3) == {}
    assert sum(spend_schedule(D("44000"), 9).values(), D(0)) == D(44000)
