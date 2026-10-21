"""The pricing matrix: the lowest grid rate that earns the target, per loan amount step.

# SPEC §8.10

Wichita is the independent check. A NO_DRAW note's flows all scale with the loan, so its
IRR does not depend on the amount at all, and the IRR at each grid rate is checked here
against a bisection XIRR over hand-built flows (month ends from ``calendar``, actual/365),
no code shared with ``engine/calc``. At the 20% placeholder target 15.5% falls short and
16.5% clears for every step; the borrower's figures at 16.5% are the Borrower Sensitivity
formulas written out in ``tests/test_borrower_sensitivity.py``.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from engine.calc.borrower import BorrowerReturns
from engine.calc.pricing import dollars, pricing_matrix, pricing_sentence, rate_text
from engine.screen import credit_check, experience_check
from engine.underwrite import underwrite
from schema.models import AnalysisStatus, PricingMatrix, UnderwriteInputs
from tests.test_borrower_sensitivity import by_hand, wichita_inputs

CONFIG = Config.load()
D = Decimal


def config_with(**pricing: Any) -> Config:
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["pricing"] = {**data["pricing"], **pricing}
    return Config.from_dict(data)


def matrix_for(
    inputs: UnderwriteInputs, config: Config = CONFIG, flip_on: bool = True
) -> PricingMatrix:
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    return pricing_matrix(inputs, credit.tranche, experience.tier, flip_on, config)


def wichita_irr_by_hand(rate: Decimal) -> float:
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


def test_placeholders() -> None:
    assert CONFIG.pricing.target_lender_irr == D("0.20")
    assert CONFIG.pricing.loan_amount_steps == [D(0), D(-10000), D(-20000)]


def test_wichita_against_the_ledger_written_out_by_hand() -> None:
    matrix = matrix_for(wichita_inputs())
    assert matrix.target_lender_irr == D("0.20")
    assert matrix.borrower_status is AnalysisStatus.EVALUATED
    assert wichita_irr_by_hand(D("0.155")) < 0.20 < wichita_irr_by_hand(D("0.165"))
    assert [row.loan_amount for row in matrix.rows] == [D(150000), D(140000), D(130000)]
    for row in matrix.rows:
        assert row.interest_rate == D("0.165")
        assert row.lender_irr is not None
        assert abs(float(row.lender_irr) - wichita_irr_by_hand(D("0.165"))) < 1e-7
        profit, cash, coc = by_hand(row.loan_amount, D("0.165"))
        assert (row.borrower_profit, row.borrower_cash_invested) == (profit, cash)
        assert row.borrower_cash_on_cash is not None
        assert abs(row.borrower_cash_on_cash - coc) < D("1e-20")


def test_the_recorded_values_from_the_independent_script() -> None:
    rows = matrix_for(wichita_inputs()).rows
    assert round(rows[0].lender_irr or 0, 8) == D("0.20190938")
    assert [(r.borrower_profit, r.borrower_cash_invested) for r in rows] == [
        (D(16850), D(82750)),
        (D(18700), D(90900)),
        (D(20550), D(99050)),
    ]
    assert [r.sentence for r in rows] == [
        "A $150,000 loan at 16.5% returns $16,850 profit (20.4% cash-on-cash).",
        "A $140,000 loan at 16.5% returns $18,700 profit (20.6% cash-on-cash).",
        "A $130,000 loan at 16.5% returns $20,550 profit (20.7% cash-on-cash).",
    ]


def test_the_lowest_clearing_rate_wins() -> None:
    """At a 15% target the deal's own 12% falls short and 12.5% is the first to clear."""
    rows = matrix_for(wichita_inputs(), config_with(target_lender_irr="0.15")).rows
    assert wichita_irr_by_hand(D("0.12")) < 0.15 < wichita_irr_by_hand(D("0.125"))
    assert {row.interest_rate for row in rows} == {D("0.125")}


def test_a_row_no_grid_rate_clears_says_so() -> None:
    rows = matrix_for(wichita_inputs(), config_with(target_lender_irr="0.50")).rows
    for row in rows:
        assert not row.cleared
        assert row.lender_irr is None and row.borrower_profit is None
    assert rows[0].sentence == (
        "No rate on the grid (12% to 17.5%) gets a $150,000 loan to the 50% lender IRR target."
    )


def test_a_step_that_leaves_no_loan_cannot_be_priced() -> None:
    rows = matrix_for(wichita_inputs(), config_with(loan_amount_steps=[0, -150000])).rows
    assert rows[0].cleared
    assert not rows[1].cleared and rows[1].loan_amount == D(0)
    assert rows[1].sentence == "A $0 loan cannot be priced: it leaves no commitment to lend."


def test_the_borrower_side_is_not_evaluated_without_a_flip() -> None:
    matrix = matrix_for(wichita_inputs(), flip_on=False)
    assert matrix.borrower_status is AnalysisStatus.NOT_EVALUATED
    row = matrix.rows[0]
    assert row.interest_rate == D("0.165") and row.borrower_profit is None
    assert "not evaluated" in row.sentence


def test_the_underwrite_carries_the_matrix() -> None:
    result = underwrite(wichita_inputs(), CONFIG)
    assert result.pricing == matrix_for(wichita_inputs())


def test_the_sentence_wording() -> None:
    returns = BorrowerReturns(D("-5000.4"), D(1000), D("-5.0004"))
    assert pricing_sentence(D(140000), D("0.1275"), returns, D("0.2"), [D("0.12")]) == (
        "A $140,000 loan at 12.75% returns -$5,000 profit (-500.0% cash-on-cash)."
    )
    no_cash = BorrowerReturns(D(100), D(0), None)
    assert "no cash-on-cash" in pricing_sentence(D(1), D("0.12"), no_cash, D("0.2"), [D("0.12")])
    assert (dollars(D("1234.5")), rate_text(D("0.12")), rate_text(D("0.145"))) == (
        "$1,235",
        "12%",
        "14.5%",
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
