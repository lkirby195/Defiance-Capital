"""The v0.3 calc layer: the ledger, the three analyses, and the exit that toggles two of them.

# SPEC §8.3-§8.6

Every figure below is one a person can check: the commitments are round, the rates divide
into twelve, and the draw schedules divide into their rehab months. Where an arrangement does
not divide evenly the test says what the remainder does instead of rounding past it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from config.config import Config
from engine.calc.exit import exit_inference, flip_default, infer_exit, rental_default
from engine.calc.flip import financing_costs, flip_analysis
from engine.calc.ledger import (
    build_ledger,
    draw_schedule,
    drawn_by,
    interest_balance,
    return_overview,
)
from engine.calc.rental import (
    monthly_payment,
    net_monthly_income,
    rental_analysis,
    take_back_analysis,
)
from engine.calc.terms import (
    LoanTerms,
    holding_costs_total,
    loan_terms,
    origination_fee_pct,
    rehab_months,
)
from engine.sizing import size_deal
from schema.models import (
    AnalysisStatus,
    AssetType,
    BorrowerInputs,
    ExitSource,
    ExperienceBucket,
    ExperienceTier,
    Product,
    SizingInputs,
    StatedExit,
    Tranche,
    UnderwriteInputs,
)

CONFIG = Config.load()
D = Decimal
CLOSING = date(2027, 1, 1)

BORROWER = BorrowerInputs(
    credit_range_self_reported=Tranche.T1,
    experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
    repeat_borrower_self_reported=False,
)


def inputs(
    *,
    product: Product = Product.NO_DRAW,
    purchase_price: str = "200000",
    rehab_costs: str = "0",
    loan_requested: str = "150000",
    purchase_portion: str | None = None,
    rehab_portion: str | None = None,
    term_months: int = 12,
    interest_rate: str = "0.12",
    closing_date: date = CLOSING,
    monthly_rent: str | None = "2000",
    estimated_sale_price: str | None = "260000",
    contingency_pct: Decimal | None = None,
    closing_costs_usd: Decimal | None = None,
    **extra: object,
) -> UnderwriteInputs:
    """One deal, with every knob the §8 math reads and sensible round defaults."""
    return UnderwriteInputs(
        deal=SizingInputs(
            product=product,
            purchase_price=D(purchase_price),
            rehab_costs=D(rehab_costs),
            loan_requested=D(loan_requested),
            contingency_pct=contingency_pct,
            closing_costs_usd=closing_costs_usd,
            estimated_sale_price=(
                None if estimated_sale_price is None else D(estimated_sale_price)
            ),
            loan_purchase_portion=None if purchase_portion is None else D(purchase_portion),
            loan_rehab_portion=None if rehab_portion is None else D(rehab_portion),
        ),
        state="OK",  # type: ignore[arg-type]
        borrower=BORROWER,
        closing_date=closing_date,
        term_months=term_months,
        interest_rate=D(interest_rate),
        monthly_rent=None if monthly_rent is None else D(monthly_rent),
        **extra,  # type: ignore[arg-type]
    )


def terms(deal: UnderwriteInputs, config: Config = CONFIG) -> LoanTerms:
    sizing = size_deal(deal.deal, Tranche.T1, ExperienceTier.E3, config)
    return loan_terms(sizing, deal, config)


# --- the term facts (SPEC §8.1, §8.3) ------------------------------------------------------------


@pytest.mark.parametrize(("term", "expected"), [(12, 9), (9, 6), (4, 1), (3, 0), (2, 0), (1, 0)])
def test_rehab_months_is_the_term_less_the_listing_period(term: int, expected: int) -> None:
    assert rehab_months(term, CONFIG) == expected


def test_rehab_months_is_zero_on_a_term_with_no_whole_months() -> None:
    """A term inside its first month is all stub, and a stub is no part of a rehab period."""
    assert rehab_months(0, CONFIG) == 0
    with pytest.raises(ValueError, match="cannot be negative"):
        rehab_months(-1, CONFIG)


def test_the_origination_fee_falls_back_to_config_and_splits_in_half() -> None:
    loan = terms(inputs())
    assert loan.origination_fee_pct == CONFIG.fees.origination_default_pct
    # 2% of a 150,000 commitment is 3,000, half either end.
    assert loan.origination_at_close == D("1500.00")
    assert loan.origination_at_payoff == D("1500.00")
    assert origination_fee_pct(inputs(origination_fee_pct=D("0.03")), CONFIG) == D("0.03")


def test_the_holding_cost_is_a_percentage_of_price_plus_rehab() -> None:
    """2% of 200,000 + 50,000 is 5,000 over the hold, not per month.  # SPEC §8.1"""
    deal = inputs(rehab_costs="50000")
    assert holding_costs_total(deal, CONFIG) == D("5000.00")
    # The team's own percentage wins over the config default: 4.5% of 200,000 is 9,000.
    entered = inputs(holding_costs_pct_of_cost=D("0.045"))
    assert holding_costs_total(entered, CONFIG) == D("9000.00")
    assert terms(entered).holding_costs_monthly == D("750")  # 9,000 over 12 months
    # ...and it moves with the cost it is a percentage of, which a dollar figure would not.
    bigger = inputs(rehab_costs="50000", holding_costs_pct_of_cost=D("0.045"))
    assert holding_costs_total(bigger, CONFIG) == D("11250.00")


def test_a_deal_with_no_commitment_cannot_be_priced() -> None:
    """Every §8 figure divides by the commitment, so a zero is refused by name, not by
    ZeroDivisionError three modules later."""
    deal = inputs(
        product=Product.SPLIT_PRINCIPAL,
        rehab_costs="0",
        loan_requested="100000",
        purchase_portion="0",
        rehab_portion="100000",
    )
    with pytest.raises(ValueError, match="no commitment cannot be priced"):
        terms(deal)


# --- the draw schedule (SPEC §8.3) ---------------------------------------------------------------


def test_the_rehab_portion_is_drawn_straight_line_over_the_rehab_period() -> None:
    loan = terms(
        inputs(
            product=Product.SPLIT_PRINCIPAL,
            rehab_costs="54000",
            loan_requested="190000",
            purchase_portion="136000",
            rehab_portion="54000",
        )
    )
    schedule = draw_schedule(loan)
    assert loan.rehab_months == 9
    assert list(schedule) == list(range(1, 10))
    assert set(schedule.values()) == {D("6000.00")}
    assert sum(schedule.values()) == D("54000.00")


def test_an_uneven_draw_puts_the_remainder_in_the_last_month() -> None:
    """50,000 over six months is 8,333.33 with two cents left; the last draw carries them.

    The alternative is a schedule that funds 49,999.98 of a 50,000 portion, and a payoff row
    that is not the commitment.
    """
    loan = terms(
        inputs(
            product=Product.SPLIT_DRAW,
            rehab_costs="50000",
            loan_requested="200000",
            purchase_portion="150000",
            rehab_portion="50000",
            term_months=9,
        )
    )
    schedule = draw_schedule(loan)
    assert [schedule[m] for m in range(1, 7)] == [D("8333.33")] * 5 + [D("8333.35")]
    assert sum(schedule.values()) == D("50000")


def test_no_rehab_period_means_no_draws_and_the_money_goes_out_at_close() -> None:
    loan = terms(
        inputs(
            product=Product.SPLIT_PRINCIPAL,
            rehab_costs="30000",
            loan_requested="110000",
            purchase_portion="85000",
            rehab_portion="25000",
            term_months=3,
        )
    )
    assert loan.rehab_months == 0
    assert draw_schedule(loan) == {}
    assert loan.sizing.funded_at_close == D("85000")  # what the sizing says
    assert loan.funded_at_close == D("110000")  # what actually leaves on the day
    # ...and interest runs on all of it from month 1, not on the Principal Note alone.
    assert interest_balance(loan, {}, 1) == D("110000")


def test_a_product_with_no_rehab_portion_draws_nothing() -> None:
    loan = terms(inputs(product=Product.NO_DRAW))
    assert loan.rehab_portion == 0
    assert draw_schedule(loan) == {}
    assert loan.funded_at_close == loan.commitment


# --- the interest balance (SPEC §3, §8.3) --------------------------------------------------------


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL])
def test_a_single_note_accrues_on_the_whole_commitment(product: Product) -> None:
    loan = terms(inputs(product=product))
    for month in range(1, loan.term_months + 1):
        assert interest_balance(loan, {}, month) == D("150000")


def test_split_draw_accrues_on_the_full_commitment_though_the_holdback_is_not_out() -> None:
    """The product, not an approximation: SPLIT_DRAW pays on money it has not received."""
    loan = terms(
        inputs(
            product=Product.SPLIT_DRAW,
            rehab_costs="48000",
            loan_requested="195000",
            purchase_portion="147000",
            rehab_portion="48000",
            term_months=9,
        )
    )
    schedule = draw_schedule(loan)
    assert loan.funded_at_close == D("147000")
    assert all(interest_balance(loan, schedule, m) == D("195000") for m in range(1, 10))


def test_split_principal_accrues_on_the_balance_standing_at_the_start_of_the_month() -> None:
    """A draw taken in month k first earns in month k + 1."""
    loan = terms(
        inputs(
            product=Product.SPLIT_PRINCIPAL,
            rehab_costs="54000",
            loan_requested="190000",
            purchase_portion="136000",
            rehab_portion="54000",
        )
    )
    schedule = draw_schedule(loan)
    assert interest_balance(loan, schedule, 1) == D("136000")  # nothing drawn yet
    assert interest_balance(loan, schedule, 2) == D("142000")  # one draw in
    assert interest_balance(loan, schedule, 10) == D("190000")  # fully drawn
    assert interest_balance(loan, schedule, 12) == D("190000")
    assert drawn_by(schedule, 0) == 0
    assert drawn_by(schedule, 9) == D("54000.00")


# --- the ledger (SPEC §8.3) ----------------------------------------------------------------------


def test_the_ledger_has_a_row_for_every_month_from_closing_to_payoff() -> None:
    loan = terms(inputs(term_months=12))
    entries = build_ledger(loan)
    assert [e.month for e in entries] == list(range(13))
    assert entries[0].date == CLOSING
    assert entries[-1].date == date(2028, 1, 1)
    assert loan.payoff_date == date(2028, 1, 1)


def test_month_zero_is_the_money_out_and_the_close_half_of_the_fee() -> None:
    entries = build_ledger(terms(inputs()))
    first = entries[0]
    assert first.funding == D("-150000")
    assert first.draws == 0
    assert first.interest == 0
    assert first.fees == D("1500.00")
    assert first.payoff == 0
    assert first.net == D("-148500.00")


def test_the_payoff_month_returns_the_principal_and_the_payoff_half() -> None:
    entries = build_ledger(terms(inputs()))
    last = entries[-1]
    assert last.payoff == D("150000")
    assert last.interest == D("1500.00")
    assert last.fees == D("1500.00")
    assert last.net == D("153000.00")


def test_every_dollar_funded_comes_back_in_the_payoff() -> None:
    """The identity that makes total_profit equal interest + fees.  # SPEC §8.3"""
    loan = terms(
        inputs(
            product=Product.SPLIT_DRAW,
            rehab_costs="50000",
            loan_requested="200000",
            purchase_portion="150000",
            rehab_portion="50000",
            term_months=9,
        )
    )
    overview = return_overview(loan)
    assert -(overview.total_funding + overview.total_draws) == overview.total_payoff
    assert overview.total_payoff == loan.commitment
    assert overview.total_profit == overview.total_interest + overview.total_fees


def test_the_totals_are_the_columns_added_up() -> None:
    overview = return_overview(terms(inputs()))
    assert overview.total_interest == D("18000.00")  # 12 x 1,500
    assert overview.total_fees == D("3000.00")  # 2% of 150,000
    assert overview.total_profit == D("21000.00")
    assert overview.irr is not None
    assert D("0.149") < overview.irr < D("0.150")


def test_the_irr_is_the_rate_that_zeroes_the_net_column() -> None:
    from engine.calc.irr import NPV_TOLERANCE, xnpv

    overview = return_overview(terms(inputs()))
    assert overview.irr is not None
    assert abs(xnpv(overview.irr, [(e.date, e.net) for e in overview.entries])) < NPV_TOLERANCE


def test_an_earlier_payoff_at_the_same_rate_earns_a_higher_irr() -> None:
    """The fees are the same either way, so a shorter term spreads them over less time."""
    short = return_overview(terms(inputs(term_months=6)))
    long = return_overview(terms(inputs(term_months=24)))
    assert short.irr is not None and long.irr is not None
    assert short.irr > long.irr


# --- flip (SPEC §8.4) ----------------------------------------------------------------------------


def flip_for(deal: UnderwriteInputs, on: bool = True) -> object:
    loan = terms(deal)
    return flip_analysis(deal, loan, return_overview(loan), on, CONFIG)


def test_the_flip_nets_the_sale_price_of_the_broker_and_the_whole_cost_stack() -> None:
    deal = inputs()
    loan = terms(deal)
    overview = return_overview(loan)
    flip = flip_analysis(deal, loan, overview, True, CONFIG)
    assert flip.status is AnalysisStatus.EVALUATED
    # 200,000 price + 1,000 closing + 4,000 holding + 0 rehab + 0 contingency + 21,000 financing
    assert flip.total_costs == D("226000.00")
    assert flip.financing_costs == D("21000.00") == financing_costs(overview)
    assert flip.broker_costs == D("10400.00")  # 4% of 260,000
    assert flip.net_profit == D("23600.00")
    assert flip.profit_yield == D("23600.00") / D("226000.00")


def test_the_broker_cut_comes_off_the_price_and_is_not_a_project_cost() -> None:
    """Deliberate (SPEC §8.4): it is what selling costs, not what the project costs."""
    flip = flip_for(inputs())
    assert flip.broker_costs not in (None,)
    assert flip.total_costs == D("226000.00")  # the broker's 10,400 is not in it


def test_a_flip_that_is_off_still_reports_the_cost_stack() -> None:
    flip = flip_for(inputs(), on=False)
    assert flip.status is AnalysisStatus.OFF
    assert flip.total_costs == D("226000.00")
    assert flip.estimated_sale_price is None
    assert flip.net_profit is None and flip.profit_yield is None


def test_a_flip_with_no_sale_price_is_not_evaluated() -> None:
    flip = flip_for(inputs(estimated_sale_price=None))
    assert flip.status is AnalysisStatus.NOT_EVALUATED
    assert flip.net_profit is None
    assert flip.total_costs == D("226000.00")


def test_the_contingency_is_a_flip_cost_of_its_own() -> None:
    """rehab_costs and the contingency on them are two lines, not one (SPEC §8.4)."""
    plain = flip_for(inputs(rehab_costs="50000"))
    assert plain.rehab_costs == D("50000")
    assert plain.contingency == 0  # the config default is 0.00

    padded = inputs(rehab_costs="50000", contingency_pct=D("0.10"))
    flip = flip_for(padded)
    assert flip.rehab_costs == D("50000")
    assert flip.contingency == D("5000.00")


# --- rental and take-back (SPEC §8.5, §8.6) ------------------------------------------------------


def test_a_level_payment_amortizes_the_loan() -> None:
    """A 30-year 6.5% loan of 150,000 costs about $948 a month; the zero-rate case is P/n."""
    payment = monthly_payment(D("150000"), D("0.065"), 30)
    assert D("948.10") < payment < D("948.11")
    assert monthly_payment(D("360000"), D("0"), 30) == D("1000")
    with pytest.raises(ValueError, match="at least 1"):
        monthly_payment(D("100"), D("0.05"), 0)


def test_net_monthly_income_is_rent_less_expenses_less_the_monthly_carry() -> None:
    deal = inputs(monthly_rent="2000", holding_costs_pct_of_cost=D("0.024"))
    loan = terms(deal)
    # 2,000 - 35% of 2,000 - 4,800/12 = 2,000 - 700 - 400
    assert net_monthly_income(D("2000"), loan, CONFIG) == D("900.00")


def test_the_rental_dscr_is_income_over_a_takeout_payment_on_the_commitment() -> None:
    deal = inputs(monthly_rent="2000", holding_costs_pct_of_cost=D("0.024"))
    loan = terms(deal)
    rental = rental_analysis(deal, loan, True, CONFIG)
    assert rental.status is AnalysisStatus.EVALUATED
    assert rental.loan_amount == D("150000")
    assert rental.takeout_rate == CONFIG.rental.takeout_rate
    assert rental.expenses == D("700.00")
    assert rental.net_monthly_income == D("900.00")
    assert rental.dscr == D("900.00") / rental.debt_service_monthly
    assert rental.passed is False  # 0.95x, under the 1.20 floor


def test_a_rental_that_clears_the_floor_passes() -> None:
    deal = inputs(monthly_rent="6000", holding_costs_pct_of_cost=D("0.024"))
    rental = rental_analysis(deal, terms(deal), True, CONFIG)
    assert rental.dscr is not None and rental.dscr > CONFIG.rental.dscr_floor
    assert rental.passed is True


def test_a_rental_that_is_off_still_reports_the_debt_service() -> None:
    deal = inputs()
    rental = rental_analysis(deal, terms(deal), False, CONFIG)
    assert rental.status is AnalysisStatus.OFF
    assert rental.debt_service_monthly > 0
    assert rental.monthly_rent is None and rental.dscr is None and rental.passed is None


def test_a_rental_with_no_rent_is_not_evaluated() -> None:
    deal = inputs(monthly_rent=None)
    rental = rental_analysis(deal, terms(deal), True, CONFIG)
    assert rental.status is AnalysisStatus.NOT_EVALUATED
    assert rental.dscr is None and rental.passed is None


def test_the_take_back_costs_the_loan_plus_lost_interest_plus_the_legal_bill() -> None:
    deal = inputs(monthly_rent="2000", holding_costs_pct_of_cost=D("0.024"))
    loan = terms(deal)
    take_back = take_back_analysis(deal, loan, CONFIG)
    assert take_back.loan_amount == D("150000")
    # three months of 12% on 150,000 is 4,500
    assert take_back.lost_interest == D("4500.00")
    assert take_back.legal_costs == D("5000.00")
    assert take_back.total_cost == D("159500.00")
    assert take_back.interest_rate == D("0.12")  # the deal's own rate, not a takeout rate
    assert take_back.dscr == D("900.00") / take_back.debt_service_monthly


def test_the_take_back_runs_whatever_the_rental_toggle_says() -> None:
    """It is not a toggle: a flip deal with a rent on it still gets one.  # SPEC §8.6"""
    deal = inputs(rental_analysis=False, monthly_rent="2000")
    loan = terms(deal)
    assert rental_analysis(deal, loan, False, CONFIG).status is AnalysisStatus.OFF
    assert take_back_analysis(deal, loan, CONFIG).status is AnalysisStatus.EVALUATED


def test_a_take_back_with_no_rent_is_not_evaluated_but_still_costs_what_it_costs() -> None:
    deal = inputs(monthly_rent=None)
    take_back = take_back_analysis(deal, terms(deal), CONFIG)
    assert take_back.status is AnalysisStatus.NOT_EVALUATED
    assert take_back.total_cost == D("159500.00")
    assert take_back.debt_service_monthly > 0
    assert take_back.net_monthly_income is None and take_back.dscr is None


def test_both_analyses_share_one_net_monthly_income() -> None:
    deal = inputs(monthly_rent="2000", holding_costs_pct_of_cost=D("0.024"))
    loan = terms(deal)
    rental = rental_analysis(deal, loan, True, CONFIG)
    take_back = take_back_analysis(deal, loan, CONFIG)
    assert rental.net_monthly_income == take_back.net_monthly_income


# --- the stub period: a payoff date between two anchors (SPEC §8.1, §8.3) -----------------------


def stub_deal(**extra: object) -> UnderwriteInputs:
    """150,000 at 12% for nine months and eleven days, closing on the 15th."""
    return inputs(closing_date=date(2027, 3, 15), term_months=9, term_stub_days=11, **extra)


def test_the_term_carries_its_stub_and_the_payoff_date_lands_on_it() -> None:
    loan = terms(stub_deal())
    assert (loan.term_months, loan.stub_days) == (9, 11)
    assert loan.has_stub
    assert loan.payoff_date == date(2027, 12, 26)
    assert loan.term_description == "9 month(s) and 11 day(s)"
    # The stub is eleven thirtieths of a period on the config day-count basis.
    assert loan.day_count_basis == CONFIG.interest.day_count_basis == 30
    assert loan.term_months_decimal == D(9) + D(11) / D(30)


def test_a_whole_month_term_has_no_stub_and_an_integer_term_in_months() -> None:
    loan = terms(inputs())
    assert loan.stub_days == 0 and not loan.has_stub
    assert loan.term_months_decimal == D(12)
    assert loan.stub_interest(D("150000")) == D(0)


def test_the_stub_accrues_one_months_interest_prorated_by_its_days() -> None:
    """12% of 150,000 is 1,500 a month; eleven days of it is 550.  # SPEC §8.3"""
    loan = terms(stub_deal())
    assert loan.monthly_interest(D("150000")) == D("1500")
    assert loan.stub_interest(D("150000")) == D("1500") * D(11) / D(30) == D("550")


def test_the_ledger_ends_on_a_short_row_dated_the_payoff_date() -> None:
    loan = terms(stub_deal())
    entries = build_ledger(loan)
    # Ten anchors (months 0..9) plus the stub.
    assert len(entries) == 11
    assert [entry.month for entry in entries] == list(range(11))
    assert entries[9].date == date(2027, 12, 15) and entries[9].stub_days == 0
    last = entries[-1]
    assert last.date == date(2027, 12, 26) and last.stub_days == 11
    assert last.interest == D("550")
    # The payoff and the fee's payoff half land on the stub row, not on the anchor before it.
    assert last.payoff == D("150000") and last.fees == D("1500")
    assert entries[9].payoff == D(0) and entries[9].fees == D(0)


def test_the_stub_does_not_lengthen_the_rehab_period_or_the_draw_schedule() -> None:
    """Whole months only: a stub is days at the end of the term, after the listing period."""
    loan = terms(
        stub_deal(
            product=Product.SPLIT_PRINCIPAL,
            rehab_costs="60000",
            loan_requested="240000",
            purchase_portion="180000",
            rehab_portion="60000",
            estimated_sale_price="400000",
        )
    )
    assert loan.rehab_months == 6  # 9 whole months less the 3 listing months
    schedule = draw_schedule(loan)
    assert sorted(schedule) == [1, 2, 3, 4, 5, 6]
    assert sum(schedule.values()) == D("60000")


def test_the_monthly_holding_cost_is_spread_over_the_stub_as_well() -> None:
    """A hold that runs eleven days longer carries eleven days more cost.  # SPEC §8.1"""
    whole = terms(inputs(term_months=9, holding_costs_pct_of_cost=D("0.05")))
    stubbed = terms(stub_deal(holding_costs_pct_of_cost=D("0.05")))
    assert whole.holding_costs_total == stubbed.holding_costs_total == D("10000.00")
    assert whole.holding_costs_monthly == D("10000") / D(9)
    assert stubbed.holding_costs_monthly == D("10000") / (D(9) + D(11) / D(30))
    assert stubbed.holding_costs_monthly < whole.holding_costs_monthly


def test_a_term_inside_its_first_month_is_all_stub_and_still_prices() -> None:
    loan = terms(inputs(term_months=0, term_stub_days=20))
    assert loan.rehab_months == 0
    entries = build_ledger(loan)
    assert len(entries) == 2
    assert entries[1].date == date(2027, 1, 21) and entries[1].stub_days == 20
    assert entries[1].interest == D("1500") * D(20) / D(30) == D("1000")
    assert entries[1].payoff == D("150000")


def test_a_term_of_no_time_at_all_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="no months and no days"):
        UnderwriteInputs.model_validate(
            inputs().model_dump() | {"term_months": 0, "term_stub_days": 0}
        )


def test_the_stub_rows_interest_keeps_the_ledgers_two_identities() -> None:
    overview = return_overview(terms(stub_deal()))
    assert overview.total_profit == overview.total_interest + overview.total_fees
    assert -(overview.total_funding + overview.total_draws) == overview.total_payoff


# --- the exit inference and the toggles it defaults (SPEC §3, §8.1) ------------------------------


@pytest.mark.parametrize(
    ("stated", "asset", "term", "product", "expected", "source"),
    [
        (StatedExit.HOLD, AssetType.SFR, 6, Product.NO_DRAW, StatedExit.HOLD, ExitSource.STATED),
        (
            StatedExit.UNKNOWN,
            AssetType.SFR,
            6,
            Product.NO_DRAW,
            StatedExit.FLIP,
            ExitSource.INFERRED,
        ),
        (
            StatedExit.UNKNOWN,
            AssetType.SFR,
            6,
            Product.WHOLETAIL,
            StatedExit.WHOLETAIL,
            ExitSource.INFERRED,
        ),
        (
            StatedExit.UNKNOWN,
            AssetType.SFR,
            12,
            Product.NO_DRAW,
            StatedExit.HOLD,
            ExitSource.INFERRED,
        ),
        (
            StatedExit.UNKNOWN,
            AssetType.SFR,
            10,
            Product.NO_DRAW,
            StatedExit.UNKNOWN,
            ExitSource.INFERRED,
        ),
        (
            StatedExit.UNKNOWN,
            AssetType.UNITS_5_PLUS,
            6,
            Product.NO_DRAW,
            StatedExit.UNKNOWN,
            ExitSource.INFERRED,
        ),
        (StatedExit.UNKNOWN, None, 6, Product.NO_DRAW, StatedExit.UNKNOWN, ExitSource.INFERRED),
    ],
)
def test_the_exit_is_stated_or_inferred_from_term_and_asset_type(
    stated: StatedExit,
    asset: AssetType | None,
    term: int,
    product: Product,
    expected: StatedExit,
    source: ExitSource,
) -> None:
    assert infer_exit(stated, asset, term, product, CONFIG) == (expected, source)


def test_a_resale_exit_defaults_the_flip_on_and_a_hold_defaults_the_rental_on() -> None:
    assert flip_default(StatedExit.FLIP) is True
    assert flip_default(StatedExit.WHOLETAIL) is True
    assert flip_default(StatedExit.HOLD) is False
    assert flip_default(StatedExit.UNKNOWN) is False
    assert rental_default(StatedExit.HOLD, None) is True
    assert rental_default(StatedExit.FLIP, None) is False
    # a rent the team went and looked up turns the rental on whatever the exit says
    assert rental_default(StatedExit.FLIP, D("2000")) is True


def test_a_toggle_set_by_hand_wins_over_the_exit_in_either_direction() -> None:
    hold = inputs(term_months=12, asset_type=AssetType.SFR, monthly_rent=None)
    derived = exit_inference(hold, CONFIG)
    assert derived.type is StatedExit.HOLD
    assert (derived.flip_analysis, derived.rental_analysis) == (False, True)
    assert (derived.flip_analysis_default, derived.rental_analysis_default) == (False, True)

    forced = exit_inference(
        inputs(
            term_months=12,
            asset_type=AssetType.SFR,
            monthly_rent=None,
            flip_analysis=True,
            rental_analysis=False,
        ),
        CONFIG,
    )
    assert (forced.flip_analysis, forced.rental_analysis) == (True, False)
    # the defaults are still reported, so a reader can see what was overridden
    assert (forced.flip_analysis_default, forced.rental_analysis_default) == (False, True)
