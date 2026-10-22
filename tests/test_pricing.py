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
loan, so its IRR does not depend on the amount), Denver 13.82%, 13.72% and 13.63%.

From engine 1.10.0 the rows are percent steps (0, -5%, -10%) rounded to the nearest $1,000,
so Wichita's $150,000 prices as $150,000, $143,000 (142,500 rounded half-up) and $135,000,
and Denver's $195,000 as $195,000, $185,000 (185,250) and $176,000 (175,500 rounded up); the
script was re-run on those amounts.
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
    row_amount,
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
    assert CONFIG.pricing.max_quoted_rate == D("0.18")
    assert CONFIG.pricing.loan_amount_pct_steps == [D(0), D("-0.05"), D("-0.10")]
    assert CONFIG.pricing.loan_amount_rounding == D(1000)
    assert (RATE_PRECISION, RATE_CEILING) == (D("0.0001"), D(1))


# --- the rows: a share of the request, rounded (SPEC §8.10, engine 1.10.0) ------------------------


def test_a_stepped_row_is_the_request_times_the_share_rounded_to_the_nearest_unit() -> None:
    assert row_amount(D(195000), D("-0.05"), D(1000)) == D(185000)  # 185,250 down
    assert row_amount(D(195000), D("-0.10"), D(1000)) == D(176000)  # 175,500 half-up
    assert row_amount(D(150000), D("-0.05"), D(1000)) == D(143000)  # 142,500 half-up
    assert row_amount(D(150000), D("-0.10"), D(1000)) == D(135000)  # exact
    assert row_amount(D(150000), D("-0.05"), D(500)) == D(142500)  # a finer unit keeps it
    assert row_amount(D(150000), D("-0.05"), D(1)) == D(142500)


def test_the_request_row_is_never_rounded_and_no_row_is_above_the_request() -> None:
    assert row_amount(D(150250), D(0), D(1000)) == D(150250)
    # a reduction too small to survive the rounding is the request again, not a larger loan
    assert row_amount(D(100600), D("-0.0005"), D(1000)) == D(100600)
    assert row_amount(D(1000), D("-0.5"), D(1000)) == D(1000)
    # and a whole reduction leaves nothing
    assert row_amount(D(1000), D(-1), D(1000)) == D(0)


def test_each_row_carries_its_share_its_dollar_change_and_its_label() -> None:
    rows = matrix_for(denver_inputs()).rows
    assert [row.pct_step for row in rows] == [D(0), D("-0.05"), D("-0.10")]
    assert [row.loan_amount for row in rows] == [D(195000), D(185000), D(176000)]
    assert [row.step for row in rows] == [D(0), D(-10000), D(-19000)]
    assert [row.label for row in rows] == ["Requested", "-5%", "-10%"]
    assert [row.is_request for row in rows] == [True, False, False]


def test_a_finer_share_is_labelled_with_the_digits_it_needs() -> None:
    rows = matrix_for(wichita_inputs(), config_with(loan_amount_pct_steps=[0, "-0.075"])).rows
    assert rows[1].label == "-7.5%" and rows[1].loan_amount == D(139000)  # 138,750 half-up


def test_the_rounding_unit_moves_the_rows() -> None:
    rows = matrix_for(denver_inputs(), config_with(loan_amount_rounding=100)).rows
    assert [row.loan_amount for row in rows] == [D(195000), D(185300), D(175500)]
    rows = matrix_for(denver_inputs(), config_with(loan_amount_rounding=5000)).rows
    assert [row.loan_amount for row in rows] == [D(195000), D(185000), D(175000)]


def test_a_1_9_0_row_loads_and_is_labelled_by_its_dollar_step() -> None:
    stored = underwrite(wichita_inputs(), CONFIG).model_dump(mode="json")
    assert stored["pricing"] is not None
    for row, step in zip(stored["pricing"]["rows"], ("0", "-10000", "-20000"), strict=True):
        del row["pct_step"]
        row["step"] = step
    older = UnderwriteResult.model_validate(stored)
    assert older.pricing is not None
    assert [row.pct_step for row in older.pricing.rows] == [None, None, None]
    assert [row.label for row in older.pricing.rows] == ["Requested", "-$10,000", "-$20,000"]


# --- against the independent solve, two fixtures (SPEC §8.10) -------------------------------------


def test_wichita_against_the_independent_solve() -> None:
    expected = independent_min_rate(wichita_irr_by_hand, 0.20)
    assert expected == D("0.1634")
    matrix = matrix_for(wichita_inputs())
    assert matrix.target_lender_irr == D("0.20") and matrix.solved
    assert matrix.borrower_status is AnalysisStatus.EVALUATED
    assert [row.loan_amount for row in matrix.rows] == [D(150000), D(143000), D(135000)]
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
    assert [row.loan_amount for row in rows] == [D(195000), D(185000), D(176000)]
    for row in rows:
        reduction = -row.step
        expected = independent_min_rate(lambda r, x=reduction: denver_irr_by_hand(r, x), 0.20)
        assert row.interest_rate == expected, row.loan_amount
        assert row.lender_irr is not None
        assert abs(float(row.lender_irr) - denver_irr_by_hand(float(expected), reduction)) < 1e-6
    assert [row.interest_rate for row in rows] == [D("0.1382"), D("0.1372"), D("0.1363")]


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
        (D("18373.8"), D("88226.2")),
        (D(19841), D(94759)),
    ]
    assert [r.sentence for r in rows] == [
        "A $150,000 loan at 16.34% returns $17,090 profit (20.7% cash-on-cash).",
        "A $143,000 loan at 16.34% returns $18,374 profit (20.8% cash-on-cash).",
        "A $135,000 loan at 16.34% returns $19,841 profit (20.9% cash-on-cash).",
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


# --- the max quoted rate (SPEC §8.10, engine 1.9.0) ----------------------------------------------


def test_a_rate_above_the_max_quote_is_not_a_quote() -> None:
    """Wichita needs 16.34% everywhere; at a 16% maximum no row is quoted and each says so."""
    matrix = matrix_for(wichita_inputs(), config_with(max_quoted_rate="0.16"))
    assert matrix.max_quoted_rate == D("0.16")
    for row in matrix.rows:
        assert not row.cleared and row.over_max_quote
        assert row.rate_needed == D("0.1634")
        assert row.lender_irr is None
        assert (row.borrower_profit, row.borrower_cash_invested, row.borrower_cash_on_cash) == (
            None,
            None,
            None,
        )
    assert [row.sentence for row in matrix.rows] == [
        "A $150,000 loan exceeds max quoted rate (16.34% needed).",
        "A $143,000 loan exceeds max quoted rate (16.34% needed).",
        "A $135,000 loan exceeds max quoted rate (16.34% needed).",
    ]


def test_a_rate_equal_to_the_max_quote_is_quoted() -> None:
    rows = matrix_for(wichita_inputs(), config_with(max_quoted_rate="0.1634")).rows
    assert {row.interest_rate for row in rows} == {D("0.1634")}
    assert not any(row.over_max_quote for row in rows)


def test_the_max_quote_is_per_row() -> None:
    """Denver's rows need 13.82%, 13.72% and 13.63%: a 13.7% maximum quotes only the last."""
    rows = matrix_for(denver_inputs(), config_with(max_quoted_rate="0.137")).rows
    assert [row.rate_needed for row in rows] == [D("0.1382"), D("0.1372"), None]
    assert [row.interest_rate for row in rows] == [None, None, D("0.1363")]
    assert rows[0].sentence == "A $195,000 loan exceeds max quoted rate (13.82% needed)."
    assert rows[2].sentence.startswith("A $176,000 loan at 13.63% returns")


def test_the_placeholder_maximum_quotes_every_fixture_row() -> None:
    for inputs in (wichita_inputs(), denver_inputs()):
        assert all(row.cleared for row in matrix_for(inputs).rows)


def test_a_1_8_0_matrix_still_loads() -> None:
    stored = underwrite(wichita_inputs(), CONFIG).model_dump(mode="json")
    assert stored["pricing"] is not None
    del stored["pricing"]["max_quoted_rate"]
    for row in stored["pricing"]["rows"]:
        del row["rate_needed"]
    older = UnderwriteResult.model_validate(stored)
    assert older.pricing is not None and older.pricing.max_quoted_rate is None
    assert not any(row.over_max_quote for row in older.pricing.rows)


def test_a_step_that_leaves_no_loan_cannot_be_priced() -> None:
    rows = matrix_for(wichita_inputs(), config_with(loan_amount_pct_steps=[0, -1])).rows
    assert rows[0].cleared
    assert not rows[1].cleared and rows[1].loan_amount == D(0)
    assert rows[1].label == "-100%" and rows[1].step == D(-150000)
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
        {"loan_amount_pct_steps": []},
        {"loan_amount_pct_steps": [0, "0.05"]},
        {"loan_amount_pct_steps": [0, 0]},
        {"loan_amount_pct_steps": [0, "-1.5"]},
        {"loan_amount_rounding": 0},
        {"loan_amount_rounding": -1000},
        {"target_lender_irr": "1.5"},
        {"max_quoted_rate": "0"},
        {"max_quoted_rate": "1.2"},
    ],
    ids=[
        "empty",
        "positive",
        "duplicate",
        "below_minus_one",
        "rounding_zero",
        "rounding_negative",
        "irr_over_one",
        "max_quote_zero",
        "max_quote_over_one",
    ],
)
def test_config_refuses_a_bad_pricing_block(pricing: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        config_with(**pricing)


def test_config_refuses_the_dollar_steps_by_name() -> None:
    """A yaml still carrying ``pricing.loan_amount_steps`` is told what replaced it."""
    with pytest.raises(ConfigError, match="loan_amount_steps.*loan_amount_pct_steps"):
        config_with(loan_amount_steps=[0, -10000])
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["pricing"]["loan_amount_pct_steps"]
    data["pricing"]["loan_amount_steps"] = [0, -10000]
    with pytest.raises(ConfigError, match="loan_amount_pct_steps"):
        Config.from_dict(data)
