"""The take-back: the lender's downside, solved on its XIRR.  # SPEC §8.6

The timeline is laid out by hand here on Wichita's note - the fixture the sensitivity and
pricing tests already write out in closed form - and the solved price is checked two ways:
against the engine's own XIRR (the dollar below it falls short, the dollar itself clears)
and against a float XIRR of this module's own, over month ends of its own, with no code
shared with ``engine/calc``. The recorded fixture figures (``tests/test_fixtures.py``) were
computed first by a standalone script of the same kind.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import pytest

from config.config import Config
from engine.calc.irr import xirr, xnpv
from engine.calc.take_back import (
    DOLLAR,
    NOT_SOLVED,
    PRICE_BOUND_MULTIPLE,
    RENT_BOUND_MULTIPLE,
    RENT_BOUND_USD,
    Timeline,
    default_month,
    lender_flows,
    lender_months,
    solve,
    solve_flows,
    take_back_analysis,
)
from schema.models import (
    AnalysisStatus,
    BoundHit,
    LegacyTakeBackAnalysis,
    Solve,
    SolveStatus,
    State,
)
from tests.test_calc import inputs, terms

CONFIG = Config.load()
D = Decimal
TARGET = CONFIG.pricing.target_lender_irr


def wichita_timeline() -> Timeline:
    """12-month note: default in month 9, 3 months of nothing, legal in month 12, six months
    of Oklahoma carry at $333.33, the REO sale in month 18 net of 6%."""
    loan = terms(inputs())
    return Timeline(
        default_month=9,
        lost_months=3,
        legal_costs=D("5000"),
        foreclosure_months=6,
        carry_monthly=loan.holding_costs_monthly,
        broker_pct=D("0.06"),
    )


# --- the timeline ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("term", "expected"), [(12, 9), (9, 6), (4, 1), (3, 1), (1, 1), (0, 1)])
def test_the_default_month_is_the_term_less_the_listing_period_floored_at_one(
    term: int, expected: int
) -> None:
    assert default_month(term, CONFIG) == expected


def test_the_default_month_refuses_a_negative_term() -> None:
    with pytest.raises(ValueError, match="negative"):
        default_month(-1, CONFIG)


def test_the_lender_flows_lay_the_default_out_month_by_month() -> None:
    loan = terms(inputs())
    timeline = wichita_timeline()
    assert (timeline.legal_month, timeline.sale_month) == (12, 18)
    months = lender_months(loan, timeline, D("200000"))
    assert list(months) == [0, *range(1, 10), 12, *range(13, 19)]
    assert months[0] == D("-150000") + D("1500")  # the advance and the fee's close half
    assert all(months[m] == D("1500") for m in range(1, 10))  # 12% on 150,000, nine months
    assert 10 not in months and 11 not in months  # the lost interest: nothing at all
    assert months[12] == D("-5000")
    carry = loan.holding_costs_monthly
    assert all(months[m] == -carry for m in range(13, 18))
    assert months[18] == -carry + D("200000") * D("0.94")  # the last carry and the sale
    flows = lender_flows(loan, timeline, D("200000"))
    assert [when for when, _ in flows][:3] == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
    ]
    assert flows[-1][0] == date(2028, 7, 31)


def test_a_hold_collects_the_net_rent_and_sells_at_its_end() -> None:
    loan = terms(inputs())
    timeline = wichita_timeline()
    months = lender_months(loan, timeline, D("200000"), hold_months=12, net_rent=D("1300"))
    assert months[18] == -loan.holding_costs_monthly  # no sale yet
    assert all(months[m] == D("1300") for m in range(19, 30))
    assert months[30] == D("1300") + D("200000") * D("0.94")


def test_a_holdbacks_draws_finish_by_the_default_month() -> None:
    """Denver: six $8,000 draws in months 1-6, which is the default month on a 9-month term."""
    denver = inputs(
        product="ACQUISITION_REHAB",
        rehab_costs="48000",
        loan_requested="195000",
        purchase_portion="147000",
        rehab_portion="48000",
        term_months=9,
        closing_date=date(2026, 10, 15),
    )
    loan = terms(denver)
    timeline = Timeline(6, 3, D("5000"), 4, loan.holding_costs_monthly, D("0.06"))
    months = lender_months(loan, timeline, D("250000"))
    interest = D("195000") * D("0.12") / 12
    assert all(months[m] == interest - D("8000") for m in range(1, 7))
    assert months[0] == D("-147000") + D("1950")
    assert list(months) == [0, 1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13]


# --- the solve ------------------------------------------------------------------------------------


def test_the_solve_finds_the_lowest_whole_dollar_that_clears_the_target() -> None:
    """A toy that rises a tenth of a percent per dollar: 200 is the first to reach 20%."""
    found = solve(lambda x: x / 1000, D(0), D(1000), TARGET)
    assert found.status is SolveStatus.SOLVED
    assert (found.value, found.irr, found.lower, found.upper) == (D(200), D("0.2"), D(0), D(1000))
    assert found.bound_hit is None
    # a function that never reaches it, and one that is over it from the start
    short = solve(lambda x: x / 100000, D(0), D(1000), TARGET)
    assert (short.status, short.bound_hit, short.value) == (
        SolveStatus.NO_SOLUTION,
        BoundHit.UPPER,
        None,
    )
    over = solve(lambda x: D(1), D(0), D(1000), TARGET)
    assert (over.status, over.bound_hit) == (SolveStatus.NO_SOLUTION, BoundHit.LOWER)
    assert over.lower == D(0) and over.upper == D(1000)
    # an empty range is no solution either, and None from the XIRR never clears
    assert solve(lambda x: D(1), D(5), D(5), TARGET).status is SolveStatus.NO_SOLUTION
    assert solve(lambda x: None, D(0), D(10), TARGET).status is SolveStatus.NO_SOLUTION


def test_a_solve_with_no_solution_says_which_bound_and_a_solved_one_says_neither() -> None:
    with pytest.raises(ValueError, match="which bound"):
        Solve(status=SolveStatus.NO_SOLUTION, value=None, irr=None, lower=D(0), upper=D(1))
    with pytest.raises(ValueError, match="which bound"):
        Solve(
            status=SolveStatus.SOLVED,
            value=D(1),
            irr=D("0.2"),
            lower=D(0),
            upper=D(1),
            bound_hit=BoundHit.UPPER,
        )
    with pytest.raises(ValueError, match="carries its bounds"):
        Solve(status=SolveStatus.SOLVED, value=D(1), irr=D("0.2"), lower=None, upper=None)
    assert NOT_SOLVED.status is SolveStatus.NOT_EVALUATED and NOT_SOLVED.lower is None


# --- the analysis ---------------------------------------------------------------------------------


def test_wichita_needs_about_198_thousand_and_the_dollar_below_it_falls_short() -> None:
    """Block A on the engine's own XIRR: P clears the target, P - 1 does not (SPEC §8.6)."""
    deal = inputs()
    loan = terms(deal)
    result = take_back_analysis(deal, loan, CONFIG)
    assert result.status is AnalysisStatus.EVALUATED
    assert (result.default_month, result.foreclosure_months, result.sale_month) == (9, 6, 18)
    assert result.interest_received == D("13500.00")
    assert result.lost_interest == D("4500.00")
    assert result.foreclosure_carry == loan.holding_costs_monthly * 6
    haircut = result.haircut_price
    assert haircut is not None and haircut.status is SolveStatus.SOLVED
    assert haircut.value == D(198132)
    assert (haircut.lower, haircut.upper) == (D(0), D("260000") * PRICE_BOUND_MULTIPLE)
    timeline = wichita_timeline()
    at = xirr(lender_flows(loan, timeline, haircut.value))
    below = xirr(lender_flows(loan, timeline, haircut.value - DOLLAR))
    assert at is not None and below is not None
    assert below < TARGET <= at
    assert abs(haircut.irr - at) < D("1e-9")  # the same root, from a different Newton start
    # the figures derived from P
    assert result.haircut_amount == D("260000") - D(198132)
    assert result.haircut_pct == (D("260000") - D(198132)) / D("260000")
    assert result.price_pct_of_purchase == D(198132) / D("200000")
    assert result.price_above_purchase is False
    assert result.as_is_value is None and result.price_vs_as_is is None
    # the flows recorded at P are the ones it was solved on
    recorded = [(flow.date, flow.amount) for flow in result.haircut_flows]
    assert recorded == lender_flows(loan, timeline, haircut.value)
    assert abs(xnpv(TARGET, recorded)) < D(1)


def test_wichita_against_a_float_xirr_of_this_modules_own() -> None:
    """The same solve with nothing shared: own month ends, own XIRR, own bisection."""
    closing = date(2027, 1, 1)

    def month_end(months_on: int) -> date:
        year, month = (
            closing.year + (closing.month - 1 + months_on) // 12,
            (closing.month - 1 + months_on) % 12 + 1,
        )
        return date(year, month, calendar.monthrange(year, month)[1])

    def flows_at(price: float) -> list[tuple[date, float]]:
        carry = 4000.0 / 12
        by_month = {0: -150000.0 + 1500.0}
        for m in range(1, 10):
            by_month[m] = 1500.0
        by_month[12] = -5000.0
        for m in range(13, 19):
            by_month[m] = by_month.get(m, 0.0) - carry
        by_month[18] += price * 0.94
        return [(month_end(m), amount) for m, amount in sorted(by_month.items())]

    def xirr_float(flows: list[tuple[date, float]]) -> float:
        start = flows[0][0]

        def npv(rate: float) -> float:
            return sum(a / (1 + rate) ** ((d - start).days / 365.0) for d, a in flows)

        lo, hi = -0.99, 10.0
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
        return (lo + hi) / 2

    short, clear = -1, 780000
    while clear - short > 1:
        mid = (short + clear) // 2
        if xirr_float(flows_at(float(mid))) >= 0.20:
            clear = mid
        else:
            short = mid
    engine = take_back_analysis(inputs(), terms(inputs()), CONFIG).haircut_price
    assert engine is not None and engine.value is not None
    assert abs(engine.value - D(clear)) <= 1


def test_the_hold_rows_solve_the_rent_and_the_price_for_each_config_period() -> None:
    deal = inputs()
    result = take_back_analysis(deal, terms(deal), CONFIG)
    assert [hold.hold_months for hold in result.holds] == [12, 24, 36]
    assert [hold.sale_month for hold in result.holds] == [30, 42, 54]
    twelve, twenty_four, thirty_six = result.holds
    # selling at the full 260,000 after a year clears 20% with no rent at all
    assert twelve.required_rent.status is SolveStatus.NO_SOLUTION
    assert twelve.required_rent.bound_hit is BoundHit.LOWER
    assert (twelve.required_rent.lower, twelve.required_rent.upper) == (
        D(0),
        D("2000") * RENT_BOUND_MULTIPLE,
    )
    assert twenty_four.required_rent.value == D(1274)
    assert thirty_six.required_rent.value == D(2506)
    # at the 2,000 market rent, the price the sale needs rises with every year held
    prices = [hold.required_sale_price.value for hold in result.holds]
    assert prices == [D(219693), D(245566), D(276613)]
    for hold in result.holds:
        assert hold.required_sale_price.upper == D("260000") * PRICE_BOUND_MULTIPLE
    # the market rent column is the market rent net of expenses, held for the period
    loan = terms(deal)
    timeline = wichita_timeline()
    net_rent = D("2000") * (1 - CONFIG.rental.expenses_pct_of_rent)
    for hold in result.holds:
        price = hold.required_sale_price.value
        assert price is not None
        at = xirr(lender_flows(loan, timeline, price, hold.hold_months, net_rent))
        below = xirr(lender_flows(loan, timeline, price - DOLLAR, hold.hold_months, net_rent))
        assert at is not None and below is not None and below < TARGET <= at


def test_without_a_market_rent_the_rent_bound_is_twenty_thousand_and_no_price_column() -> None:
    deal = inputs(monthly_rent=None)
    result = take_back_analysis(deal, terms(deal), CONFIG)
    assert result.status is AnalysisStatus.EVALUATED
    assert result.market_rent_status is AnalysisStatus.NOT_EVALUATED
    for hold in result.holds:
        assert hold.required_rent.upper == RENT_BOUND_USD
        assert hold.required_sale_price == NOT_SOLVED
    assert result.haircut_price is not None and result.haircut_price.value == D(198132)


def test_without_a_projected_sale_price_the_take_back_reports_the_timeline_only() -> None:
    deal = inputs(estimated_sale_price=None)
    result = take_back_analysis(deal, terms(deal), CONFIG)
    assert result.status is AnalysisStatus.NOT_EVALUATED
    assert result.market_rent_status is AnalysisStatus.NOT_EVALUATED
    assert result.haircut_price is None and result.haircut_amount is None
    assert not result.haircut_flows
    assert (result.default_month, result.sale_month) == (9, 18)
    assert result.lost_interest == D("4500.00") and result.legal_costs == D("5000.00")
    assert [hold.sale_month for hold in result.holds] == [30, 42, 54]
    assert all(hold.required_rent == NOT_SOLVED for hold in result.holds)


def test_a_state_outside_the_footprint_sells_on_the_other_entry() -> None:
    """No entry of its own, so config's OTHER (8 months) stands in: two more months of
    carry, a sale in month 20, and a higher price needed - checked on the engine's own XIRR
    over a timeline laid out here (SPEC §8.6, v0.20)."""
    deal = inputs().model_copy(update={"state": State.OTHER})
    loan = terms(deal)
    result = take_back_analysis(deal, loan, CONFIG)
    assert result.status is AnalysisStatus.EVALUATED
    assert (result.foreclosure_months, result.foreclosure_months_from) == (8, State.OTHER)
    assert (result.default_month, result.sale_month) == (9, 20)
    assert result.foreclosure_carry == loan.holding_costs_monthly * 8
    assert [hold.sale_month for hold in result.holds] == [32, 44, 56]
    haircut = result.haircut_price
    assert haircut is not None and haircut.status is SolveStatus.SOLVED
    assert haircut.value is not None
    timeline = wichita_timeline()._replace(foreclosure_months=8)
    at = xirr(lender_flows(loan, timeline, haircut.value))
    below = xirr(lender_flows(loan, timeline, haircut.value - DOLLAR))
    assert at is not None and below is not None and below < TARGET <= at
    # more carry, later sale: more needed than in Oklahoma, and over the purchase price
    oklahoma = take_back_analysis(inputs(), loan, CONFIG).haircut_price
    assert oklahoma is not None and oklahoma.value is not None
    assert haircut.value > oklahoma.value and result.price_above_purchase is True


def test_without_an_other_entry_such_a_state_has_no_timeline_to_sell_on() -> None:
    config = CONFIG.model_copy(
        update={
            "take_back": CONFIG.take_back.model_copy(
                update={"foreclosure_months": {State.OK: 6, State.CO: 4}}
            )
        }
    )
    deal = inputs().model_copy(update={"state": State.OTHER})
    result = take_back_analysis(deal, terms(deal), config)
    assert result.status is AnalysisStatus.NOT_EVALUATED
    assert result.foreclosure_months is None and result.foreclosure_months_from is None
    assert result.sale_month is None and result.foreclosure_carry is None
    assert result.projected_sale_price == D("260000")  # reported, just not solved against
    # a served state reads its own entry, and says so
    home = take_back_analysis(inputs(), terms(inputs()), config)
    assert (home.foreclosure_months, home.foreclosure_months_from) == (6, State.OK)


def test_the_haircut_is_set_beside_the_as_is_value_when_there_is_one() -> None:
    deal = inputs(as_is_value=D("180000"))
    result = take_back_analysis(deal, terms(deal), CONFIG)
    assert result.as_is_value == D("180000")
    assert result.price_vs_as_is == D(198132) - D("180000")
    assert result.price_pct_of_as_is == D(198132) / D("180000")


def test_the_deals_own_assumptions_move_the_timeline() -> None:
    """Four lost months, a $7,500 legal bill and a 5% broker: later sale, more needed."""
    base = take_back_analysis(inputs(), terms(inputs()), CONFIG)
    deal = inputs(
        take_back_lost_interest_months=4,
        take_back_legal_costs_usd=D("7500"),
        broker_selling_pct=D("0.05"),
    )
    own = take_back_analysis(deal, terms(deal), CONFIG)
    assert (own.lost_interest_months, own.legal_costs, own.broker_selling_pct) == (
        4,
        D("7500"),
        D("0.05"),
    )
    assert own.lost_interest == D("6000.00")
    assert own.sale_month == 19
    assert own.haircut_price is not None and base.haircut_price is not None
    assert own.haircut_price.value is not None and base.haircut_price.value is not None
    # a cheaper sale (5% broker) against a later, dearer one: the engine decides which wins
    assert own.haircut_price.value != base.haircut_price.value


def test_a_stored_take_back_from_before_2_1_0_reads_as_the_legacy_block() -> None:
    old = LegacyTakeBackAnalysis.model_validate(
        {
            "status": "NOT_EVALUATED",
            "loan_amount": "80000.00",
            "interest_rate": "0.12",
            "lost_interest_months": 3,
            "lost_interest": "2400.00",
            "legal_costs": "5000.00",
            "total_cost": "87400.00",
            "amortization_years": 30,
            "debt_service_monthly": "899.03",
            "dscr_floor": "1.00",
            "net_monthly_income": None,
            "dscr": None,
            "passed": None,
        }
    )
    assert old.total_cost == D("87400.00") and old.dscr is None


def test_the_fast_solve_lands_where_the_xirr_bisection_does() -> None:
    """``solve_flows`` probes the NPV at the target and confirms on the XIRR; on Wichita's
    block A it names the same dollar ``solve`` finds by bisecting the XIRR alone."""
    loan = terms(inputs())
    timeline = wichita_timeline()
    upper = D("260000") * PRICE_BOUND_MULTIPLE

    def flows_at(price: Decimal) -> list[tuple[date, Decimal]]:
        return lender_flows(loan, timeline, price)

    def irr_at(price: Decimal) -> Decimal | None:
        return xirr(flows_at(price))

    fast = solve_flows(flows_at, D(0), upper, TARGET)
    assert same_answer(fast, solve(irr_at, D(0), upper, TARGET))
    assert fast.value == D(198132)
    # the two bound cases agree too: a price range that never clears, and a hold whose
    # sale clears the target with no rent at all
    assert same_answer(
        solve_flows(flows_at, D(0), D(100000), TARGET), solve(irr_at, D(0), D(100000), TARGET)
    )

    def rent_flows(rent: Decimal) -> list[tuple[date, Decimal]]:
        return lender_flows(loan, timeline, D("260000"), 12, rent)

    assert same_answer(
        solve_flows(rent_flows, D(0), D(20000), TARGET),
        solve(lambda rent: xirr(rent_flows(rent)), D(0), D(20000), TARGET),
    )


def same_answer(a: Solve, b: Solve) -> bool:
    """The same status, bound, value and bounds; the XIRRs agree to a billionth."""
    if (a.status, a.bound_hit, a.value, a.lower, a.upper) != (
        b.status,
        b.bound_hit,
        b.value,
        b.lower,
        b.upper,
    ):
        return False
    if a.irr is None or b.irr is None:
        return a.irr is None and b.irr is None
    return abs(a.irr - b.irr) < D("1e-9")
