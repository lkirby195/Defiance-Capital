"""engine/calc: outstanding, lender, borrower, exit, downside.  # SPEC §8.3-8.6

Expected numbers are worked by hand in the comments so the tests are independent of the
engine's own arithmetic.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from config.config import DEFAULT_PATH, Config, load_yaml
from engine.calc.borrower import (
    borrower_economics,
    exit_net,
    monthly_holding_cost,
    resolve_annual_insurance,
    resolve_annual_taxes,
    resolve_exit_price,
)
from engine.calc.downside import liquidation_value, recovery_basis, reo_downside
from engine.calc.exit import (
    annual_opex,
    dscr_at,
    dscr_loan,
    dscr_takeout,
    infer_exit,
    loan_for_payment,
    monthly_payment,
    payoff_due,
)
from engine.calc.lender import (
    annualized_yield,
    fee_schedule,
    interest,
    lender_return,
    payoff_fees,
)
from engine.calc.outstanding import (
    LoanTerms,
    average_outstanding,
    dollar_months,
    loan_terms,
    rehab_months,
    tranche_a_dollar_months,
)
from engine.sizing import buy_closing, size_deal
from schema.models import (
    AssetType,
    BorrowerInputs,
    ExitSource,
    ExperienceBucket,
    ExperienceTier,
    OpexSource,
    Product,
    SizingInputs,
    State,
    StatedExit,
    Tranche,
    UnderwriteInputs,
    ValueSource,
)

CONFIG = Config.load()
D = Decimal
CENT = D("0.01")


def config_with(**sections: dict[str, Any]) -> Config:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    for section, values in sections.items():
        for key, value in values.items():
            data[section][key] = value
    return Config.from_dict(data)


def borrower(**overrides: Any) -> BorrowerInputs:
    base: dict[str, Any] = {
        "credit_range_self_reported": Tranche.T2,
        "experience_bucket_self_reported": ExperienceBucket.THREE_TO_FIVE,
        "repeat_borrower_self_reported": False,
    }
    base.update(overrides)
    return BorrowerInputs(**base)


# go_no_draw: NO_DRAW, price 150,000, loan 105,000, as-is 160,000, ARV 165,000, term 9
NO_DRAW_DEAL = SizingInputs(
    product=Product.NO_DRAW,
    purchase_price=D("150000.00"),
    rehab_budget=D("0.00"),
    loan_requested=D("105000.00"),
    as_is_value=D("160000.00"),
    arv=D("165000.00"),
)
# go_split_principal_repeat_override: price 150,000, rehab 60,000 (rehab_adj 66,000),
# loan 170,000 -> Principal Note 104,000 + Tranche A 66,000; as-is 230,000, ARV 290,000
SPLIT_PRINCIPAL_DEAL = SizingInputs(
    product=Product.SPLIT_PRINCIPAL,
    purchase_price=D("150000.00"),
    rehab_budget=D("60000.00"),
    loan_requested=D("170000.00"),
    loan_purchase_portion=D("104000.00"),
    loan_rehab_portion=D("66000.00"),
    as_is_value=D("230000.00"),
    arv=D("290000.00"),
)


def sized(deal: SizingInputs, config: Config = CONFIG) -> Any:
    return size_deal(deal, Tranche.T2, ExperienceTier.E2, config)


def loan(
    deal: SizingInputs = NO_DRAW_DEAL,
    term: int = 9,
    extension: Decimal | None = None,
    config: Config = CONFIG,
) -> LoanTerms:
    return loan_terms(sized(deal, config), term, extension, config)


def inputs(deal: SizingInputs = NO_DRAW_DEAL, **overrides: Any) -> UnderwriteInputs:
    base: dict[str, Any] = {
        "deal": deal,
        "state": State.OK,
        "borrower": borrower(),
        "term_months": 9,
        "market_rent_monthly": D("1500.00"),
        "annual_taxes_usd": D("1800.00"),
        "annual_insurance_usd": D("1200.00"),
        "annual_utilities_usd": D("600.00"),
    }
    base.update(overrides)
    return UnderwriteInputs(**base)


# --- §8.3 outstanding ----------------------------------------------------------------------------


@pytest.mark.parametrize("term, expected", [(12, 9), (9, 6), (6, 3), (3, 0), (2, 0), (1, 0)])
def test_rehab_months_is_term_less_listing_months_floored_at_zero(term: int, expected: int) -> None:
    assert CONFIG.draws.listing_months == 3
    assert rehab_months(term, CONFIG) == expected


def test_rehab_months_uses_config() -> None:
    assert rehab_months(12, config_with(draws={"listing_months": 2})) == 10
    with pytest.raises(ValueError, match="at least 1"):
        rehab_months(0, CONFIG)


def test_loan_terms_bundles_term_rehab_and_extension_default() -> None:
    terms = loan(term=12)
    assert terms.term_months == 12 and terms.rehab_months == 9
    assert terms.extension_fee_pct == CONFIG.fees.extension_default_pct == 0
    assert terms.commitment == D("105000.00") and terms.product is Product.NO_DRAW
    assert loan(extension=D("0.01")).extension_fee_pct == D("0.01")


def test_tranche_a_dollar_months_straight_line_then_fully_drawn() -> None:
    # 66,000 x (9 x 0.5 + (12 - 9)) = 66,000 x 7.5 = 495,000
    assert tranche_a_dollar_months(D("66000"), 12, 9, CONFIG) == D("495000.0")
    # at rehab completion: 66,000 x 4.5 = 297,000
    assert tranche_a_dollar_months(D("66000"), 9, 9, CONFIG) == D("297000.0")
    # rehab_months 0: fully drawn from close
    assert tranche_a_dollar_months(D("66000"), 6, 0, CONFIG) == D("396000")


def test_tranche_a_average_utilization_comes_from_config() -> None:
    cfg = config_with(draws={"draw_avg_utilization": D("0.60")})
    # 66,000 x (9 x 0.6 + 3) = 66,000 x 8.4 = 554,400
    assert tranche_a_dollar_months(D("66000"), 12, 9, cfg) == D("554400.0")


def test_payoff_before_rehab_completion_is_rejected() -> None:
    with pytest.raises(ValueError, match="no early payoff"):
        tranche_a_dollar_months(D("66000"), 8, 9, CONFIG)


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL, Product.SPLIT_DRAW])
def test_single_note_and_split_draw_are_fully_funded_from_close(product: Product) -> None:
    deal = SizingInputs(
        product=product,
        purchase_price=D("150000.00"),
        rehab_budget=D("20000.00"),
        loan_requested=D("120000.00"),
        as_is_value=D("160000.00"),
        arv=D("200000.00"),
    )
    terms = loan(deal, term=9)
    assert dollar_months(terms, 9, CONFIG) == D("120000.00") * 9
    assert average_outstanding(terms, 9, CONFIG) == D("120000.00")
    assert average_outstanding(terms, 15, CONFIG) == D("120000.00")


def test_split_principal_average_outstanding() -> None:
    terms = loan(SPLIT_PRINCIPAL_DEAL, term=12)
    # 104,000 x 12 + 495,000 = 1,743,000; / 12 = 145,250
    assert dollar_months(terms, 12, CONFIG) == D("1743000.0")
    assert average_outstanding(terms, 12, CONFIG) == D("145250")
    # month 13: 104,000 x 13 + 66,000 x (4.5 + 4) = 1,352,000 + 561,000 = 1,913,000
    assert dollar_months(terms, 13, CONFIG) == D("1913000.0")


def test_dollar_months_rejects_month_below_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        dollar_months(loan(), 0, CONFIG)


# --- §8.4 lender ---------------------------------------------------------------------------------


def test_fee_schedule_origination_split_on_total_commitment() -> None:
    fees = fee_schedule(loan(SPLIT_PRINCIPAL_DEAL, term=12), 12, CONFIG)
    assert fees.origination_at_close == D("1700.00")
    assert fees.origination_at_payoff == D("1700.00")
    assert fees.extension == 0
    assert fees.total == D("3400.00")
    assert payoff_fees(loan(SPLIT_PRINCIPAL_DEAL, term=12), CONFIG) == D("1700.00")


def test_extension_fee_only_past_term_and_from_the_loan() -> None:
    terms = loan(SPLIT_PRINCIPAL_DEAL, term=12, extension=D("0.01"))
    assert fee_schedule(terms, 12, CONFIG).extension == 0
    assert fee_schedule(terms, 13, CONFIG).extension == D("1700.00")
    assert fee_schedule(terms, 13, CONFIG).total == D("5100.00")
    assert fee_schedule(loan(SPLIT_PRINCIPAL_DEAL, term=12), 13, CONFIG).extension == 0  # default 0


def test_interest_is_dollar_months_times_rate_over_twelve() -> None:
    # NO_DRAW: 105,000 x 0.12 x 9 / 12 = 9,450
    assert interest(loan(), 9, D("0.12"), CONFIG) == D("9450.0000")
    # SPLIT_PRINCIPAL: 1,743,000 x 0.12 / 12 = 17,430
    assert interest(loan(SPLIT_PRINCIPAL_DEAL, term=12), 12, D("0.12"), CONFIG) == D("17430.000")


def test_annualized_yield_denominator_is_full_commitment() -> None:
    # (9,450 + 2,100) / 105,000 x 12 / 9 = 0.14666...
    result = annualized_yield(D("9450"), D("2100"), D("105000"), 9)
    assert result.quantize(D("0.000001")) == D("0.146667")
    with pytest.raises(ValueError, match="commitment"):
        annualized_yield(D("1"), D("1"), D("0"), 9)
    with pytest.raises(ValueError, match="month"):
        annualized_yield(D("1"), D("1"), D("1"), 0)


def test_lender_return_assembles_everything() -> None:
    result = lender_return(loan(), 9, D("0.12"), CONFIG)
    assert result.month == 9 and result.rate == D("0.12")
    assert result.avg_outstanding == D("105000.00")
    assert result.interest == D("9450.0000")
    assert result.fees.total == D("2100.00")
    assert result.annualized_yield.quantize(D("0.000001")) == D("0.146667")


def test_lender_yield_falls_as_payoff_slides_without_extension_fee() -> None:
    terms = loan()
    yields = [lender_return(terms, m, D("0.12"), CONFIG).annualized_yield for m in (9, 12, 15)]
    assert yields[0] > yields[1] > yields[2]


# --- §8.6 borrower -------------------------------------------------------------------------------


def test_taxes_and_insurance_actual_or_default_pct_of_as_is_value() -> None:
    actual = inputs()
    assert resolve_annual_taxes(actual, CONFIG) == (D("1800.00"), OpexSource.ACTUAL)
    assert resolve_annual_insurance(actual, CONFIG) == (D("1200.00"), OpexSource.ACTUAL)
    defaulted = inputs(annual_taxes_usd=None, annual_insurance_usd=None)
    # SPEC §8.6: the defaults are a percentage of the as-is value, not the ARV.
    # as-is 160,000 x 1.2% = 1,920; x 0.5% = 800 (the ARV 165,000 would give 1,980 / 825)
    assert resolve_annual_taxes(defaulted, CONFIG) == (D("1920.000"), OpexSource.DEFAULT)
    assert resolve_annual_insurance(defaulted, CONFIG) == (D("800.000"), OpexSource.DEFAULT)


def test_opex_defaults_track_the_as_is_value_not_the_arv() -> None:
    """Moving the ARV alone leaves the defaults alone; moving the as-is value moves them."""
    base = inputs(annual_taxes_usd=None, annual_insurance_usd=None)
    richer_arv = base.model_copy(
        update={"deal": base.deal.model_copy(update={"arv": D("400000.00")})}
    )
    assert resolve_annual_taxes(richer_arv, CONFIG) == resolve_annual_taxes(base, CONFIG)
    richer_as_is = base.model_copy(
        update={"deal": base.deal.model_copy(update={"as_is_value": D("320000.00")})}
    )
    # 320,000 x 1.2% = 3,840, exactly double the 160,000 case
    assert resolve_annual_taxes(richer_as_is, CONFIG)[0] == D("3840.000")


def test_monthly_holding_cost() -> None:
    # (1,800 + 1,200 + 600) / 12 = 300
    assert monthly_holding_cost(inputs(), CONFIG) == D("300")


def test_buy_closing_and_exit_net_from_config() -> None:
    assert buy_closing(D("150000"), CONFIG) == D("4500.00")  # 3%
    assert exit_net(D("165000"), CONFIG) == D("155100.00")  # x 0.94
    cfg = config_with(
        fees={"borrower_closing_pct_of_price": D("0.04"), "selling_cost_pct": D("0.05")}
    )
    assert buy_closing(D("150000"), cfg) == D("6000.00")
    assert exit_net(D("165000"), cfg) == D("156750.00")


def test_exit_price_defaults_to_arv_unless_team_sets_it() -> None:
    assert resolve_exit_price(inputs()) == D("165000.00")
    assert resolve_exit_price(inputs(exit_price=D("170000.00"))) == D("170000.00")


def test_borrower_economics_at_term_and_solved_rate() -> None:
    # r* for a single-note 9-month loan = 0.175 - 0.02 x 12 / 9 = 0.148333...; interest 11,681.25
    result = borrower_economics(inputs(), loan(), 9, D("11681.25") / D("78750"), CONFIG)
    assert result.total_project_cost == D("154500.00")  # 150,000 + 0 + 4,500
    assert result.interest_paid.quantize(CENT) == D("11681.25")
    assert result.fees_paid == D("2100.00")
    assert result.holding_costs == D("2700")  # 300 x 9
    assert result.exit_price == D("165000.00")
    assert result.exit_net == D("155100.00")
    # 155,100 - 154,500 - 11,681.25 - 2,100 - 2,700 = -15,881.25
    assert result.profit.quantize(CENT) == D("-15881.25")
    # 154,500 + 11,681.25 + 2,100 + 2,700 - 105,000 = 65,981.25
    assert result.cash_in.quantize(CENT) == D("65981.25")
    assert result.cash_on_cash is not None
    assert result.cash_on_cash.quantize(D("0.0001")) == D("-0.2407")


def test_borrower_economics_uses_rehab_adj_as_the_rehab_cost() -> None:
    result = borrower_economics(
        inputs(SPLIT_PRINCIPAL_DEAL, term_months=12),
        loan(SPLIT_PRINCIPAL_DEAL, term=12),
        12,
        D("0.12"),
        CONFIG,
    )
    assert result.rehab_adj == D("66000.0000")
    assert result.total_project_cost == D("220500.0000")  # 150,000 + 66,000 + 4,500


def test_cash_on_cash_is_none_when_no_cash_in() -> None:
    # loan the whole project and more: cash_in <= 0
    deal = SizingInputs(
        product=Product.NO_DRAW,
        purchase_price=D("100000.00"),
        rehab_budget=D("0.00"),
        loan_requested=D("140000.00"),
        as_is_value=D("200000.00"),
        arv=D("200000.00"),
    )
    result = borrower_economics(inputs(deal), loan(deal), 9, D("0.10"), CONFIG)
    assert result.cash_in < 0
    assert result.cash_on_cash is None


# --- §8.6 DSCR takeout ---------------------------------------------------------------------------


def test_monthly_payment_matches_the_mortgage_table() -> None:
    # $100,000 at 7.5% over 30 years is $699.21 / month
    assert monthly_payment(D("100000"), D("0.075"), 30).quantize(CENT) == D("699.21")
    assert monthly_payment(D("120000"), D("0"), 30) == D("333.3333333333333333333333333")


def test_loan_for_payment_inverts_monthly_payment() -> None:
    principal = loan_for_payment(D("699.21"), D("0.075"), 30)
    assert abs(principal - D("100000")) < 1  # 699.21 is the payment rounded to the cent
    assert monthly_payment(principal, D("0.075"), 30).quantize(CENT) == D("699.21")
    assert loan_for_payment(D("100"), D("0"), 30) == D("36000")


def test_annual_opex_pct_lines_on_gross_rent_plus_taxes_and_insurance() -> None:
    # 18,000 x (0.05 + 0.08 + 0.05) + 1,800 + 1,200 = 3,240 + 3,000 = 6,240
    assert annual_opex(D("18000"), D("1800"), D("1200"), CONFIG) == D("6240.00")


def test_dscr_loan_is_the_loan_whose_service_is_noi_over_floor() -> None:
    by_dscr = dscr_loan(D("11760"), CONFIG)
    service = monthly_payment(by_dscr, CONFIG.takeout.rate, CONFIG.takeout.amortization_years) * 12
    assert (service * CONFIG.takeout.dscr_floor).quantize(CENT) == D("11760.00")
    assert dscr_loan(D("0"), CONFIG) == 0 and dscr_loan(D("-500"), CONFIG) == 0


def test_payoff_due_and_dscr_at() -> None:
    assert payoff_due(loan(), CONFIG) == D("106050.00")  # 105,000 + 1%
    assert dscr_at(D("0"), D("11760"), CONFIG) is None
    ratio = dscr_at(D("106050"), D("11760"), CONFIG)
    assert ratio is not None and ratio.quantize(D("0.001")) == D("1.322")


def test_dscr_takeout_covers_when_max_takeout_meets_payoff() -> None:
    result = dscr_takeout(inputs(stated_exit=StatedExit.HOLD), loan(), CONFIG)
    assert result.type is StatedExit.HOLD
    assert result.gross_rent_annual == D("18000.00")
    assert result.annual_taxes_source is OpexSource.ACTUAL
    assert result.opex_annual == D("6240.00") and result.noi_annual == D("11760.00")
    assert result.ltv_takeout == D("123750.0000")  # 165,000 x 0.75
    assert result.dscr_takeout.quantize(CENT) == D("116797.73")
    assert result.max_takeout == result.dscr_takeout  # the DSCR loan binds here
    assert result.payoff_due == D("106050.00")
    assert result.refi_covers is True and result.shortfall == 0
    assert result.dscr_at_payoff is not None and result.dscr_at_payoff > CONFIG.takeout.dscr_floor


def test_dscr_takeout_reports_the_shortfall() -> None:
    # rent 900: gross 10,800; opex 1,944 + 3,000 = 4,944; NOI 5,856 -> DSCR loan ~58,160
    result = dscr_takeout(inputs(market_rent_monthly=D("900.00")), loan(), CONFIG)
    assert result.noi_annual == D("5856.00")
    assert result.max_takeout == result.dscr_takeout < result.ltv_takeout
    assert result.refi_covers is False
    assert result.shortfall == result.payoff_due - result.max_takeout > 0


def test_dscr_takeout_ltv_binds_when_rent_is_high() -> None:
    result = dscr_takeout(inputs(market_rent_monthly=D("5000.00")), loan(), CONFIG)
    assert result.max_takeout == result.ltv_takeout == D("123750.0000")


def test_takeout_assumptions_come_from_config() -> None:
    cfg = config_with(takeout={"ltv": D("0.70"), "dscr_floor": D("1.0")})
    result = dscr_takeout(inputs(), loan(config=cfg), cfg)
    assert result.ltv_takeout == D("115500.0000")  # 165,000 x 0.70
    assert result.dscr_takeout > dscr_loan(D("11760"), CONFIG)  # looser floor -> larger loan


# --- §8.6 REO downside ---------------------------------------------------------------------------


def test_recovery_basis_and_liquidation() -> None:
    assert recovery_basis(D("160000"), D("0"), D("165000")) == D("160000")
    assert recovery_basis(D("230000"), D("66000"), D("290000")) == D("290000")  # capped at ARV
    assert liquidation_value(D("160000"), CONFIG) == D("136000.00")  # x 0.85


def test_reo_downside_numbers() -> None:
    result = reo_downside(inputs(), loan(), CONFIG)
    assert result.recovery_basis == D("160000.00")
    assert result.liquidation == D("136000.0000")
    assert result.selling_costs == D("8160.000000")  # 6%
    assert result.foreclosure_cost == D("10000")
    assert result.foreclosure_months == 8  # OK
    assert result.monthly_holding_cost == D("300")
    assert result.holding_through_foreclosure == D("2400")
    # 136,000 - 8,160 - 10,000 - 2,400 = 115,440
    assert result.recovery == D("115440.000000")
    assert result.unpaid_fees == D("1050.00") and result.exposure == D("106050.00")
    assert result.cover.quantize(D("0.0001")) == D("1.0885")
    assert result.cover_floor == D("1.0") and result.passed is True


def test_foreclosure_months_by_state() -> None:
    ok = reo_downside(inputs(state=State.OK), loan(), CONFIG)
    co = reo_downside(inputs(state=State.CO), loan(), CONFIG)
    assert (ok.foreclosure_months, co.foreclosure_months) == (8, 4)
    assert co.recovery - ok.recovery == D("300") * 4


def test_downside_fails_below_the_cover_floor() -> None:
    # small deal: 60,000 loan on an 85,000 house; fixed foreclosure cost sinks the cover
    deal = SizingInputs(
        product=Product.NO_DRAW,
        purchase_price=D("80000.00"),
        rehab_budget=D("0.00"),
        loan_requested=D("60000.00"),
        as_is_value=D("85000.00"),
        arv=D("95000.00"),
    )
    small = inputs(
        deal,
        annual_taxes_usd=D("1000.00"),
        annual_insurance_usd=D("800.00"),
        annual_utilities_usd=D("600.00"),
    )
    result = reo_downside(small, loan(deal), CONFIG)
    # 85,000 x 0.85 = 72,250; x 0.94 = 67,915; - 10,000 - 8 x 200 = 56,315 vs 60,600
    assert result.recovery == D("56315.000000")
    assert result.exposure == D("60600.00")
    assert result.passed is False
    assert reo_downside(small, loan(deal), config_with(downside={"cover_floor": D("0.9")})).passed


def test_downside_assumptions_come_from_config() -> None:
    cfg = config_with(downside={"reo_haircut": D("0.20"), "foreclosure_cost_usd": D("5000")})
    result = reo_downside(inputs(), loan(config=cfg), cfg)
    assert result.liquidation == D("128000.0000")
    assert result.foreclosure_cost == D("5000")


# --- inputs --------------------------------------------------------------------------------------


def test_underwrite_inputs_require_valuation() -> None:
    without = {**NO_DRAW_DEAL.model_dump(), "arv": None, "arv_source": None}
    with pytest.raises(ValidationError, match="as_is_value and arv"):
        inputs(SizingInputs(**without))
    without = {**NO_DRAW_DEAL.model_dump(), "as_is_value": None, "as_is_value_source": None}
    with pytest.raises(ValidationError, match="as_is_value and arv"):
        inputs(SizingInputs(**without))


def test_a_valuation_source_cannot_stand_without_its_value() -> None:
    """A source with no value would claim provenance for a number that is not there."""
    with pytest.raises(ValidationError, match="source cannot be recorded without its value"):
        SizingInputs(**{**NO_DRAW_DEAL.model_dump(), "arv": None})


def test_an_unstated_valuation_source_reads_as_an_adapter_value() -> None:
    """Fixtures predate the team overrides; their valuations stand in for enrichment."""
    assert NO_DRAW_DEAL.as_is_value_source is ValueSource.ADAPTER
    assert NO_DRAW_DEAL.arv_source is ValueSource.ADAPTER
    team = SizingInputs(
        **{
            **NO_DRAW_DEAL.model_dump(),
            "as_is_value_source": ValueSource.TEAM,
            "arv_source": ValueSource.TEAM,
        }
    )
    assert team.as_is_value_source is ValueSource.TEAM
    sized = size_deal(team, Tranche.T2, ExperienceTier.E2, CONFIG)
    assert sized.as_is_value_source is ValueSource.TEAM
    assert sized.arv_source is ValueSource.TEAM


def test_underwrite_inputs_bounds() -> None:
    with pytest.raises(ValidationError):
        inputs(term_months=0)
    with pytest.raises(ValidationError):
        inputs(annual_utilities_usd=D("-1"))
    with pytest.raises(ValidationError):
        inputs(extension_fee_pct=D("1.5"))
    assert inputs().stated_exit is StatedExit.UNKNOWN
    assert inputs().arv == D("165000.00") and inputs().as_is_value == D("160000.00")


# --- §3 exit inference ---------------------------------------------------------------------------


def infer(
    stated: StatedExit = StatedExit.UNKNOWN,
    asset_type: AssetType | None = AssetType.SFR,
    term: int = 6,
    product: Product = Product.NO_DRAW,
    config: Config = CONFIG,
) -> tuple[StatedExit, ExitSource]:
    return infer_exit(stated, asset_type, term, product, config)


def test_a_team_stated_exit_always_wins() -> None:
    # a stated FLIP survives a 24-month term that would otherwise infer a hold
    assert infer(stated=StatedExit.FLIP, term=24) == (StatedExit.FLIP, ExitSource.STATED)
    # and a stated HOLD survives a 3-month term on a house
    assert infer(stated=StatedExit.HOLD, term=3) == (StatedExit.HOLD, ExitSource.STATED)
    assert infer(stated=StatedExit.WHOLETAIL, term=12) == (
        StatedExit.WHOLETAIL,
        ExitSource.STATED,
    )


def test_short_term_on_a_house_or_a_two_to_four_infers_a_resale() -> None:
    assert infer(term=9, asset_type=AssetType.SFR) == (StatedExit.FLIP, ExitSource.INFERRED)
    assert infer(term=3, asset_type=AssetType.UNITS_2_4) == (StatedExit.FLIP, ExitSource.INFERRED)
    # the resale is a wholetail when that is the product (SPEC §3)
    assert infer(term=6, product=Product.WHOLETAIL) == (
        StatedExit.WHOLETAIL,
        ExitSource.INFERRED,
    )


def test_long_term_infers_a_hold_whatever_the_asset_type() -> None:
    for asset in (AssetType.SFR, AssetType.UNITS_2_4, AssetType.UNITS_5_PLUS, AssetType.OTHER):
        assert infer(term=12, asset_type=asset) == (StatedExit.HOLD, ExitSource.INFERRED)
    assert infer(term=24, asset_type=None) == (StatedExit.HOLD, ExitSource.INFERRED)


def test_nothing_is_inferred_between_the_two_boundaries_or_on_larger_assets() -> None:
    # 10 and 11 months are past the resale rule and short of the hold rule
    for term in (10, 11):
        assert infer(term=term) == (StatedExit.UNKNOWN, ExitSource.INFERRED)
    # a short term on 5+ units or an unknown asset type reaches neither rule
    assert infer(term=9, asset_type=AssetType.UNITS_5_PLUS) == (
        StatedExit.UNKNOWN,
        ExitSource.INFERRED,
    )
    assert infer(term=9, asset_type=AssetType.OTHER) == (StatedExit.UNKNOWN, ExitSource.INFERRED)
    assert infer(term=9, asset_type=None) == (StatedExit.UNKNOWN, ExitSource.INFERRED)


def test_the_term_boundaries_come_from_config_not_code() -> None:
    assert CONFIG.exit.resale_max_term_months == 9
    assert CONFIG.exit.hold_min_term_months == 12
    cfg = config_with(exit={"resale_max_term_months": 6, "hold_min_term_months": 9})
    assert infer(term=9) == (StatedExit.FLIP, ExitSource.INFERRED)  # resale on the defaults
    assert infer(term=9, config=cfg) == (StatedExit.HOLD, ExitSource.INFERRED)
    assert infer(term=6, config=cfg) == (StatedExit.FLIP, ExitSource.INFERRED)


def test_dscr_takeout_carries_the_inference_onto_the_result() -> None:
    stated = dscr_takeout(inputs(stated_exit=StatedExit.FLIP), loan(), CONFIG)
    assert (stated.type, stated.exit_source) == (StatedExit.FLIP, ExitSource.STATED)
    inferred = dscr_takeout(
        inputs(stated_exit=StatedExit.UNKNOWN, asset_type=AssetType.SFR), loan(), CONFIG
    )
    assert (inferred.type, inferred.exit_source) == (StatedExit.FLIP, ExitSource.INFERRED)
    # the takeout numbers do not move with the exit type: it runs on every deal (SPEC §8.6)
    assert inferred.max_takeout == stated.max_takeout
    assert inferred.payoff_due == stated.payoff_due
