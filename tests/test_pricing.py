"""The pricing matrix: the lowest rate that earns the target, solved to 0.01%.  # SPEC §8.10

Two fixtures carry the weight, each against a solve done here with no code shared with
``engine/calc``. The ledgers are the ones the sensitivity tests already write out by hand -
Wichita's NO_DRAW note in closed form, Denver's SPLIT_DRAW flows from
``tests/test_sensitivity.split_draw_flows`` - with their own month ends and their own float
XIRR. The solve is a different method from the engine's on purpose: a secant root-find on
the continuous rate at which the IRR equals the target, rounded up to the next 0.01% and
then nudged a step either way until the rate one step down falls short and the rate itself
clears. The engine bisects on whole 0.01% steps over Decimal ledgers; if the two land on the
same rate, the rate is the minimum.

The recorded values were computed first by a standalone script of the same kind and agree
with the engine to the last digit: Wichita 16.34% on every step (its flows scale with the
loan, so its IRR does not depend on the amount), Denver 13.82%, 13.72% and 13.61%.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from engine.calc.borrower import BorrowerReturns
from engine.calc.pricing import (
    RATE_CEILING,
    RATE_PRECISION,
    dollars,
    pricing_matrix,
    pricing_sentence,
    rate_text,
    solve_rate,
)
from engine.calc.sensitivity import Grid
from engine.screen import credit_check, experience_check
from engine.underwrite import underwrite
from schema.models import AnalysisStatus, PricingMatrix, UnderwriteInputs, UnderwriteResult
from tests.test_borrower_sensitivity import by_hand, wichita_inputs
from tests.test_excel_export import xirr_by_secant
from tests.test_sensitivity import denver_inputs, split_draw_flows

CONFIG = Config.load()
D = Decimal
STEP = 1e-4  # 0.01%, as a float for the independent solve


def config_with(**pricing: Any) -> Config:
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["pricing"] = {**data["pricing"], **pricing}
    return Config.from_dict(data)


def matrix_for(
    inputs: UnderwriteInputs,
    config: Config = CONFIG,
    flip_on: bool = True,
    ceiling: Decimal = RATE_CEILING,
) -> PricingMatrix:
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    return pricing_matrix(inputs, credit.tranche, experience.tier, flip_on, config, ceiling=ceiling)


def wichita_irr_by_hand(rate: Decimal | float) -> float:
    """XIRR of a $150,000 12-month NO_DRAW note at ``rate`` with a 2% fee split either end."""
    dates = []
    for k in range(13):
        year, month = 2027 + (k // 12), k % 12 + 1
        dates.append(date(year, month, calendar.monthrange(year, month)[1]))
    loan, fee = 150000.0, 0.02
    flows = [-loan + loan * fee / 2] + [loan * float(rate) / 12] * 12
    flows[-1] += loan + loan * fee / 2

    def npv(x: float) -> float:
        return sum(
            c / (1 + x) ** ((d - dates[0]).days / 365.0) for c, d in zip(flows, dates, strict=True)
        )

    lo, hi = -0.99, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    return (lo + hi) / 2


def denver_irr_by_hand(rate: float, reduction: Decimal) -> float:
    """Denver's ledger ``reduction`` less, at ``rate``: every reduction comes off the advance."""
    return xirr_by_secant(
        split_draw_flows(
            advance=D(147000) - reduction,
            rehab=D(48000),
            commitment=D(195000) - reduction,
            rate=D(repr(rate)),
            closing=date(2026, 10, 15),
            term=9,
        )
    )


def independent_min_rate(irr_at: Callable[[float], float], target: float) -> Decimal:
    """The lowest 0.01% step whose IRR reaches ``target``, by a secant root-find and a ceiling.

    The secant finds the continuous rate where the IRR equals the target; rounding it up to
    the next step is the candidate, and the two loops make it exact against the same
    function: the answer clears, and one step below it does not.
    """
    a, b = 0.05, 0.30
    fa, fb = irr_at(a) - target, irr_at(b) - target
    for _ in range(100):
        c = b - fb * (b - a) / (fb - fa)
        a, fa, b, fb = b, fb, c, irr_at(c) - target
        if abs(b - a) < 1e-13:
            break
    steps = math.ceil(b / STEP - 1e-9)
    while irr_at(steps * STEP) < target:
        steps += 1
    while irr_at((steps - 1) * STEP) >= target:
        steps -= 1
    return D(steps) * RATE_PRECISION


def test_placeholders_and_the_solve_constants() -> None:
    assert CONFIG.pricing.target_lender_irr == D("0.20")
    assert CONFIG.pricing.loan_amount_steps == [D(0), D(-10000), D(-20000)]
    assert (RATE_PRECISION, RATE_CEILING) == (D("0.0001"), D(1))


# --- against the independent solve, two fixtures (SPEC §8.10) -------------------------------------


def test_wichita_against_the_independent_solve() -> None:
    expected = independent_min_rate(wichita_irr_by_hand, 0.20)
    assert expected == D("0.1634")
    matrix = matrix_for(wichita_inputs())
    assert matrix.target_lender_irr == D("0.20") and matrix.solved
    assert matrix.borrower_status is AnalysisStatus.EVALUATED
    assert [row.loan_amount for row in matrix.rows] == [D(150000), D(140000), D(130000)]
    for row in matrix.rows:
        assert row.interest_rate == expected
        assert row.lender_irr is not None and row.lender_irr >= D("0.20")
        assert abs(float(row.lender_irr) - wichita_irr_by_hand(expected)) < 1e-7
        profit, cash, coc = by_hand(row.loan_amount, expected)
        assert (row.borrower_profit, row.borrower_cash_invested) == (profit, cash)
        assert row.borrower_cash_on_cash is not None
        assert abs(row.borrower_cash_on_cash - coc) < D("1e-20")


def test_denver_against_the_independent_solve() -> None:
    """A draw ledger: each row's flows differ, so each row solves to its own rate."""
    rows = matrix_for(denver_inputs()).rows
    assert [row.loan_amount for row in rows] == [D(195000), D(185000), D(175000)]
    for row in rows:
        reduction = -row.step
        expected = independent_min_rate(lambda r, x=reduction: denver_irr_by_hand(r, x), 0.20)
        assert row.interest_rate == expected, row.loan_amount
        assert row.lender_irr is not None
        assert abs(float(row.lender_irr) - denver_irr_by_hand(float(expected), reduction)) < 1e-6
    assert [row.interest_rate for row in rows] == [D("0.1382"), D("0.1372"), D("0.1361")]


def test_the_rate_is_the_minimum_one_step_down_falls_short() -> None:
    """Rounded up, never down: the step below the answer misses the target on the ledger."""
    for row in matrix_for(denver_inputs()).rows:
        assert row.interest_rate is not None
        below = float(row.interest_rate - RATE_PRECISION)
        assert denver_irr_by_hand(below, -row.step) < 0.20 <= float(row.lender_irr or 0)


def test_the_recorded_values_from_the_independent_script() -> None:
    rows = matrix_for(wichita_inputs()).rows
    assert round(rows[0].lender_irr or 0, 8) == D("0.20001167")
    assert [(r.borrower_profit, r.borrower_cash_invested) for r in rows] == [
        (D(17090), D(82510)),
        (D(18924), D(90676)),
        (D(20758), D(98842)),
    ]
    assert [r.sentence for r in rows] == [
        "A $150,000 loan at 16.34% returns $17,090 profit (20.7% cash-on-cash).",
        "A $140,000 loan at 16.34% returns $18,924 profit (20.9% cash-on-cash).",
        "A $130,000 loan at 16.34% returns $20,758 profit (21.0% cash-on-cash).",
    ]


def test_another_target_solves_to_its_own_rate() -> None:
    """At a 15% target the answer sits between two grid columns, 12% and 12.5%."""
    rows = matrix_for(wichita_inputs(), config_with(target_lender_irr="0.15")).rows
    expected = independent_min_rate(wichita_irr_by_hand, 0.15)
    assert expected == D("0.1204")
    assert {row.interest_rate for row in rows} == {expected}


def test_a_fee_that_clears_the_target_alone_prices_at_zero() -> None:
    """Wichita's 2% fee over twelve months is about a 2% IRR on its own, so 1% needs no rate."""
    assert wichita_irr_by_hand(0) > 0.01
    rows = matrix_for(wichita_inputs(), config_with(target_lender_irr="0.01")).rows
    assert {row.interest_rate for row in rows} == {D(0)}


def test_a_row_nothing_up_to_the_ceiling_clears_says_so() -> None:
    matrix = matrix_for(wichita_inputs(), ceiling=D("0.15"))
    assert matrix.rate_ceiling == D("0.15")
    for row in matrix.rows:
        assert not row.cleared
        assert row.lender_irr is None and row.borrower_profit is None
    assert matrix.rows[0].sentence == (
        "No rate up to 15% gets a $150,000 loan to the 20% lender IRR target."
    )


def test_the_grid_seeds_are_only_a_bracket() -> None:
    """With no seeds at all, or seeds off the 0.01% steps, the solve lands on the same rate."""
    inputs = wichita_inputs()
    credit = credit_check(inputs.borrower, CONFIG)
    experience = experience_check(inputs.borrower, CONFIG)
    target = CONFIG.pricing.target_lender_irr
    answers = set()
    for seeds in ([], [D("0.12345"), D("0.16345")], [D("0.01"), D("0.9")]):
        grid = Grid(inputs, credit.tranche, experience.tier, CONFIG)
        solved = solve_rate(grid, D(0), target, seeds)
        assert solved is not None
        answers.add(solved[0])
    assert answers == {D("0.1634")}


def test_a_step_that_leaves_no_loan_cannot_be_priced() -> None:
    rows = matrix_for(wichita_inputs(), config_with(loan_amount_steps=[0, -150000])).rows
    assert rows[0].cleared
    assert not rows[1].cleared and rows[1].loan_amount == D(0)
    assert rows[1].sentence == "A $0 loan cannot be priced: it leaves no commitment to lend."


def test_the_borrower_side_is_not_evaluated_without_a_flip() -> None:
    matrix = matrix_for(wichita_inputs(), flip_on=False)
    assert matrix.borrower_status is AnalysisStatus.NOT_EVALUATED
    row = matrix.rows[0]
    assert row.interest_rate == D("0.1634") and row.borrower_profit is None
    assert "not evaluated" in row.sentence


def test_the_underwrite_carries_the_matrix() -> None:
    result = underwrite(wichita_inputs(), CONFIG)
    assert result.pricing == matrix_for(wichita_inputs())


def test_a_1_7_0_matrix_still_loads() -> None:
    """Searched over the grid, with its columns recorded and no solve fields."""
    stored = underwrite(wichita_inputs(), CONFIG).model_dump(mode="json")
    assert stored["pricing"] is not None
    for key in ("rate_precision", "rate_ceiling"):
        del stored["pricing"][key]
    stored["pricing"]["rates"] = ["0.12", "0.125", "0.135"]
    older = UnderwriteResult.model_validate(stored)
    assert older.pricing is not None and not older.pricing.solved
    assert older.pricing.rates == [D("0.12"), D("0.125"), D("0.135")]


def test_the_sentence_wording() -> None:
    returns = BorrowerReturns(D("-5000.4"), D(1000), D("-5.0004"))
    assert pricing_sentence(D(140000), D("0.1275"), returns, D("0.2")) == (
        "A $140,000 loan at 12.75% returns -$5,000 profit (-500.0% cash-on-cash)."
    )
    no_cash = BorrowerReturns(D(100), D(0), None)
    assert "no cash-on-cash" in pricing_sentence(D(1), D("0.12"), no_cash, D("0.2"))
    assert pricing_sentence(D(1000), None, None, D("0.2")) == (
        "No rate up to 100% gets a $1,000 loan to the 20% lender IRR target."
    )
    assert (dollars(D("1234.5")), rate_text(D("0.12")), rate_text(D("0.1437"))) == (
        "$1,235",
        "12%",
        "14.37%",
    )


@pytest.mark.parametrize(
    "pricing",
    [
        {"loan_amount_steps": []},
        {"loan_amount_steps": [0, 5000]},
        {"loan_amount_steps": [0, 0]},
        {"target_lender_irr": "1.5"},
    ],
    ids=["empty", "positive", "duplicate", "irr_over_one"],
)
def test_config_refuses_a_bad_pricing_block(pricing: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        config_with(**pricing)
