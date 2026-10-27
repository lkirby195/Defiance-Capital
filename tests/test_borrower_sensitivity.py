"""The Borrower Sensitivity: profit and project yield by loan amount and rate.  # SPEC §8.9

The weight is on one fixture checked against arithmetic written out here with no code
shared with ``engine/calc``. Wichita is 100% at close on a 12-month term, so the borrower's
side is closed-form: twelve months of interest on the full commitment plus the 2% fee is the
whole financing cost, and the rest of the flip's cost stack does not move with the loan.

    financing     = L x r + L x 0.02                    (12 months: L x r / 12 x 12)
    total_costs   = 200,000 + 1,000 + 4,000 + financing   (price, closing, holding; no rehab)
    net_profit    = 260,000 - 260,000 x 0.06 - total_costs
    project_yield = net_profit / total_costs            (what the cell shows, engine 2.0.0)
    cash_invested = total_costs - L                     (the borrower's equity)
    cash_on_cash  = net_profit / cash_invested

The recorded values below were computed first by a standalone Decimal script on the same
formulas and agree with the engine to the last digit; the in-test recomputation keeps them
honest if the fixture moves. Engine 2.0.0 moved the broker's default from 4% to 6%, which
takes 5,200 off every cell's profit (2% of the 260,000 sale) and leaves the costs and the
equity where they were: at 12.5% the bottom cell is 17,650 on 226,750 of cost (7.7839%) and
76,750 of equity, the top 20,550 on 223,850 (9.1803%) and 93,850.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from config.config import Config
from engine.calc.borrower import borrower_sensitivity_table, borrower_status
from engine.calc.sensitivity import Grid, sensitivity_table
from engine.screen import credit_check, experience_check
from engine.underwrite import underwrite
from schema.models import AnalysisStatus, BorrowerSensitivityTable, UnderwriteInputs

CONFIG = Config.load()
D = Decimal
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
WICHITA = FIXTURE_DIR / "go_acquisition_wichita.json"


def wichita_inputs(**changes: Any) -> UnderwriteInputs:
    block = json.loads(WICHITA.read_text(encoding="utf-8"))["underwrite"]["inputs"]
    return UnderwriteInputs.model_validate({**block, **changes})


def table_for(inputs: UnderwriteInputs) -> BorrowerSensitivityTable:
    credit = credit_check(inputs.borrower, CONFIG)
    experience = experience_check(inputs.borrower, CONFIG)
    return borrower_sensitivity_table(inputs, credit.tranche, experience.tier, CONFIG)


def by_hand(loan: Decimal, rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Wichita's borrower side at one loan amount and rate: profit, equity, cash-on-cash."""
    profit, total, cash = by_hand_with_cost(loan, rate)
    return profit, cash, profit / cash


def by_hand_with_cost(loan: Decimal, rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Profit, total deal cost and equity at one loan amount and rate, as above."""
    financing = loan * rate + loan * D("0.02")
    total = D(200000) + D(1000) + D(4000) + financing
    profit = D(260000) - D(260000) * D("0.06") - total
    return profit, total, total - loan


def test_every_cell_agrees_with_the_arithmetic_written_out_by_hand() -> None:
    table = table_for(wichita_inputs())
    assert table.status is AnalysisStatus.EVALUATED
    assert [row.loan_amount for row in table.rows] == [
        D(130000),
        D(135000),
        D(140000),
        D(145000),
        D(150000),
    ]
    # the config grid alone: Wichita's own 12% is not a column (engine 1.11.0)
    assert table.rates == [D(x) for x in ("0.125", "0.135", "0.145", "0.155", "0.165", "0.175")]
    for row in table.rows:
        for cell in row.cells:
            profit, total, cash = by_hand_with_cost(row.loan_amount, cell.interest_rate)
            assert cell.net_profit == profit
            assert cell.total_costs == total
            assert cell.project_yield is not None
            assert abs(cell.project_yield - profit / total) < D("1e-20")
            assert cell.cash_invested == cash
            assert cell.cash_on_cash is not None
            assert abs(cell.cash_on_cash - profit / cash) < D("1e-20")


def test_the_recorded_values_from_the_independent_script() -> None:
    table = table_for(wichita_inputs())
    bottom, top = table.rows[-1], table.rows[0]
    # Wichita is priced at 12%, off the grid: no cell is the deal's
    assert table.deal_cell is None
    first = bottom.cells[0]
    assert (first.net_profit, first.total_costs, first.cash_invested) == (
        D(17650),
        D(226750),
        D(76750),
    )
    assert round(first.project_yield or 0, 6) == D("0.077839")
    assert round(first.cash_on_cash or 0, 6) == D("0.229967")
    last = bottom.cells[-1]
    assert (last.net_profit, last.total_costs, last.cash_invested) == (
        D(10150),
        D(234250),
        D(84250),
    )
    assert round(last.project_yield or 0, 6) == D("0.043330")
    assert (top.cells[0].net_profit, top.cells[0].total_costs) == (D(20550), D(223850))
    assert round(top.cells[0].project_yield or 0, 6) == D("0.091803")
    assert round(top.cells[-1].project_yield or 0, 6) == D("0.060994")
    # at 12.5%, on the grid, the deal as it stands is that first bottom cell
    on_grid = table_for(wichita_inputs(interest_rate="0.125"))
    deal = on_grid.deal_cell
    assert deal is not None and deal is on_grid.rows[-1].cells[0]
    assert (deal.net_profit, deal.cash_invested) == (D(17650), D(76750))


def test_the_deal_cell_is_the_flip_analysis_itself() -> None:
    result = underwrite(wichita_inputs(interest_rate="0.125"), CONFIG)
    assert result.borrower_sensitivity is not None
    cell = result.borrower_sensitivity.deal_cell
    assert cell is not None and cell.net_profit == result.flip.net_profit
    assert cell.total_costs == result.flip.total_costs
    assert cell.project_yield == result.flip.project_yield
    assert cell.cash_invested == result.flip.borrower_equity


def test_rows_and_columns_line_up_with_the_lender_sensitivity() -> None:
    """Denver is a split product with a draw schedule: the two tables still share a shape."""
    block = json.loads((FIXTURE_DIR / "go_holdback_denver.json").read_text(encoding="utf-8"))
    inputs = UnderwriteInputs.model_validate(block["underwrite"]["inputs"])
    result = underwrite(inputs, CONFIG)
    lender, borrower = result.sensitivity, result.borrower_sensitivity
    assert lender is not None and borrower is not None
    assert borrower.rates == lender.rates
    assert [r.loan_amount for r in borrower.rows] == [r.loan_amount for r in lender.rows]
    assert [[c.is_deal for c in r.cells] for r in borrower.rows] == [
        [c.is_deal for c in r.cells] for r in lender.rows
    ]


def no_price() -> UnderwriteInputs:
    return wichita_inputs(
        deal={
            **wichita_inputs().deal.model_dump(),
            "estimated_sale_price": None,
            "estimated_sale_price_source": None,
        }
    )


def test_not_evaluated_with_no_sale_price() -> None:
    table = table_for(no_price())
    assert table.status is AnalysisStatus.NOT_EVALUATED
    assert table.rows == [] and table.deal_cell is None
    assert table.rates  # the columns are still the lender's, for the page to label


def test_the_status_follows_the_sale_price_alone() -> None:
    """There is no toggle (engine 2.0.0): a price evaluates the table, no price does not."""
    assert borrower_status(wichita_inputs()) is AnalysisStatus.EVALUATED
    assert borrower_status(no_price()) is AnalysisStatus.NOT_EVALUATED
    result = underwrite(no_price(), CONFIG)
    assert result.borrower_sensitivity is not None
    assert result.borrower_sensitivity.status is AnalysisStatus.NOT_EVALUATED


def test_no_cash_on_cash_when_the_loan_covers_every_cost() -> None:
    """A loan above the whole cost stack leaves the borrower no cash in: no ratio, not infinity."""
    deal = {**wichita_inputs().deal.model_dump(), "loan_requested": "260000.00"}
    table = table_for(wichita_inputs(deal=deal))
    cell = table.rows[-1].cells[0]
    assert cell.cash_invested < 0 and cell.cash_on_cash is None


def test_a_shared_grid_changes_nothing() -> None:
    inputs = wichita_inputs()
    credit = credit_check(inputs.borrower, CONFIG)
    experience = experience_check(inputs.borrower, CONFIG)
    grid = Grid(inputs, credit.tranche, experience.tier, CONFIG)
    shared = borrower_sensitivity_table(inputs, credit.tranche, experience.tier, CONFIG, grid)
    assert shared == table_for(inputs)
    assert sensitivity_table(inputs, credit.tranche, experience.tier, CONFIG, grid) == (
        sensitivity_table(inputs, credit.tranche, experience.tier, CONFIG)
    )


def test_a_result_from_before_1_7_0_still_loads() -> None:
    stored = underwrite(wichita_inputs(), CONFIG).model_dump(mode="json")
    del stored["borrower_sensitivity"], stored["pricing"]
    from schema.models import UnderwriteResult

    older = UnderwriteResult.model_validate(stored)
    assert older.borrower_sensitivity is None and older.pricing is None
