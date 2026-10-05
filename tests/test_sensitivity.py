"""The sensitivity table: IRR by loan amount and rate, every cell a ledger re-run.  # SPEC §8.9

Two checks carry the weight. The base cell - the request at the deal's own rate - has to be
the deal's own IRR, because the table claims to be the same ledger laid out again. And every
other cell is checked against a ledger built here, by hand, with no code shared with
``engine/calc``: month ends from ``calendar``, the draws and the interest written out as
SPEC §8.3 states them, and the XIRR solved with the float secant method the workbook test
uses. If the two agree to a millionth on every cell they agree about the arithmetic.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from engine.calc.sensitivity import (
    rate_columns,
    reduced_deal,
    reduced_split,
    reductions,
    sensitivity_table,
)
from engine.screen import credit_check, experience_check
from engine.underwrite import underwrite
from schema.models import (
    BorrowerInputs,
    ExperienceBucket,
    Product,
    SensitivityTable,
    SizingInputs,
    State,
    Tranche,
    UnderwriteInputs,
    UnderwriteResult,
)
from tests.test_excel_export import xirr_by_secant

CONFIG = Config.load()
D = Decimal
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
DENVER = FIXTURE_DIR / "go_split_draw_denver.json"


def denver_inputs(**changes: Any) -> UnderwriteInputs:
    """The Denver fixture's own underwrite inputs, with whatever the test wants different."""
    block = json.loads(DENVER.read_text(encoding="utf-8"))["underwrite"]["inputs"]
    return UnderwriteInputs.model_validate({**block, **changes})


def table_for(inputs: UnderwriteInputs) -> SensitivityTable:
    credit = credit_check(inputs.borrower, CONFIG)
    experience = experience_check(inputs.borrower, CONFIG)
    return sensitivity_table(inputs, credit.tranche, experience.tier, CONFIG)


# --- the shape, from config ----------------------------------------------------------------------


GRID = [D("0.125"), D("0.135"), D("0.145"), D("0.155"), D("0.165"), D("0.175")]


def test_the_columns_and_rows_come_from_config() -> None:
    assert rate_columns(CONFIG) == GRID
    assert reductions(CONFIG) == [D(20000), D(15000), D(10000), D(5000), D(0)]


def test_the_deals_own_rate_is_a_column_in_rate_order_and_never_twice() -> None:
    """SPEC §8.9: the grid plus the deal's rate - between two columns, below, above, or on one."""
    assert rate_columns(CONFIG, D("0.12")) == [D("0.12"), *GRID]
    assert rate_columns(CONFIG, D("0.14")) == [*GRID[:2], D("0.14"), *GRID[2:]]
    assert rate_columns(CONFIG, D("0.20")) == [*GRID, D("0.20")]
    assert rate_columns(CONFIG, D("0.145")) == GRID
    assert rate_columns(CONFIG, D("0.14500")) == GRID  # numerically equal is the same column


def test_the_request_is_the_bottom_row_and_each_row_above_is_one_step_less() -> None:
    table = table_for(denver_inputs())
    assert [row.loan_amount for row in table.rows] == [
        D(175000),
        D(180000),
        D(185000),
        D(190000),
        D(195000),
    ]
    assert table.rows[-1].reduction == 0 and table.rows[-1].loan_amount == table.loan_requested
    # the six grid rates plus the deal's own 12%, first because it is the lowest
    assert table.rates == [D("0.12"), *GRID]
    assert [len(row.cells) for row in table.rows] == [7] * 5
    assert [cell.interest_rate for cell in table.rows[0].cells] == table.rates


def test_a_reshaped_grid_follows_the_yaml() -> None:
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["sensitivity"] = {
        "loan_step_usd": "10000.00",
        "loan_steps": 2,
        "rate_min": "0.10",
        "rate_max": "0.12",
        "rate_step": "0.005",
    }
    config = Config.from_dict(data)
    assert rate_columns(config) == [D("0.10"), D("0.105"), D("0.11"), D("0.115"), D("0.12")]
    assert reductions(config) == [D(20000), D(10000), D(0)]
    assert len(table_for(denver_inputs()).rates) == 7  # the default config: six, plus 12%


def test_a_grid_whose_rates_run_backwards_is_refused_at_load() -> None:
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["sensitivity"]["rate_max"] = "0.10"
    with pytest.raises(ConfigError, match="rate_max"):
        Config.from_dict(data)


# --- the base cell is the deal's own IRR ---------------------------------------------------------


def test_the_base_cell_equals_the_deals_irr_when_the_rate_is_on_the_grid() -> None:
    inputs = denver_inputs(interest_rate="0.145")
    result = underwrite(inputs, CONFIG)
    assert result.sensitivity is not None
    cell = result.sensitivity.deal_cell
    assert cell is not None and cell.is_deal
    assert cell.interest_rate == D("0.145")
    assert cell.irr == result.return_overview.irr
    assert result.sensitivity.rates == GRID  # on a grid rate: no second 14.5% column
    # ...and it sits on the bottom row, the request's own
    bottom = result.sensitivity.rows[-1]
    assert bottom.loan_amount == inputs.deal.loan_requested
    assert cell in bottom.cells
    assert sum(1 for row in result.sensitivity.rows for c in row.cells if c.is_deal) == 1


def test_a_deal_priced_off_the_grid_gets_its_own_column_and_cell() -> None:
    """The placeholder rate (12%) is below the grid, so it is the first column."""
    result = underwrite(denver_inputs(), CONFIG)
    assert result.sensitivity is not None
    assert result.sensitivity.interest_rate == D("0.12")
    assert result.sensitivity.rates[0] == D("0.12")
    cell = result.sensitivity.deal_cell
    assert cell is not None and cell.interest_rate == D("0.12")
    assert cell.irr == result.return_overview.irr
    assert cell is result.sensitivity.rows[-1].cells[0]
    assert sum(1 for row in result.sensitivity.rows for c in row.cells if c.is_deal) == 1
    assert len(result.sensitivity.rows) == 5


def test_a_deal_priced_between_two_columns_sits_between_them() -> None:
    result = underwrite(denver_inputs(interest_rate="0.14"), CONFIG)
    assert result.sensitivity is not None
    assert result.sensitivity.rates == [*GRID[:2], D("0.14"), *GRID[2:]]
    cell = result.sensitivity.deal_cell
    assert cell is not None and cell.irr == result.return_overview.irr
    assert result.sensitivity.rows[-1].cells[2] is cell


# --- how a reduction is taken ---------------------------------------------------------------------


def test_a_reduction_comes_off_the_advance_first_then_the_rehab_portion() -> None:
    assert reduced_split(D(147000), D(48000), D(5000)) == (D(142000), D(48000))
    assert reduced_split(D(147000), D(48000), D(0)) == (D(147000), D(48000))
    # the advance runs out: the rest comes off the rehab portion
    assert reduced_split(D(15000), D(30000), D(20000)) == (D(0), D(25000))
    assert reduced_split(D(15000), D(30000), D(15000)) == (D(0), D(30000))


def test_a_single_note_product_simply_lends_less() -> None:
    deal = SizingInputs(
        product=Product.NO_DRAW,
        purchase_price=D(100000),
        rehab_costs=D(0),
        loan_requested=D(70000),
    )
    smaller = reduced_deal(deal, D(5000))
    assert smaller is not None
    assert smaller.loan_requested == D(65000) and smaller.loan_split is None
    assert reduced_deal(deal, D(70000)) is None  # nothing left to lend


def test_a_row_that_would_leave_no_loan_is_left_out() -> None:
    inputs = denver_inputs(
        deal={
            "product": "NO_DRAW",
            "purchase_price": "100000.00",
            "rehab_costs": "0.00",
            "loan_requested": "15000.00",
        }
    )
    table = table_for(inputs)
    assert [row.loan_amount for row in table.rows] == [D(5000), D(10000), D(15000)]


# --- every cell against an independent ledger (SPEC §8.3, §8.9) ----------------------------------


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def anchor(closing: date, months: int) -> date:
    total = closing.month - 1 + months
    return month_end(closing.year + total // 12, total % 12 + 1)


def split_draw_flows(
    *,
    advance: Decimal,
    rehab: Decimal,
    commitment: Decimal,
    rate: Decimal,
    closing: date,
    term: int,
) -> list[tuple[date, float]]:
    """A SPLIT_DRAW ledger written out as SPEC §8.3 states it, in plain floats.

    Interest on the full commitment every month; the rehab portion drawn straight-line over
    ``term - listing_months``; the origination fee half at close and half at payoff; the
    commitment back on the last row. No code from ``engine/`` is used.
    """
    rehab_months = term - CONFIG.draws.listing_months
    fee_half = float(commitment) * float(CONFIG.fees.origination_default_pct) / 2
    draw = float(rehab) / rehab_months
    flows = [(anchor(closing, 0), -float(advance) + fee_half)]
    for month in range(1, term + 1):
        net = float(commitment) * float(rate) / 12
        if month <= rehab_months:
            net -= draw
        if month == term:
            net += fee_half + float(commitment)
        flows.append((anchor(closing, month), net))
    return flows


def test_every_denver_cell_matches_the_independent_ledger() -> None:
    """On the Denver fixture every reduction comes off the advance; the rehab stays $48,000."""
    inputs = denver_inputs()
    table = table_for(inputs)
    assert len(table.rows) == 5
    for row in table.rows:
        assert row.loan_rehab_portion == D(48000)
        assert row.loan_purchase_portion == D(147000) - row.reduction
        for cell in row.cells:
            assert cell.irr is not None
            expected = xirr_by_secant(
                split_draw_flows(
                    advance=D(147000) - row.reduction,
                    rehab=D(48000),
                    commitment=D(195000) - row.reduction,
                    rate=cell.interest_rate,
                    closing=date(2026, 10, 15),
                    term=9,
                )
            )
            assert abs(float(cell.irr) - expected) < 1e-6, (row.loan_amount, cell.interest_rate)


def test_a_small_advance_runs_out_and_the_rehab_gives_matching_the_independent_ledger() -> None:
    """$15,000 at close, $30,000 of rehab: the -$20,000 row advances nothing and holds $25,000."""
    inputs = UnderwriteInputs(
        deal=SizingInputs(
            product=Product.SPLIT_DRAW,
            purchase_price=D(100000),
            rehab_costs=D(30000),
            loan_requested=D(45000),
            loan_purchase_portion=D(15000),
            loan_rehab_portion=D(30000),
            estimated_sale_price=D(200000),
        ),
        state=State.OK,
        borrower=BorrowerInputs(
            credit_range_self_reported=Tranche.T1,
            experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
            repeat_borrower_self_reported=False,
        ),
        closing_date=date(2027, 1, 1),
        term_months=9,
        interest_rate=D("0.12"),
    )
    table = table_for(inputs)
    splits = [(row.loan_purchase_portion, row.loan_rehab_portion) for row in table.rows]
    assert splits == [
        (D(0), D(25000)),
        (D(0), D(30000)),
        (D(5000), D(30000)),
        (D(10000), D(30000)),
        (D(15000), D(30000)),
    ]
    for row in table.rows:
        assert row.loan_purchase_portion is not None and row.loan_rehab_portion is not None
        for cell in row.cells:
            assert cell.irr is not None
            expected = xirr_by_secant(
                split_draw_flows(
                    advance=row.loan_purchase_portion,
                    rehab=row.loan_rehab_portion,
                    commitment=row.loan_amount,
                    rate=cell.interest_rate,
                    closing=date(2027, 1, 1),
                    term=9,
                )
            )
            assert abs(float(cell.irr) - expected) < 1e-6, (row.loan_amount, cell.interest_rate)


# --- it is on the result, and a stored result from before it still loads -------------------------


def test_the_underwrite_result_carries_the_table_and_an_older_row_loads_without_one() -> None:
    result = underwrite(denver_inputs(), CONFIG)
    assert result.sensitivity is not None
    dumped = result.model_dump(mode="json")
    rebuilt = UnderwriteResult.model_validate(dumped)
    assert rebuilt.sensitivity == result.sensitivity
    del dumped["sensitivity"]  # a 1.4.0 row
    older = UnderwriteResult.model_validate(dumped)
    assert older.sensitivity is None
    assert older.return_overview == result.return_overview
