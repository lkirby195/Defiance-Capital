"""The calc layer: the ledger, the three analyses, and Sources & Uses.

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
from engine.calc.flip import cash_on_cash, financing_costs, flip_analysis
from engine.calc.ledger import (
    build_ledger,
    draw_schedule,
    drawn_by,
    interest_balance,
    return_overview,
)
from engine.calc.rental import (
    exit_value,
    interest_only_payment,
    monthly_payment,
    payoff_due,
    rental_analysis,
    required_rent,
)
from engine.calc.sources_uses import sources_and_uses
from engine.calc.take_back import take_back_analysis
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
    BorrowerInputs,
    ExitValueSource,
    ExperienceBucket,
    ExperienceTier,
    FlipAnalysis,
    Product,
    SizingInputs,
    SourcesAndUses,
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
    product: Product = Product.ACQUISITION,
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
    ZeroDivisionError three modules later. Both structures lend the request, which is never
    zero, so the guard is reached here with a sizing made to say so."""
    deal = inputs()
    sizing = size_deal(deal.deal, Tranche.T1, ExperienceTier.E3, CONFIG)
    with pytest.raises(ValueError, match="no commitment cannot be priced"):
        loan_terms(sizing.model_copy(update={"commitment": D(0)}), deal, CONFIG)


# --- the draw schedule (SPEC §8.3) ---------------------------------------------------------------


def test_the_rehab_portion_is_drawn_straight_line_over_the_rehab_period() -> None:
    loan = terms(
        inputs(
            product=Product.ACQUISITION_REHAB,
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
    assert drawn_by(schedule, 0) == 0
    assert drawn_by(schedule, 9) == D("54000.00")


def test_an_uneven_draw_puts_the_remainder_in_the_last_month() -> None:
    """50,000 over six months is 8,333.33 with two cents left; the last draw carries them.

    The alternative is a schedule that funds 49,999.98 of a 50,000 portion, and a payoff row
    that is not the commitment.
    """
    loan = terms(
        inputs(
            product=Product.ACQUISITION_REHAB,
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
            product=Product.ACQUISITION_REHAB,
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
    assert interest_balance(loan) == D("110000")


def test_a_product_with_no_rehab_portion_draws_nothing() -> None:
    loan = terms(inputs(product=Product.ACQUISITION))
    assert loan.rehab_portion == 0
    assert draw_schedule(loan) == {}
    assert loan.funded_at_close == loan.commitment


# --- the interest balance (SPEC §3, §8.3) --------------------------------------------------------


def test_100_pct_at_close_accrues_on_the_whole_commitment() -> None:
    loan = terms(inputs(product=Product.ACQUISITION))
    assert interest_balance(loan) == D("150000")
    entries = build_ledger(loan)
    assert {entry.interest for entry in entries[1:]} == {D("1500.00")}


def test_a_holdback_accrues_on_the_full_commitment_though_it_is_not_all_out() -> None:
    """The structure, not an approximation: a holdback pays on money not yet drawn, from
    month 1 to the payoff, the same as 100% at close (SPEC §3)."""
    loan = terms(
        inputs(
            product=Product.ACQUISITION_REHAB,
            rehab_costs="54000",
            loan_requested="190000",
            purchase_portion="136000",
            rehab_portion="54000",
        )
    )
    entries = build_ledger(loan)
    assert loan.funded_at_close == D("136000")
    assert interest_balance(loan) == D("190000")
    # 190,000 at 12% is 1,900 a month in every month, the first one included
    assert {entry.interest for entry in entries[1:]} == {D("1900.00")}
    assert return_overview(loan).total_interest == D("22800.00")


# --- the ledger (SPEC §8.3) ----------------------------------------------------------------------


def test_the_ledger_has_a_row_for_every_month_from_closing_to_payoff() -> None:
    loan = terms(inputs(term_months=12))
    entries = build_ledger(loan)
    assert [e.month for e in entries] == list(range(13))
    # month 0 is the end of the closing month, not the closing date (SPEC §8.3)
    assert entries[0].date == date(2027, 1, 31)
    assert entries[-1].date == date(2028, 1, 31)
    assert loan.payoff_date == date(2028, 1, 31)
    assert loan.closing_date == CLOSING


def test_the_ledger_is_the_same_whichever_day_of_the_month_the_deal_closes() -> None:
    """Closing on the 1st and on the 15th lay out the same month-end rows (SPEC §8.3)."""
    first = build_ledger(terms(inputs(closing_date=date(2026, 10, 1), term_months=9)))
    fifteenth = build_ledger(terms(inputs(closing_date=date(2026, 10, 15), term_months=9)))
    assert [e.date for e in first] == [e.date for e in fifteenth]
    assert first[0].date == date(2026, 10, 31) and first[-1].date == date(2027, 7, 31)
    assert return_overview(terms(inputs(closing_date=date(2026, 10, 1), term_months=9))).irr == (
        return_overview(terms(inputs(closing_date=date(2026, 10, 15), term_months=9))).irr
    )


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
            product=Product.ACQUISITION_REHAB,
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


def flip_for(deal: UnderwriteInputs) -> FlipAnalysis:
    loan = terms(deal)
    return flip_analysis(deal, loan, return_overview(loan), CONFIG)


def test_the_flip_nets_the_sale_price_of_the_broker_and_the_whole_cost_stack() -> None:
    deal = inputs()
    loan = terms(deal)
    overview = return_overview(loan)
    flip = flip_analysis(deal, loan, overview, CONFIG)
    assert flip.status is AnalysisStatus.EVALUATED
    # 200,000 price + 1,000 closing + 0 rehab + 0 contingency + 4,000 holding + 21,000 financing
    assert flip.total_costs == D("226000.00")
    assert flip.financing_costs == D("21000.00") == financing_costs(overview)
    assert flip.broker_costs == D("15600.00")  # 6% of 260,000
    assert flip.net_profit == D("18400.00")
    assert flip.project_yield == D("18400.00") / D("226000.00")


def test_the_financing_is_reported_as_its_interest_and_its_origination() -> None:
    """The profit line by line: 18,000 of interest and 3,000 of fees, not one lump."""
    flip = flip_for(inputs())
    assert flip.interest == D("18000.00")
    assert flip.origination == D("3000.00")
    assert flip.interest + flip.origination == flip.financing_costs
    lines = (
        flip.purchase_price,
        flip.closing_costs,
        flip.rehab_costs,
        flip.contingency,
        flip.holding_costs_total,
        flip.interest,
        flip.origination,
    )
    assert sum(lines, D(0)) == flip.total_costs
    assert flip.estimated_sale_price is not None and flip.broker_costs is not None
    assert flip.estimated_sale_price - flip.broker_costs - sum(lines, D(0)) == flip.net_profit


def test_the_borrower_equity_is_the_total_deal_cost_less_the_loan() -> None:
    """226,000 of cost on a 150,000 loan: the borrower brings 76,000.  # SPEC §8.4"""
    flip = flip_for(inputs())
    assert flip.loan_amount == D("150000")
    assert flip.borrower_equity == D("76000.00")
    # 18,400 on 76,000 is 24.2%, over the 20% target
    assert flip.cash_on_cash == D("18400.00") / D("76000.00")
    assert flip.cash_on_cash_target == CONFIG.pricing.borrower_coc_target == D("0.20")
    assert flip.cash_on_cash_passed is True


def test_a_cash_on_cash_under_the_target_fails_it() -> None:
    """A 230,000 sale nets -9,800 on the same 76,000 of equity: under target, not passed."""
    flip = flip_for(inputs(estimated_sale_price="230000"))
    assert flip.net_profit == D("230000") - D("13800.00") - D("226000.00")
    assert flip.cash_on_cash is not None and flip.cash_on_cash < 0
    assert flip.cash_on_cash_passed is False


def test_a_loan_that_covers_every_cost_has_no_cash_on_cash() -> None:
    assert cash_on_cash(D("1000"), D("0")) is None
    assert cash_on_cash(D("1000"), D("-5")) is None
    assert cash_on_cash(D("1000"), D("4000")) == D("0.25")


def test_the_broker_cut_comes_off_the_price_and_is_not_a_project_cost() -> None:
    """Deliberate (SPEC §8.4): it is what selling costs, not what the project costs."""
    flip = flip_for(inputs())
    assert flip.broker_costs not in (None,)
    assert flip.total_costs == D("226000.00")  # the broker's 15,600 is not in it


def test_a_flip_with_no_sale_price_is_not_evaluated_and_keeps_the_cost_stack() -> None:
    flip = flip_for(inputs(estimated_sale_price=None))
    assert flip.status is AnalysisStatus.NOT_EVALUATED
    assert flip.net_profit is None and flip.project_yield is None
    assert flip.cash_on_cash is None and flip.cash_on_cash_passed is None
    assert flip.total_costs == D("226000.00")
    # the equity needs no sale price
    assert flip.borrower_equity == D("76000.00")


def test_a_stored_flip_reads_its_old_profit_yield_as_the_project_yield() -> None:
    """A result stored before 2.0.0 named it ``profit_yield``; it still loads."""
    flip = flip_for(inputs())
    stored = flip.model_dump(mode="json", exclude={"project_yield"})
    stored["profit_yield"] = str(flip.project_yield)
    for name in ("interest", "origination", "loan_amount", "borrower_equity", "cash_on_cash"):
        stored.pop(name)
    old = FlipAnalysis.model_validate(stored)
    assert old.project_yield == flip.project_yield
    assert old.borrower_equity is None


def test_the_contingency_is_a_flip_cost_of_its_own() -> None:
    """rehab_costs and the contingency on them are two lines, not one (SPEC §8.4)."""
    plain = flip_for(inputs(rehab_costs="50000"))
    assert plain.rehab_costs == D("50000")
    assert plain.contingency == 0  # the config default is 0.00

    padded = inputs(rehab_costs="50000", contingency_pct=D("0.10"))
    flip = flip_for(padded)
    assert flip.rehab_costs == D("50000")
    assert flip.contingency == D("5000.00")


# --- Sources & Uses (SPEC §8.11) -----------------------------------------------------------------


def holdback_deal(term_months: int = 9) -> UnderwriteInputs:
    return inputs(
        product=Product.ACQUISITION_REHAB,
        rehab_costs="48000",
        loan_requested="195000",
        purchase_portion="147000",
        rehab_portion="48000",
        term_months=term_months,
    )


def table_for(deal: UnderwriteInputs) -> SourcesAndUses:
    loan = terms(deal)
    overview = return_overview(loan)
    return sources_and_uses(loan, flip_analysis(deal, loan, overview, CONFIG))


def test_the_sources_are_the_two_advances_and_the_equity() -> None:
    table = table_for(holdback_deal())
    assert table.advance_at_closing == D("147000")
    assert table.rehab_holdback == D("48000")
    assert table.advance_at_closing + table.rehab_holdback == D("195000")  # the loan
    assert table.total_sources == table.total_uses


@pytest.mark.parametrize(
    "deal",
    [
        inputs(),
        inputs(estimated_sale_price=None, monthly_rent=None),
        inputs(rehab_costs="50000", contingency_pct=D("0.10"), origination_fee_pct=D("0.03")),
        inputs(closing_date=date(2027, 3, 15), term_months=9, term_stub_days=11),
        inputs(interest_rate="0.1634", loan_requested="143000"),
    ],
    ids=["acquisition", "no-price-no-rent", "contingency-and-fee", "stub", "odd-cents"],
)
def test_sources_equal_uses_to_the_cent(deal: UnderwriteInputs) -> None:
    table = table_for(deal)
    sources = table.advance_at_closing + table.rehab_holdback + table.borrower_equity
    uses = (
        table.purchase_price
        + table.closing_costs
        + table.rehab_costs
        + table.contingency
        + table.holding_costs
        + table.interest
        + table.origination
    )
    cents = D("0.01")
    assert sources.quantize(cents) == uses.quantize(cents)
    assert table.total_sources.quantize(cents) == table.total_uses.quantize(cents)


def test_a_holdback_with_no_rehab_period_holds_nothing_back() -> None:
    """The rehab money goes out at closing (SPEC §8.3), so the advance is the whole loan."""
    table = table_for(holdback_deal(term_months=3))
    assert table.advance_at_closing == D("195000")
    assert table.rehab_holdback == 0


def test_100_pct_at_close_has_no_holdback_and_the_same_uses_as_the_flip() -> None:
    deal = inputs()
    table = table_for(deal)
    flip = flip_for(deal)
    assert table.rehab_holdback == 0
    assert table.advance_at_closing == D("150000")
    assert table.borrower_equity == flip.borrower_equity == D("76000.00")
    assert table.total_uses == flip.total_costs


# --- rental: the takeout (SPEC §8.5) -----------------------------------------------------------


def test_a_level_payment_amortizes_the_loan() -> None:
    """A 30-year 6.5% loan of 150,000 costs about $948 a month; the zero-rate case is P/n."""
    payment = monthly_payment(D("150000"), D("0.065"), 30)
    assert D("948.10") < payment < D("948.11")
    assert monthly_payment(D("360000"), D("0"), 30) == D("1000")
    with pytest.raises(ValueError, match="at least 1"):
        monthly_payment(D("100"), D("0.05"), 0)


def test_interest_only_is_a_month_of_interest() -> None:
    assert interest_only_payment(D("195000"), D("0.065")) == D("1056.25")


def test_the_takeout_is_sized_from_the_exit_value_down_to_the_rent() -> None:
    """260,000 x 75% = 195,000; the required rent grosses the target NOI up by the expenses."""
    deal = inputs(monthly_rent="2000")
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert rental.status is AnalysisStatus.EVALUATED
    assert rental.market_rent_status is AnalysisStatus.EVALUATED
    assert (rental.exit_value, rental.exit_value_source) == (
        D("260000"),
        ExitValueSource.SALE_PRICE,
    )
    assert rental.takeout_loan == D("195000.00")
    amortizing = monthly_payment(D("195000"), CONFIG.rental.takeout_rate, 30)
    assert rental.debt_service_amortizing == amortizing
    assert rental.debt_service_interest_only == D("1056.25")
    assert rental.required_noi == amortizing * CONFIG.rental.target_dscr
    assert rental.required_rent == rental.required_noi / D("0.65")
    assert D("2275.44") < rental.required_rent < D("2275.45")
    # the market side: 2,000 nets 1,300 after 35% expenses, short of the required rent
    assert rental.market_rent == D("2000")
    assert rental.market_noi == D("1300.00")
    assert rental.rent_gap == D("2000") - rental.required_rent
    assert rental.dscr_amortizing == D("1300.00") / amortizing
    assert rental.dscr_interest_only == D("1300.00") / D("1056.25")
    assert rental.rent_passed is False
    # the spread: the takeout takes out the commitment and the fee's payoff half
    assert rental.payoff_due == D("151500.00")
    assert rental.spread_to_loan == D("43500.00")
    assert rental.spread_passed is True


def test_a_rent_over_the_required_rent_passes() -> None:
    deal = inputs(monthly_rent="6000")
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert rental.rent_passed is True and rental.rent_gap is not None and rental.rent_gap > 0


def test_an_entered_exit_value_wins_over_the_sale_price() -> None:
    deal = inputs(rental_exit_value=D("300000"))
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert (rental.exit_value, rental.exit_value_source) == (D("300000"), ExitValueSource.ENTERED)
    assert rental.takeout_loan == D("225000.00")
    assert exit_value(deal) == (D("300000"), ExitValueSource.ENTERED)


def test_a_takeout_short_of_the_payoff_fails_the_spread() -> None:
    deal = inputs(rental_exit_value=D("150000"))
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert rental.takeout_loan == D("112500.00")
    assert rental.spread_to_loan == D("-39000.00")
    assert rental.spread_passed is False


def test_no_exit_value_leaves_the_rental_not_evaluated_but_keeps_the_market_rent() -> None:
    deal = inputs(estimated_sale_price=None)
    assert exit_value(deal) is None
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert rental.status is AnalysisStatus.NOT_EVALUATED
    assert rental.market_rent_status is AnalysisStatus.NOT_EVALUATED
    assert rental.market_rent == D("2000")
    assert rental.required_rent is None and rental.spread_to_loan is None
    assert rental.takeout_ltv == CONFIG.rental.takeout_ltv


def test_no_market_rent_still_sizes_the_takeout_and_the_spread() -> None:
    deal = inputs(monthly_rent=None)
    rental = rental_analysis(deal, terms(deal), CONFIG)
    assert rental.status is AnalysisStatus.EVALUATED
    assert rental.market_rent_status is AnalysisStatus.NOT_EVALUATED
    assert rental.required_rent is not None and rental.spread_to_loan == D("43500.00")
    assert rental.market_rent is None and rental.rent_gap is None
    assert rental.dscr_amortizing is None and rental.rent_passed is None


def test_holding_costs_are_not_in_the_rental() -> None:
    """A takeout lender underwrites the rent against its own debt service (SPEC §8.5)."""
    lean = inputs(monthly_rent="2000")
    heavy = inputs(monthly_rent="2000", holding_costs_pct_of_cost=D("0.10"))
    assert terms(heavy).holding_costs_monthly > terms(lean).holding_costs_monthly
    assert rental_analysis(lean, terms(lean), CONFIG) == rental_analysis(
        heavy, terms(heavy), CONFIG
    )


def test_the_required_rent_refuses_a_total_expense_ratio() -> None:
    assert required_rent(D("650"), D("0.35")) == D("1000")
    with pytest.raises(ValueError, match="no rent is enough"):
        required_rent(D("650"), D("1"))


def test_our_payoff_due_is_the_commitment_and_the_fees_payoff_half() -> None:
    assert payoff_due(terms(inputs())) == D("151500.00")


def test_all_three_analyses_run_on_a_deal_with_a_price_and_a_rent() -> None:
    """There are no toggles (engine 2.0.0): a price and a rent evaluate all three."""
    deal = inputs(monthly_rent="2000")
    loan = terms(deal)
    assert flip_for(deal).status is AnalysisStatus.EVALUATED
    assert rental_analysis(deal, loan, CONFIG).status is AnalysisStatus.EVALUATED
    assert take_back_analysis(deal, loan, CONFIG).status is AnalysisStatus.EVALUATED


# --- the stub period: a payoff date between two anchors (SPEC §8.1, §8.3) -----------------------


def stub_deal(**extra: object) -> UnderwriteInputs:
    """150,000 at 12% for nine months and eleven days: closing 15 March, anchored to month
    ends from 31 March, so the ninth anchor is 31 December and the payoff is 11 January."""
    return inputs(closing_date=date(2027, 3, 15), term_months=9, term_stub_days=11, **extra)


def test_the_term_carries_its_stub_and_the_payoff_date_lands_on_it() -> None:
    loan = terms(stub_deal())
    assert (loan.term_months, loan.stub_days) == (9, 11)
    assert loan.has_stub
    assert loan.payoff_date == date(2028, 1, 11)
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
    assert entries[9].date == date(2027, 12, 31) and entries[9].stub_days == 0
    last = entries[-1]
    assert last.date == date(2028, 1, 11) and last.stub_days == 11
    assert last.interest == D("550")
    # The payoff and the fee's payoff half land on the stub row, not on the anchor before it.
    assert last.payoff == D("150000") and last.fees == D("1500")
    assert entries[9].payoff == D(0) and entries[9].fees == D(0)


def test_the_stub_does_not_lengthen_the_rehab_period_or_the_draw_schedule() -> None:
    """Whole months only: a stub is days at the end of the term, after the listing period."""
    loan = terms(
        stub_deal(
            product=Product.ACQUISITION_REHAB,
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
    # twenty days past month 0, which is the end of the closing month
    assert entries[1].date == date(2027, 2, 20) and entries[1].stub_days == 20
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
