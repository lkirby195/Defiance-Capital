"""The two SPEC §8.1 inputs that stopped being required.  # SPEC §8.1, §8.6

Both used to refuse the run outright; neither does now, and they stopped for different
reasons, which is the whole of what is tested here.

**Annual utilities** joined the taxes and insurance beside it: utilities are a cost the
property incurs whatever it is worth, so a percentage of the as-is value is the same kind of
stand-in as theirs and the source says `DEFAULT`.

**Market rent** did not, and deliberately. Nothing is a percentage of what a property lets
for. So a deal without one is underwritten with the DSCR takeout `NOT_EVALUATED` — every
figure the rent feeds is None, `refi_covers` included — and an INFO `MARKET_RENT_MISSING`
instead of a `REFI_SHORTFALL` it has not been shown to have. The tests below are mostly about
the difference between *not evaluated* and *failed*.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from engine.calc.borrower import (
    monthly_holding_cost,
    resolve_annual_insurance,
    resolve_annual_taxes,
    resolve_annual_utilities,
)
from engine.underwrite import underwrite
from schema.models import (
    GRADED_UNDERWRITE_FLAGS,
    OpexSource,
    TakeoutStatus,
    UnderwriteFlag,
)
from tests.test_underwrite import CONFIG, codes, inputs

D = Decimal


def severity_of(result: Any, code: UnderwriteFlag) -> Any:
    return next(flag.severity for flag in result.flags if flag.code is code)


def message_of(result: Any, code: UnderwriteFlag) -> str:
    text: str = next(flag.message for flag in result.flags if flag.code is code)
    return text


# --- annual utilities: a config default like the two beside it ----------------------------------


def test_utilities_default_to_a_percentage_of_the_as_is_value() -> None:
    """The same shape as taxes and insurance, from a key of its own.  # SPEC §8.6"""
    pct = CONFIG.takeout.opex_defaults.utilities_pct_of_as_is_value
    assert pct > 0, "the placeholder is still a real percentage"
    without = inputs(annual_utilities_usd=None)
    value, source = resolve_annual_utilities(without, CONFIG)
    assert source is OpexSource.DEFAULT
    assert value == without.as_is_value * pct


def test_a_team_figure_beats_the_default() -> None:
    value, source = resolve_annual_utilities(inputs(annual_utilities_usd=D("600.00")), CONFIG)
    assert source is OpexSource.ACTUAL and value == D("600.00")


def test_the_three_opex_lines_resolve_the_same_way() -> None:
    """One rule, three lines; a reader should not have to remember which is special."""
    bare = inputs(annual_taxes_usd=None, annual_insurance_usd=None, annual_utilities_usd=None)
    for resolve, pct in (
        (resolve_annual_taxes, CONFIG.takeout.opex_defaults.taxes_pct_of_as_is_value),
        (resolve_annual_insurance, CONFIG.takeout.opex_defaults.insurance_pct_of_as_is_value),
        (resolve_annual_utilities, CONFIG.takeout.opex_defaults.utilities_pct_of_as_is_value),
    ):
        value, source = resolve(bare, CONFIG)
        assert source is OpexSource.DEFAULT
        assert value == bare.as_is_value * pct


def test_the_holding_cost_carries_the_defaulted_utilities() -> None:
    """It is not silently dropped: the REO carry and the borrower's costs both include it."""
    with_team = monthly_holding_cost(inputs(annual_utilities_usd=D("600.00")), CONFIG)
    defaulted = monthly_holding_cost(inputs(annual_utilities_usd=None), CONFIG)
    default_annual = (
        inputs().as_is_value * CONFIG.takeout.opex_defaults.utilities_pct_of_as_is_value
    )
    assert defaulted == with_team - D("600.00") / 12 + default_annual / 12


def test_a_deal_with_no_utilities_still_underwrites() -> None:
    result = underwrite(inputs(annual_utilities_usd=None), CONFIG)
    assert result.downside.monthly_holding_cost > 0
    assert UnderwriteFlag.MARKET_RENT_MISSING not in [flag.code for flag in result.flags]


# --- market rent: no default, and no takeout without one ----------------------------------------


def test_no_rent_leaves_the_takeout_not_evaluated() -> None:
    result = underwrite(inputs(market_rent_monthly=None), CONFIG)
    takeout = result.exit
    assert takeout.status is TakeoutStatus.NOT_EVALUATED
    for name in (
        "gross_rent_annual",
        "opex_annual",
        "noi_annual",
        "dscr_takeout",
        "max_takeout",
        "dscr_at_payoff",
        "shortfall",
    ):
        assert getattr(takeout, name) is None, name


def test_refi_covers_is_none_rather_than_false() -> None:
    """The distinction the whole change exists for: unknown is not a failure."""
    unknown = underwrite(inputs(market_rent_monthly=None), CONFIG).exit
    assert unknown.refi_covers is None
    assert unknown.refi_covers is not False

    # a rent so low the takeout genuinely cannot cover the payoff is False, not None
    short = underwrite(inputs(market_rent_monthly=D("1.00")), CONFIG).exit
    assert short.status is TakeoutStatus.EVALUATED
    assert short.refi_covers is False


def test_what_does_not_depend_on_the_rent_is_still_reported() -> None:
    """A reader still wants the exit, the opex, the LTV takeout and what is due."""
    takeout = underwrite(inputs(market_rent_monthly=None), CONFIG).exit
    priced = underwrite(inputs(), CONFIG).exit
    assert takeout.type is priced.type and takeout.exit_source is priced.exit_source
    assert takeout.annual_taxes == priced.annual_taxes
    assert takeout.annual_insurance == priced.annual_insurance
    assert takeout.ltv_takeout == priced.ltv_takeout > 0
    assert takeout.payoff_due == priced.payoff_due > 0


def test_the_missing_rent_flag_replaces_the_shortfall_flag() -> None:
    result = underwrite(inputs(market_rent_monthly=None), CONFIG)
    assert UnderwriteFlag.MARKET_RENT_MISSING.value in codes(result.flags)
    assert UnderwriteFlag.REFI_SHORTFALL.value not in codes(result.flags)
    assert severity_of(result, UnderwriteFlag.MARKET_RENT_MISSING).value == "INFO"
    message = message_of(result, UnderwriteFlag.MARKET_RENT_MISSING)
    assert "not evaluated" in message
    assert "nothing stands in" in message
    assert "enter a rent and re-run" in message


def test_the_flag_is_fixed_info_and_config_cannot_grade_it() -> None:
    """Like the other two informational codes (SPEC §8.7): severity is not a lender tunable."""
    assert UnderwriteFlag.MARKET_RENT_MISSING not in GRADED_UNDERWRITE_FLAGS
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    data["flags"]["underwrite_severities"]["MARKET_RENT_MISSING"] = "HARD"
    with pytest.raises(ConfigError, match="MARKET_RENT_MISSING"):
        Config.from_dict(data)


# --- the rest of the underwrite is untouched ----------------------------------------------------


def test_everything_but_the_takeout_is_the_same_without_a_rent() -> None:
    """The rent feeds the takeout and nothing else: no pricing moves when it is absent."""
    priced = underwrite(inputs(), CONFIG)
    without = underwrite(inputs(market_rent_monthly=None), CONFIG)
    assert without.solved_rate == priced.solved_rate
    assert without.sizing.commitment == priced.sizing.commitment
    assert without.borrower_at_solve.profit == priced.borrower_at_solve.profit
    assert without.downside.cover == priced.downside.cover
