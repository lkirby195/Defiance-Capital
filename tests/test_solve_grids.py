"""engine/solve.py and engine/grids.py: closed-form r*, the yield grid.  # SPEC §8.4, §8.5"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config.config import DEFAULT_PATH, Config, load_yaml
from engine.calc.lender import fee_schedule, interest, lender_return
from engine.calc.outstanding import LoanTerms, dollar_months, loan_terms
from engine.grids import grid_cell, month_axis, rate_axis, yield_grid
from engine.sizing import size_deal
from engine.solve import required_interest, solve_rate, target_income
from schema.models import ExperienceTier, Product, SizingInputs, Tranche, YieldGrid

CONFIG = Config.load()
D = Decimal
CENT = D("0.01")
TIGHT = D("0.000000000001")


def config_with(**sections: dict[str, Any]) -> Config:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    for section, values in sections.items():
        for key, value in values.items():
            data[section][key] = value
    return Config.from_dict(data)


def deal(product: Product = Product.NO_DRAW, **overrides: Any) -> SizingInputs:
    base: dict[str, Any] = {
        "product": product,
        "purchase_price": D("150000.00"),
        "rehab_budget": D("60000.00") if product is Product.SPLIT_PRINCIPAL else D("0.00"),
        "loan_requested": D("170000.00") if product is Product.SPLIT_PRINCIPAL else D("105000.00"),
        "as_is_value": D("230000.00"),
        "arv": D("290000.00"),
    }
    base.update(overrides)
    return SizingInputs(**base)


def loan(
    product: Product = Product.NO_DRAW,
    term: int = 9,
    extension: Decimal | None = None,
    config: Config = CONFIG,
    **overrides: Any,
) -> LoanTerms:
    sizing = size_deal(deal(product, **overrides), Tranche.T2, ExperienceTier.E2, config)
    return loan_terms(sizing, term, extension, config)


# --- §8.4 solve ----------------------------------------------------------------------------------


def test_target_income_and_required_interest() -> None:
    terms = loan(term=9)
    # 0.175 x 105,000 x 9 / 12 = 13,781.25; less 2% fees (2,100) = 11,681.25
    assert target_income(terms, CONFIG) == D("13781.25")
    assert required_interest(terms, CONFIG) == D("11681.25")


def test_solve_rate_no_draw_by_hand() -> None:
    # r* = 11,681.25 / (105,000 x 9 / 12) = 11,681.25 / 78,750 = 0.148333...
    r_star = solve_rate(loan(term=9), CONFIG)
    assert r_star.quantize(D("0.000001")) == D("0.148333")


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL, Product.SPLIT_DRAW])
@pytest.mark.parametrize("term, expected", [(6, "0.135"), (9, "0.148333333333"), (12, "0.155")])
def test_single_note_products_solve_to_target_less_annualized_fees(
    product: Product, term: int, expected: str
) -> None:
    # Fully funded from close: r* = target - origination_pct x 12 / term, independent of size.
    r_star = solve_rate(loan(product, term=term), CONFIG)
    assert r_star.quantize(D("0.000000000001")) == D(expected)


def test_solve_rate_split_principal_by_hand() -> None:
    # Principal Note 104,000 + Tranche A 66,000, term 12, rehab 9: dollar-months
    # 104,000 x 12 + 66,000 x (4.5 + 3) = 1,743,000; avg outstanding 145,250.
    # target income 0.175 x 170,000 = 29,750; fees 3,400; required interest 26,350
    # r* = 26,350 / 145,250 = 0.181411...
    r_star = solve_rate(loan(Product.SPLIT_PRINCIPAL, term=12), CONFIG)
    assert r_star.quantize(D("0.000001")) == D("0.181411")
    assert r_star > solve_rate(loan(Product.NO_DRAW, term=12), CONFIG)  # less capital at work


@pytest.mark.parametrize("product", list(Product))
@pytest.mark.parametrize("term", [4, 6, 9, 12, 18])
def test_yield_at_solved_rate_hits_target_to_the_cent(product: Product, term: int) -> None:
    terms = loan(product, term=term)
    r_star = solve_rate(terms, CONFIG)
    income = interest(terms, term, r_star, CONFIG) + fee_schedule(terms, term, CONFIG).total
    assert income.quantize(CENT) == target_income(terms, CONFIG).quantize(CENT)
    result = lender_return(terms, term, r_star, CONFIG)
    assert result.annualized_yield.quantize(TIGHT) == CONFIG.returns.target_irr


def test_solve_uses_target_from_config() -> None:
    cfg = config_with(returns={"target_irr": D("0.20")})
    # single note, 12 months: r* = 0.20 - 0.02 = 0.18
    assert solve_rate(loan(term=12, config=cfg), cfg) == D("0.18")


def test_extension_fee_does_not_enter_the_solve() -> None:
    # r* is solved at the term, where the extension fee is never charged
    assert solve_rate(loan(term=9, extension=D("0.02")), CONFIG) == solve_rate(loan(term=9), CONFIG)


def test_solve_can_go_negative_on_very_short_terms() -> None:
    # 1 month: fees alone annualize to 24% > 17.5% target
    assert solve_rate(loan(term=1), CONFIG) < 0


money = st.decimals(min_value=D("10000"), max_value=D("5000000"), places=2)


@settings(max_examples=100, deadline=None)
@given(
    product=st.sampled_from(list(Product)),
    price=money,
    rehab=st.decimals(min_value=D("0"), max_value=D("1000000"), places=2),
    loan_amount=money,
    term=st.integers(min_value=1, max_value=36),
)
def test_solve_hits_target_for_any_deal(
    product: Product, price: Decimal, rehab: Decimal, loan_amount: Decimal, term: int
) -> None:
    terms = loan(
        product, term=term, purchase_price=price, rehab_budget=rehab, loan_requested=loan_amount
    )
    r_star = solve_rate(terms, CONFIG)
    result = lender_return(terms, term, r_star, CONFIG)
    assert result.annualized_yield.quantize(TIGHT) == CONFIG.returns.target_irr
    assert dollar_months(terms, term, CONFIG) > 0


# --- §8.5 grid -----------------------------------------------------------------------------------


def test_rate_axis_is_the_config_grid_plus_r_star_in_order() -> None:
    rates, inserted = rate_axis(D("0.1483"), CONFIG)
    assert inserted is True
    assert len(rates) == 12
    assert rates[0] == D("0.10") and rates[-1] == D("0.15")
    assert rates == sorted(rates)
    assert rates.index(D("0.1483")) == 10  # between 14.5% and 15.0%
    assert [r for r in rates if r != D("0.1483")] == [D("0.10") + D("0.005") * k for k in range(11)]


def test_rate_axis_does_not_duplicate_r_star_on_the_grid() -> None:
    rates, inserted = rate_axis(D("0.125"), CONFIG)
    assert inserted is False and len(rates) == 11
    rates, inserted = rate_axis(D("0.1250000000"), CONFIG)  # Decimal equality, not text
    assert inserted is False and len(rates) == 11


def test_rate_axis_inserts_r_star_outside_the_grid_range() -> None:
    low, _ = rate_axis(D("0.08"), CONFIG)
    high, _ = rate_axis(D("0.19"), CONFIG)
    assert low[0] == D("0.08") and high[-1] == D("0.19")


def test_rate_axis_from_config() -> None:
    cfg = config_with(
        returns={"rate_grid": {"min": D("0.08"), "max": D("0.12"), "step": D("0.01")}}
    )
    rates, _ = rate_axis(D("0.10"), cfg)
    assert rates == [D("0.08"), D("0.09"), D("0.10"), D("0.11"), D("0.12")]


def test_month_axis_runs_term_through_term_plus_after() -> None:
    assert month_axis(9, CONFIG) == [9, 10, 11, 12, 13, 14, 15]
    assert month_axis(6, config_with(returns={"month_window": {"after_term": 2}})) == [6, 7, 8]


def test_grid_cell_flags_at_or_above_target() -> None:
    terms = loan(term=9)
    r_star = solve_rate(terms, CONFIG)
    at = grid_cell(terms, 9, r_star, r_star, CONFIG)
    assert at.meets_target is True and at.is_solved_rate is True
    assert at.annualized_yield.quantize(TIGHT) == CONFIG.returns.target_irr
    below = grid_cell(terms, 9, D("0.145"), r_star, CONFIG)
    assert below.meets_target is False and below.is_solved_rate is False
    above = grid_cell(terms, 9, D("0.15"), r_star, CONFIG)
    assert above.meets_target is True


def test_yield_grid_shape_and_flagging() -> None:
    terms = loan(term=9)
    r_star = solve_rate(terms, CONFIG)
    grid = yield_grid(terms, r_star, CONFIG)
    assert grid.target == D("0.175") and grid.solved_rate == r_star
    assert grid.solved_rate_inserted is True
    assert grid.months == [9, 10, 11, 12, 13, 14, 15]
    assert len(grid.rates) == 12 and len(grid.rows) == 7
    assert all(len(row.cells) == 12 for row in grid.rows)
    assert [row.month for row in grid.rows] == grid.months
    for row in grid.rows:
        assert [c.rate for c in row.cells] == grid.rates
        assert all(c.month == row.month for c in row.cells)
        assert sum(c.is_solved_rate for c in row.cells) == 1
        # yields rise with rate along a row
        yields = [c.annualized_yield for c in row.cells]
        assert yields == sorted(yields)
        assert all(c.meets_target is (c.annualized_yield >= grid.target) for c in row.cells)
    # term row: r* and 15.0% meet the target; 14.5% and below do not
    term_row = grid.rows[0]
    assert [c.meets_target for c in term_row.cells] == [False] * 10 + [True, True]
    # sliding past the term without an extension fee lowers every yield
    for col in range(12):
        column = [row.cells[col].annualized_yield for row in grid.rows]
        assert column == sorted(column, reverse=True)


def test_yield_grid_with_extension_fee_steps_up_past_term() -> None:
    terms = loan(term=9, extension=D("0.02"))
    grid = yield_grid(terms, solve_rate(terms, CONFIG), CONFIG)
    col = grid.rates.index(D("0.15"))
    at_term = grid.rows[0].cells[col].annualized_yield
    month_after = grid.rows[1].cells[col].annualized_yield
    assert month_after > at_term  # 2% extension fee outweighs one more month of denominator


def test_yield_grid_round_trips_as_json() -> None:
    terms = loan(Product.SPLIT_PRINCIPAL, term=12)
    grid = yield_grid(terms, solve_rate(terms, CONFIG), CONFIG)
    again = YieldGrid.model_validate_json(grid.model_dump_json())
    assert again == grid
    assert isinstance(again.rows[0].cells[0].annualized_yield, Decimal)
