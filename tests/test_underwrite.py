"""engine/underwrite.py: assembly, flags, recording, purity.  # SPEC §8.7"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from config.config import DEFAULT_PATH, Config, load_yaml
from engine.underwrite import downside_flags, exit_flags, underwrite
from engine.version import ENGINE_VERSION
from schema.models import (
    BorrowerInputs,
    ExperienceBucket,
    ExperienceTier,
    Flag,
    Product,
    RepeatBorrowerStatus,
    ScreenFlag,
    Severity,
    SizingInputs,
    State,
    StatedExit,
    Tranche,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
    ValueSource,
)

CONFIG = Config.load()
D = Decimal
TIGHT = D("0.000000000001")


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
        "verified_credit_score": 715,
        "verified_deals_36mo": 4,
    }
    base.update(overrides)
    return BorrowerInputs(**base)


def deal(**overrides: Any) -> SizingInputs:
    base: dict[str, Any] = {
        "product": Product.NO_DRAW,
        "purchase_price": D("150000.00"),
        "rehab_budget": D("0.00"),
        "loan_requested": D("105000.00"),
        "as_is_value": D("160000.00"),
        "arv": D("165000.00"),
    }
    base.update(overrides)
    return SizingInputs(**base)


def inputs(**overrides: Any) -> UnderwriteInputs:
    base: dict[str, Any] = {
        "deal": deal(),
        "state": State.OK,
        "borrower": borrower(),
        "term_months": 9,
        "market_rent_monthly": D("1500.00"),
        "annual_taxes_usd": D("1800.00"),
        "annual_insurance_usd": D("1200.00"),
        "annual_utilities_usd": D("600.00"),
        "stated_exit": StatedExit.FLIP,
    }
    base.update(overrides)
    return UnderwriteInputs(**base)


def codes(flags: list[Flag]) -> list[str]:
    return [f.code.value for f in flags]


def test_underwrite_assembles_the_result() -> None:
    result = underwrite(inputs(), CONFIG)
    assert result.engine_version == ENGINE_VERSION
    assert result.config_hash == CONFIG.config_hash
    assert result.term_months == 9 and result.rehab_months == 6
    assert result.sizing.credit_tranche is Tranche.T2  # from the verified 715
    assert result.sizing.experience_tier is ExperienceTier.E2  # from the verified 4 deals
    assert result.sizing.commitment == D("105000.00")
    assert result.solved_rate.quantize(D("0.000001")) == D("0.148333")
    assert result.lender_yield_at_solve.quantize(TIGHT) == CONFIG.returns.target_irr
    assert result.lender_at_solve.rate == result.solved_rate
    assert result.lender_at_solve.month == 9
    assert result.grid_lender.solved_rate == result.solved_rate
    assert result.grid_lender.months[0] == 9
    assert result.borrower_at_solve.month == 9
    assert result.borrower_at_solve.rate == result.solved_rate
    assert result.borrower_at_solve.profit.quantize(D("0.01")) == D("-15881.25")
    assert result.exit.type is StatedExit.FLIP and result.exit.refi_covers is True
    assert result.downside.passed is True
    assert result.flags == []


def test_underwrite_result_round_trips_as_json() -> None:
    result = underwrite(inputs(), CONFIG)
    again = UnderwriteResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert isinstance(again.solved_rate, Decimal)
    assert isinstance(again.downside.cover, Decimal)


def test_borrower_economics_are_information_only() -> None:
    # a losing flip raises no flag: no CoC floor in v1
    result = underwrite(inputs(), CONFIG)
    assert result.borrower_at_solve.profit < 0
    assert result.borrower_at_solve.cash_on_cash is not None
    assert result.borrower_at_solve.cash_on_cash < 0
    assert result.flags == []


def test_refi_shortfall_flag_names_the_numbers() -> None:
    result = underwrite(inputs(market_rent_monthly=D("900.00")), CONFIG)
    assert result.exit.refi_covers is False
    [flag] = result.flags
    assert flag.code is UnderwriteFlag.REFI_SHORTFALL
    assert flag.severity is Severity.SOFT  # config placeholder
    assert "75.0% LTV on ARV = $123,750.00" in flag.message
    assert "1.20x DSCR, 7.5% / 30-yr" in flag.message
    assert "$106,050.00 payoff due" in flag.message
    assert f"shortfall ${result.exit.shortfall:,.2f}" in flag.message


def test_downside_flag_names_cover_and_floor() -> None:
    small = inputs(
        deal=deal(
            purchase_price=D("80000.00"),
            loan_requested=D("60000.00"),
            as_is_value=D("85000.00"),
            arv=D("95000.00"),
        ),
        annual_taxes_usd=D("1000.00"),
        annual_insurance_usd=D("800.00"),
        term_months=6,
    )
    result = underwrite(small, CONFIG)
    assert result.downside.passed is False
    assert result.exit.refi_covers is True
    [flag] = result.flags
    assert flag.code is UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR
    assert flag.severity is Severity.HARD  # config placeholder
    assert "cover 0.93x (recovery $56,315.00 / exposure $60,600.00)" in flag.message
    assert "below the 1.00x floor" in flag.message


def test_underwrite_flag_severities_come_from_config() -> None:
    cfg = config_with(
        flags={
            "underwrite_severities": {
                "REFI_SHORTFALL": "HARD",
                "DOWNSIDE_COVER_BELOW_FLOOR": "INFO",
            }
        }
    )
    result = underwrite(inputs(market_rent_monthly=D("900.00")), cfg)
    assert [f.severity for f in result.flags] == [Severity.HARD]
    assert exit_flags(result.exit, cfg)[0].severity is Severity.HARD
    assert exit_flags(underwrite(inputs(), cfg).exit, cfg) == []
    assert downside_flags(underwrite(inputs(), cfg).downside, cfg) == []


def test_credit_and_experience_flags_carry_into_the_underwrite() -> None:
    result = underwrite(
        inputs(
            borrower=borrower(
                credit_range_self_reported=Tranche.T1,  # verified 715 is T2 -> mismatch
                verified_deals_36mo=1,  # E1 vs self-reported 3-5 -> mismatch
                repeat_borrower_self_reported=True,
                repeat_borrower_verified=RepeatBorrowerStatus.CLEAN,  # lifts E1 -> E2
            )
        ),
        CONFIG,
    )
    assert result.sizing.credit_tranche is Tranche.T2
    assert result.sizing.experience_tier is ExperienceTier.E2
    assert codes(result.flags) == [
        "CREDIT_MISMATCH",
        "EXPERIENCE_MISMATCH",
        "REPEAT_BORROWER_OVERRIDE_APPLIED",
    ]


def test_verified_credit_below_floor_is_a_hard_flag() -> None:
    result = underwrite(inputs(borrower=borrower(verified_credit_score=600)), CONFIG)
    assert result.sizing.credit_tranche is Tranche.T5
    assert result.flags[0].code is ScreenFlag.CREDIT_BELOW_FLOOR
    assert result.flags[0].severity is Severity.HARD


def test_leverage_flags_use_verified_values() -> None:
    # as-is 130,000 puts LTV at 80.8% for a 105,000 loan: over the 75% cap, past the band
    result = underwrite(inputs(deal=deal(as_is_value=D("130000.00"))), CONFIG)
    assert "LTV_AS_IS_OVER_CAP" in codes(result.flags)
    assert result.flags[0].severity is Severity.HARD
    assert result.sizing.all_pass is False


def test_flags_are_severity_ordered() -> None:
    result = underwrite(
        inputs(
            deal=deal(as_is_value=D("130000.00")),  # HARD leverage
            market_rent_monthly=D("900.00"),  # SOFT refi shortfall
            borrower=borrower(repeat_borrower_self_reported=True),  # INFO unverified repeat
        ),
        CONFIG,
    )
    severities = [f.severity for f in result.flags]
    assert severities == sorted(severities, key=[Severity.HARD, Severity.SOFT, Severity.INFO].index)
    assert Severity.HARD in severities and Severity.SOFT in severities
    assert Severity.INFO in severities


def test_extension_fee_and_exit_price_inputs_flow_through() -> None:
    result = underwrite(inputs(extension_fee_pct=D("0.01"), exit_price=D("172000.00")), CONFIG)
    assert result.borrower_at_solve.exit_price == D("172000.00")
    month_after = result.grid_lender.rows[1].cells[0]
    at_term = result.grid_lender.rows[0].cells[0]
    assert month_after.annualized_yield > at_term.annualized_yield  # extension fee charged


def test_split_principal_underwrite_uses_tranche_a_draws() -> None:
    result = underwrite(
        inputs(
            deal=deal(
                product=Product.SPLIT_PRINCIPAL,
                rehab_budget=D("60000.00"),
                loan_requested=D("170000.00"),
                as_is_value=D("230000.00"),
                arv=D("290000.00"),
            ),
            term_months=12,
            market_rent_monthly=D("2400.00"),
        ),
        CONFIG,
    )
    assert result.rehab_months == 9
    assert result.sizing.split is not None
    assert result.lender_at_solve.avg_outstanding == D("145250")
    assert result.solved_rate.quantize(D("0.000001")) == D("0.181411")
    assert result.lender_yield_at_solve.quantize(TIGHT) == CONFIG.returns.target_irr


# --- Phase 2b review decisions: the two informational flags (SPEC §8.3, §8.4, §8.7) -------------


def test_no_rehab_period_flag_when_a_split_principal_term_leaves_no_rehab() -> None:
    """A SPLIT_PRINCIPAL term at or under listing_months draws Tranche A in full at close."""
    short = inputs(
        deal=deal(
            product=Product.SPLIT_PRINCIPAL,
            purchase_price=D("120000.00"),
            rehab_budget=D("20000.00"),
            loan_requested=D("105000.00"),
            as_is_value=D("175000.00"),
            arv=D("195000.00"),
        ),
        term_months=3,
    )
    result = underwrite(short, CONFIG)
    assert result.rehab_months == 0
    flag = next(f for f in result.flags if f.code is UnderwriteFlag.NO_REHAB_PERIOD)
    assert flag.severity is Severity.INFO
    assert "3-month listing period" in flag.message
    # the draw curve is inert: the whole commitment is outstanding for the whole term
    assert result.lender_at_solve.avg_outstanding == result.sizing.commitment


def test_no_rehab_period_flag_is_silent_with_a_rehab_period_or_another_product() -> None:
    with_rehab = inputs(
        deal=deal(
            product=Product.SPLIT_PRINCIPAL,
            rehab_budget=D("60000.00"),
            loan_requested=D("170000.00"),
            as_is_value=D("230000.00"),
            arv=D("290000.00"),
        ),
        term_months=12,
        market_rent_monthly=D("2400.00"),
    )
    long_enough = underwrite(with_rehab, CONFIG)
    assert UnderwriteFlag.NO_REHAB_PERIOD not in [f.code for f in long_enough.flags]
    # a single-note product on the same short term has no Tranche A to flag
    single = underwrite(inputs(term_months=3), CONFIG)
    assert UnderwriteFlag.NO_REHAB_PERIOD not in [f.code for f in single.flags]


def test_no_rehab_period_threshold_follows_the_configured_listing_months() -> None:
    cfg = config_with(draws={"listing_months": 6})
    short = inputs(
        deal=deal(
            product=Product.SPLIT_PRINCIPAL,
            rehab_budget=D("60000.00"),
            loan_requested=D("170000.00"),
            as_is_value=D("230000.00"),
            arv=D("290000.00"),
        ),
        term_months=6,
        market_rent_monthly=D("2400.00"),
    )
    codes_at_6 = [f.code for f in underwrite(short, cfg).flags]
    assert UnderwriteFlag.NO_REHAB_PERIOD in codes_at_6
    assert UnderwriteFlag.NO_REHAB_PERIOD not in [f.code for f in underwrite(short, CONFIG).flags]


def test_solved_rate_below_grid_is_reported_as_computed_and_flagged() -> None:
    """r* = target - origination x 12 / term: 9.5% at three months, under the 10% grid floor."""
    result = underwrite(inputs(term_months=3), CONFIG)
    assert result.solved_rate.quantize(D("0.000001")) == D("0.095000")
    assert result.solved_rate < CONFIG.returns.rate_grid.min
    flag = next(f for f in result.flags if f.code is UnderwriteFlag.SOLVED_RATE_BELOW_GRID)
    assert flag.severity is Severity.INFO
    assert "10.0%" in flag.message  # names the threshold it was tested against
    assert "negative" not in flag.message
    # reported as computed: the grid carries the solved rate as its first column
    grid = result.grid_lender
    assert grid.solved_rate == result.solved_rate
    assert grid.solved_rate_inserted is True
    assert grid.rates[0] == result.solved_rate
    assert result.lender_yield_at_solve.quantize(TIGHT) == CONFIG.returns.target_irr


def test_solved_rate_below_grid_says_so_when_r_star_is_negative() -> None:
    """One month of interest cannot carry 2% of fees: r* = 0.175 - 0.24 = -6.5%."""
    result = underwrite(inputs(term_months=1), CONFIG)
    assert result.solved_rate.quantize(D("0.000001")) == D("-0.065000")
    flag = next(f for f in result.flags if f.code is UnderwriteFlag.SOLVED_RATE_BELOW_GRID)
    assert "negative" in flag.message
    assert result.lender_yield_at_solve.quantize(TIGHT) == CONFIG.returns.target_irr


def test_solved_rate_flag_is_silent_on_and_above_the_grid() -> None:
    on_grid = underwrite(inputs(term_months=9), CONFIG)  # r* 14.83%
    assert UnderwriteFlag.SOLVED_RATE_BELOW_GRID not in [f.code for f in on_grid.flags]
    above = underwrite(
        inputs(
            deal=deal(
                product=Product.SPLIT_PRINCIPAL,
                rehab_budget=D("60000.00"),
                loan_requested=D("170000.00"),
                as_is_value=D("230000.00"),
                arv=D("290000.00"),
            ),
            term_months=12,
            market_rent_monthly=D("2400.00"),
        ),
        CONFIG,
    )  # r* 18.14%, above the grid top
    assert UnderwriteFlag.SOLVED_RATE_BELOW_GRID not in [f.code for f in above.flags]


def test_informational_flags_never_change_a_verdict_input() -> None:
    """Both new codes are INFO, so nothing about them is a config severity decision."""
    result = underwrite(inputs(term_months=3), CONFIG)
    informational = {UnderwriteFlag.NO_REHAB_PERIOD, UnderwriteFlag.SOLVED_RATE_BELOW_GRID}
    for flag in result.flags:
        if flag.code in informational:
            assert flag.severity is Severity.INFO


# --- TEAM_SOURCED_VALUES on the underwrite (SPEC §6.1) -------------------------------------------


def test_the_underwrite_flags_a_hand_entered_valuation() -> None:
    team_valued = inputs(
        deal=deal().model_copy(
            update={
                "as_is_value_source": ValueSource.TEAM,
                "arv_source": ValueSource.TEAM,
            }
        )
    )
    result = underwrite(team_valued, CONFIG)
    flag = next(f for f in result.flags if f.code is ScreenFlag.TEAM_SOURCED_VALUES)
    assert flag.severity is Severity.INFO
    assert flag.message.startswith("As-is value and ARV came from the team")
    # court records are named only by the screen: the underwrite takes none (SPEC §8)
    assert "court records" not in flag.message


def test_the_underwrite_is_silent_when_the_valuation_was_pulled() -> None:
    result = underwrite(inputs(), CONFIG)
    assert ScreenFlag.TEAM_SOURCED_VALUES not in [f.code for f in result.flags]
