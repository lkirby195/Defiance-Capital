"""engine/underwrite.py: the run that assembles the ledger, the analyses and the flags.

# SPEC §8

The pieces have their own tests (``test_calc.py``, ``test_irr.py``); these are about the
assembly - that the caps cell comes from the same place the screen's does, that the screen's
own flags carry through, that each of the four underwrite codes is raised exactly when its
threshold says so and not otherwise, and that the result is the shape a stored row rebuilds.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from config.config import Config
from engine.underwrite import underwrite
from schema.models import (
    RETIRED_UNDERWRITE_FLAGS,
    AnalysisStatus,
    BorrowerInputs,
    CourtFlag,
    CourtRecordInputs,
    ExperienceBucket,
    ExperienceTier,
    Flag,
    LoanPurpose,
    Product,
    RepeatBorrowerStatus,
    ScreenFlag,
    Severity,
    SizingInputs,
    State,
    TakeBackAnalysis,
    Tranche,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
    ValueSource,
)

CONFIG = Config.load()
D = Decimal
CLOSING = date(2027, 1, 1)
SEARCHED = date(2026, 12, 1)

BORROWER = BorrowerInputs(
    credit_range_self_reported=Tranche.T1,
    experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
    repeat_borrower_self_reported=False,
)


def deal(
    *,
    product: Product = Product.ACQUISITION,
    purchase_price: str = "200000",
    rehab_costs: str = "0",
    loan_requested: str = "150000",
    purchase_portion: str | None = None,
    rehab_portion: str | None = None,
    estimated_sale_price: str | None = "260000",
    **extra: object,
) -> SizingInputs:
    return SizingInputs(
        product=product,
        purchase_price=D(purchase_price),
        rehab_costs=D(rehab_costs),
        loan_requested=D(loan_requested),
        estimated_sale_price=None if estimated_sale_price is None else D(estimated_sale_price),
        loan_purchase_portion=None if purchase_portion is None else D(purchase_portion),
        loan_rehab_portion=None if rehab_portion is None else D(rehab_portion),
        **extra,  # type: ignore[arg-type]
    )


def inputs(
    sizing: SizingInputs | None = None,
    *,
    term_months: int = 12,
    interest_rate: str = "0.12",
    monthly_rent: str | None = "2000",
    borrower: BorrowerInputs = BORROWER,
    court_records: CourtRecordInputs | None = None,
    **extra: object,
) -> UnderwriteInputs:
    return UnderwriteInputs(
        deal=sizing if sizing is not None else deal(),
        state=State.OK,
        borrower=borrower,
        closing_date=CLOSING,
        term_months=term_months,
        interest_rate=D(interest_rate),
        monthly_rent=None if monthly_rent is None else D(monthly_rent),
        court_records=(
            court_records if court_records is not None else CourtRecordInputs(as_of=SEARCHED)
        ),
        **extra,  # type: ignore[arg-type]
    )


def codes(flags: list[Flag]) -> set[str]:
    return {flag.code.value for flag in flags}


def find(flags: list[Flag], code: object) -> Flag:
    return next(flag for flag in flags if flag.code is code)


# --- the shape of a run --------------------------------------------------------------------------


def test_the_result_carries_the_dates_the_term_and_the_version() -> None:
    result = underwrite(inputs(term_months=9), CONFIG)
    assert result.engine_version and result.config_hash == CONFIG.config_hash
    assert result.closing_date == CLOSING
    assert result.payoff_date == date(2027, 10, 31)  # the month end the term lands on
    assert result.term_months == 9
    assert result.rehab_months == 6
    assert len(result.return_overview.entries) == 10


def test_the_caps_cell_comes_from_the_verified_values_the_screen_would_use() -> None:
    verified = BorrowerInputs(
        credit_range_self_reported=Tranche.T1,
        experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
        repeat_borrower_self_reported=False,
        verified_credit_score=705,  # T2 on the placeholder cutoffs
        verified_deals_36mo=2,  # E1
    )
    result = underwrite(inputs(borrower=verified), CONFIG)
    assert result.sizing.credit_tranche is Tranche.T2
    assert result.sizing.experience_tier is ExperienceTier.E1
    # ...and the screen's own mismatch flags come with it
    assert {"CREDIT_MISMATCH", "EXPERIENCE_MISMATCH"} <= codes(result.flags)


def test_the_economics_are_the_resolved_inputs_not_the_raw_ones() -> None:
    result = underwrite(
        inputs(
            deal(rehab_costs="50000", contingency_pct=D("0.10"), closing_costs_usd=D("1500.00")),
            origination_fee_pct=D("0.03"),
            holding_costs_pct_of_cost=D("0.048"),
        ),
        CONFIG,
    )
    economics = result.economics
    assert economics.contingency_pct == D("0.10")
    assert economics.contingency == D("5000.00")
    assert economics.rehab_adj == D("55000.00")
    assert economics.closing_costs == D("1500.00")
    assert economics.origination_fee_pct == D("0.03")
    # 4.8% of the 250,000 price plus rehab, over the twelve-month term.
    assert economics.holding_costs_pct_of_cost == D("0.048")
    assert economics.holding_costs_basis == D("250000.00")
    assert economics.holding_costs_total == D("12000.00")
    assert economics.holding_costs_monthly == D("1000")
    assert economics.commitment == D("150000")
    assert economics.loan_purchase_portion is None  # 100% at close has no split


def test_all_three_analyses_run_on_every_deal() -> None:
    """There are no toggles (engine 2.0.0): a missing input is NOT_EVALUATED, never OFF."""
    result = underwrite(inputs(term_months=6), CONFIG)
    assert result.analyses is None
    assert result.flip.status is AnalysisStatus.EVALUATED
    assert result.rental.status is AnalysisStatus.EVALUATED
    quiet = underwrite(inputs(deal(estimated_sale_price=None), monthly_rent=None), CONFIG)
    assert quiet.flip.status is AnalysisStatus.NOT_EVALUATED
    assert quiet.rental.status is AnalysisStatus.NOT_EVALUATED
    assert quiet.take_back.status is AnalysisStatus.NOT_EVALUATED


def test_the_run_lays_out_sources_and_uses() -> None:
    result = underwrite(inputs(), CONFIG)
    table = result.sources_and_uses
    assert table is not None
    assert table.advance_at_closing == D("150000") and table.rehab_holdback == 0
    assert table.borrower_equity == result.flip.borrower_equity
    assert table.total_sources.quantize(D("0.01")) == table.total_uses.quantize(D("0.01"))


def test_a_result_round_trips_through_json_exactly() -> None:
    result = underwrite(inputs(), CONFIG)
    assert UnderwriteResult.model_validate_json(result.model_dump_json()) == result


# --- the split products a run cannot price (SPEC §8.2) -------------------------------------------


def test_a_holdback_without_its_split_is_refused_at_the_door() -> None:
    with pytest.raises(ValueError, match="requires the loan split"):
        inputs(deal(product=Product.ACQUISITION_REHAB, rehab_costs="50000"))


# --- NO_REHAB_PERIOD (SPEC §8.3, §8.8) -----------------------------------------------------------


def holdback(term: int, rehab_portion: str = "25000") -> UnderwriteInputs:
    return inputs(
        deal(
            product=Product.ACQUISITION_REHAB,
            purchase_price="120000",
            rehab_costs="30000",
            loan_requested="110000",
            purchase_portion=str(D("110000") - D(rehab_portion)),
            rehab_portion=rehab_portion,
            estimated_sale_price="185000",
        ),
        term_months=term,
    )


def test_no_rehab_period_is_raised_when_the_term_leaves_none() -> None:
    result = underwrite(holdback(3), CONFIG)
    flag = find(result.flags, UnderwriteFlag.NO_REHAB_PERIOD)
    assert flag.severity is Severity.INFO
    assert "3 month(s)" in flag.message
    assert "3-month listing period" in flag.message
    assert "$25,000.00 rehab portion is advanced at close" in flag.message
    assert result.rehab_months == 0
    # and the ledger shows it: everything out at close, nothing drawn
    assert result.return_overview.total_draws == 0
    assert result.economics.funded_at_close == D("110000")


def test_no_rehab_period_is_not_raised_when_there_is_one() -> None:
    result = underwrite(holdback(9), CONFIG)
    assert UnderwriteFlag.NO_REHAB_PERIOD.value not in codes(result.flags)


def test_no_rehab_period_is_not_raised_on_a_product_with_no_rehab_portion() -> None:
    """100% at close on a 3-month term has no rehab period either, and nothing to draw."""
    result = underwrite(inputs(term_months=3), CONFIG)
    assert result.rehab_months == 0
    assert UnderwriteFlag.NO_REHAB_PERIOD.value not in codes(result.flags)


# --- the rental's and the take-back's flags (SPEC §8.5, §8.6, §8.8) ---------------------------


def test_a_market_rent_below_the_required_rent_is_flagged_with_the_takeout_it_missed() -> None:
    result = underwrite(inputs(monthly_rent="2000"), CONFIG)
    flag = find(result.flags, UnderwriteFlag.MARKET_RENT_BELOW_REQUIRED)
    assert flag.severity is CONFIG.flags.underwrite_severities[flag.code]
    assert "$2,000.00 is below the $2,275.44" in flag.message
    assert "1.20x target DSCR" in flag.message
    assert "6.5%" in flag.message and "30 years" in flag.message
    assert "$195,000.00 takeout" in flag.message
    assert result.rental.rent_passed is False


def test_a_market_rent_over_the_required_rent_is_not_flagged() -> None:
    result = underwrite(inputs(monthly_rent="8000"), CONFIG)
    assert result.rental.rent_passed is True
    assert UnderwriteFlag.MARKET_RENT_BELOW_REQUIRED.value not in codes(result.flags)


def test_a_takeout_short_of_the_payoff_is_flagged() -> None:
    result = underwrite(inputs(rental_exit_value=D("150000")), CONFIG)
    flag = find(result.flags, UnderwriteFlag.TAKEOUT_BELOW_PAYOFF)
    assert flag.severity is CONFIG.flags.underwrite_severities[flag.code]
    assert "$112,500.00 takeout loan is $39,000.00 short of the $151,500.00 payoff" in flag.message
    assert result.rental.spread_passed is False
    # the default exit value (the 260,000 sale price) clears it
    cleared = underwrite(inputs(), CONFIG)
    assert UnderwriteFlag.TAKEOUT_BELOW_PAYOFF.value not in codes(cleared.flags)


def test_a_take_back_price_above_the_purchase_price_is_hard_and_names_the_haircut() -> None:
    """A 150,000 loan on a 150,000 house: the lender needs about 198,000 to come out whole."""
    result = underwrite(inputs(deal(purchase_price="150000")), CONFIG)
    flag = find(result.flags, UnderwriteFlag.TAKE_BACK_PRICE_ABOVE_PURCHASE)
    assert flag.severity is Severity.HARD
    assert "of the $150,000.00 purchase price" in flag.message
    assert "haircut from the $260,000.00 projected sale price" in flag.message
    assert "default in month 9, 3 months' lost interest, $5,000.00 of legal costs" in flag.message
    assert "6 months' foreclosure carry" in flag.message
    assert result.take_back.price_above_purchase is True


def test_a_take_back_price_under_the_purchase_price_is_not_flagged() -> None:
    result = underwrite(inputs(), CONFIG)
    assert result.take_back.price_above_purchase is False
    assert UnderwriteFlag.TAKE_BACK_PRICE_ABOVE_PURCHASE.value not in codes(result.flags)


def test_the_retired_dscr_codes_are_never_raised() -> None:
    for rent in ("500", "2000", None):
        result = underwrite(inputs(monthly_rent=rent), CONFIG)
        assert codes(result.flags).isdisjoint({code.value for code in RETIRED_UNDERWRITE_FLAGS})


# --- MONTHLY_RENT_MISSING (SPEC §8.5, §8.6, §8.8) ------------------------------------------------


def test_no_rent_leaves_the_market_rent_lines_unevaluated_under_one_informational_flag() -> None:
    result = underwrite(inputs(monthly_rent=None), CONFIG)
    flag = find(result.flags, UnderwriteFlag.MONTHLY_RENT_MISSING)
    assert flag.severity is Severity.INFO
    assert "nothing stands in for what a property lets for" in flag.message
    assert "market-rent lines" in flag.message
    # the takeout and the spread do not read the rent, so they are still reported
    assert "$2,275.44 required rent" in flag.message and "$43,500.00 spread" in flag.message
    assert result.rental.status is AnalysisStatus.EVALUATED
    assert result.rental.market_rent_status is AnalysisStatus.NOT_EVALUATED
    assert result.take_back.status is AnalysisStatus.EVALUATED
    assert result.take_back.market_rent_status is AnalysisStatus.NOT_EVALUATED
    assert codes(result.flags).isdisjoint(
        {UnderwriteFlag.MARKET_RENT_BELOW_REQUIRED.value, UnderwriteFlag.DSCR_BELOW_FLOOR.value}
    )


def test_a_rent_on_the_deal_leaves_the_flag_unraised() -> None:
    result = underwrite(inputs(monthly_rent="2000"), CONFIG)
    assert UnderwriteFlag.MONTHLY_RENT_MISSING.value not in codes(result.flags)


def test_the_other_entry_standing_in_for_a_state_is_informational() -> None:
    """A state outside the footprint has no timeline of its own: config's OTHER entry
    stands in, the take-back runs on it, and the flag names the placeholder (v0.20)."""
    elsewhere = inputs().model_copy(update={"state": State.OTHER})
    result = underwrite(elsewhere, CONFIG)
    flag = find(result.flags, UnderwriteFlag.FORECLOSURE_MONTHS_MISSING)
    assert flag.severity is Severity.INFO
    assert "No foreclosure months in config for Other" in flag.message
    assert "OTHER placeholder stood in" in flag.message and "8 months" in flag.message
    assert "take_back.foreclosure_months.OTHER" in flag.message
    take_back = result.take_back
    assert isinstance(take_back, TakeBackAnalysis)
    assert take_back.status is AnalysisStatus.EVALUATED
    assert (take_back.foreclosure_months, take_back.foreclosure_months_from) == (8, State.OTHER)
    assert take_back.lost_interest == D("4500.00")
    # a served state is clean
    clean = underwrite(inputs(), CONFIG)
    assert UnderwriteFlag.FORECLOSURE_MONTHS_MISSING.value not in codes(clean.flags)


def test_no_foreclosure_months_at_all_leaves_the_take_back_unevaluated() -> None:
    config = CONFIG.model_copy(
        update={
            "take_back": CONFIG.take_back.model_copy(
                update={"foreclosure_months": {State.OK: 6, State.CO: 4}}
            )
        }
    )
    elsewhere = inputs().model_copy(update={"state": State.OTHER})
    result = underwrite(elsewhere, config)
    flag = find(result.flags, UnderwriteFlag.FORECLOSURE_MONTHS_MISSING)
    assert flag.severity is Severity.INFO and "was not evaluated" in flag.message
    assert result.take_back.status is AnalysisStatus.NOT_EVALUATED
    assert isinstance(result.take_back, TakeBackAnalysis)
    assert result.take_back.foreclosure_months is None
    assert result.take_back.lost_interest == D("4500.00")  # reported regardless


# --- BORROWER_COC_BELOW_TARGET (SPEC §8.4, §8.8) -------------------------------------------------


def test_a_cash_on_cash_under_the_target_is_flagged_with_the_target_it_missed() -> None:
    """A 230,000 sale on 226,000 of deal cost nets a loss on 76,000 of equity."""
    result = underwrite(inputs(deal(estimated_sale_price="230000")), CONFIG)
    flag = find(result.flags, UnderwriteFlag.BORROWER_COC_BELOW_TARGET)
    severity = CONFIG.flags.underwrite_severities[UnderwriteFlag.BORROWER_COC_BELOW_TARGET]
    assert flag.severity is severity is Severity.SOFT
    assert "20.0% target" in flag.message
    assert "$76,000.00 of equity" in flag.message
    assert result.flip.cash_on_cash_passed is False


def test_a_cash_on_cash_over_the_target_is_not_flagged() -> None:
    """260,000 nets 18,400 on 76,000: 24.2%, over the 20% placeholder."""
    result = underwrite(inputs(), CONFIG)
    assert result.flip.cash_on_cash_passed is True
    assert UnderwriteFlag.BORROWER_COC_BELOW_TARGET.value not in codes(result.flags)


def test_no_sale_price_tests_no_cash_on_cash() -> None:
    result = underwrite(inputs(deal(estimated_sale_price=None)), CONFIG)
    assert result.flip.cash_on_cash_passed is None
    assert UnderwriteFlag.BORROWER_COC_BELOW_TARGET.value not in codes(result.flags)


# --- the screen's flags carry through (SPEC §8) --------------------------------------------------


def test_the_leverage_flags_the_screen_raises_are_raised_here_too() -> None:
    result = underwrite(
        inputs(deal(loan_requested="240000", estimated_sale_price="260000")),
        CONFIG,
    )
    assert ScreenFlag.LTC_OVER_CAP.value in codes(result.flags)
    assert ScreenFlag.LTV_OVER_CAP.value in codes(result.flags)


def test_a_run_with_no_sale_price_prices_the_deal_and_says_what_it_lost() -> None:
    """SPEC §8.4: the flip is what goes missing, not the run."""
    result = underwrite(inputs(deal(estimated_sale_price=None)), CONFIG)
    assert result.flip.status is AnalysisStatus.NOT_EVALUATED
    assert result.flip.net_profit is None and result.flip.project_yield is None
    # ...and everything that does not read a sale price is there in full
    assert result.flip.total_costs > 0
    assert result.return_overview.irr is not None
    # the take-back's solves and the rental's takeout are set against the sale price too
    assert result.take_back.status is AnalysisStatus.NOT_EVALUATED
    assert result.rental.status is AnalysisStatus.NOT_EVALUATED

    flag = find(result.flags, UnderwriteFlag.SALE_PRICE_MISSING)
    assert flag.severity is Severity.INFO
    assert "the Flip analysis was not evaluated" in flag.message
    assert "LTV is not computed" in flag.message
    assert "Take-Back's haircut and hold-and-rent solves" in flag.message
    assert "exit value defaults to it" in flag.message
    # the leverage side of the same absence keeps its own Soft flag (SPEC §7.4)
    assert ScreenFlag.ESTIMATED_SALE_PRICE_MISSING.value in codes(result.flags)


def test_the_court_tests_are_re_run_here_on_the_record_in_force() -> None:
    """Not copied from the screens row: weeks pass, and a pull may have landed since."""
    found = CourtRecordInputs(
        as_of=SEARCHED,
        source=ValueSource.ADAPTER,
        open_tax_liens_usd=[D("4000")],
    )
    result = underwrite(inputs(court_records=found), CONFIG)
    assert CourtFlag.OPEN_TAX_LIEN.value in codes(result.flags)


def test_no_court_record_at_all_is_reported_rather_than_read_as_clean() -> None:
    """NOT_CHECKED is not CLEAN (SPEC §7.2), and the underwrite says so as the screen does."""
    nothing_checked = UnderwriteInputs(
        deal=deal(),
        state=State.OK,
        borrower=BORROWER,
        closing_date=CLOSING,
        term_months=12,
        interest_rate=D("0.12"),
        monthly_rent=D("2000"),
        court_records=None,
    )
    result = underwrite(nothing_checked, CONFIG)
    assert ScreenFlag.COURT_RECORDS_NOT_CHECKED.value in codes(result.flags)


def test_hand_entered_values_are_named() -> None:
    by_hand = SizingInputs(
        product=Product.ACQUISITION,
        purchase_price=D("200000"),
        rehab_costs=D("0"),
        loan_requested=D("150000"),
        estimated_sale_price=D("260000"),
        estimated_sale_price_source=ValueSource.TEAM,
    )
    result = underwrite(inputs(by_hand), CONFIG)
    flag = find(result.flags, ScreenFlag.TEAM_SOURCED_VALUES)
    assert flag.message.startswith("Estimated sale price came from the team")
    assert flag.severity is Severity.INFO


# --- flags are ordered and every one carries a message -------------------------------------------


def test_flags_come_back_hard_first_and_every_one_says_what_it_tested() -> None:
    result = underwrite(
        inputs(
            deal(loan_requested="240000"),
            monthly_rent="2000",
            borrower=BorrowerInputs(
                credit_range_self_reported=Tranche.T1,
                experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
                repeat_borrower_self_reported=True,
                repeat_borrower_verified=RepeatBorrowerStatus.NO_MATCH,
            ),
            loan_purpose=LoanPurpose.PURCHASE,
        ),
        CONFIG,
    )
    ranks = {Severity.HARD: 0, Severity.SOFT: 1, Severity.INFO: 2}
    order = [ranks[flag.severity] for flag in result.flags]
    assert order == sorted(order)
    assert all(flag.message.strip() for flag in result.flags)
    assert result.loan_purpose is LoanPurpose.PURCHASE


def test_a_borrower_rent_is_named_and_a_team_sale_price_beside_it_is_named_too() -> None:
    """SPEC §4.2, §6.1: both provenance flags, each naming its own inputs."""
    by_hand = SizingInputs(
        product=Product.ACQUISITION,
        purchase_price=D("200000"),
        rehab_costs=D("0"),
        loan_requested=D("150000"),
        estimated_sale_price=D("260000"),
        estimated_sale_price_source=ValueSource.TEAM,
    )
    result = underwrite(
        inputs(by_hand, monthly_rent=D("1800"), monthly_rent_source=ValueSource.BORROWER),
        CONFIG,
    )
    borrower = find(result.flags, ScreenFlag.BORROWER_SOURCED_VALUES)
    assert borrower.severity is Severity.INFO
    assert borrower.message.startswith("Monthly rent came from the borrower")
    team = find(result.flags, ScreenFlag.TEAM_SOURCED_VALUES)
    assert team.message.startswith("Estimated sale price came from the team")
    # the DSCRs ran on the borrower's rent exactly as they would on anyone's
    assert result.rental.market_rent == D("1800")


def test_a_rent_source_needs_a_rent() -> None:
    with pytest.raises(ValidationError, match="source cannot be recorded without its value"):
        inputs(monthly_rent=None, monthly_rent_source=ValueSource.BORROWER)
