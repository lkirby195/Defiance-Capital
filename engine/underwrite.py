"""Underwrite: sizing on verified values, rate solve, yield grid, borrower economics,
DSCR takeout, REO downside, flags.  # SPEC §8

Pure: ``UnderwriteInputs`` and ``Config`` in, ``UnderwriteResult`` out. The caps cell comes
from the verified credit score and deal count exactly as in the screen (``credit_check``,
``experience_check``), and their flags (credit below floor, self-reported vs. verified
mismatches, repeat-borrower notes) carry into the underwrite flags alongside the leverage
flags (SPEC §8.2), the takeout shortfall, and the downside cover (SPEC §8.6). Borrower
profit and cash-on-cash are information only: no floor, no flag.
"""

from __future__ import annotations

from config.config import Config
from engine.calc.borrower import borrower_economics
from engine.calc.downside import reo_downside
from engine.calc.exit import dscr_takeout
from engine.calc.lender import lender_return
from engine.calc.outstanding import loan_terms
from engine.grids import yield_grid
from engine.screen import (
    credit_check,
    experience_check,
    leverage_flags,
    money,
    order_flags,
    pct,
)
from engine.sizing import size_deal
from engine.solve import solve_rate
from engine.version import ENGINE_VERSION
from schema.models import (
    DownsideResult,
    ExitResult,
    Flag,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
)


def exit_flags(exit_result: ExitResult, config: Config) -> list[Flag]:
    """REFI_SHORTFALL when the max takeout does not cover the payoff due.  # SPEC §8.6"""
    if exit_result.refi_covers:
        return []
    takeout = config.takeout
    return [
        Flag(
            code=UnderwriteFlag.REFI_SHORTFALL,
            severity=config.flags.underwrite_severities[UnderwriteFlag.REFI_SHORTFALL],
            message=(
                f"DSCR takeout {money(exit_result.max_takeout)} (lesser of {pct(takeout.ltv)} "
                f"LTV on ARV = {money(exit_result.ltv_takeout)} and the loan at "
                f"{takeout.dscr_floor:.2f}x DSCR, {pct(takeout.rate)} / "
                f"{takeout.amortization_years}-yr = {money(exit_result.dscr_takeout)}) does not "
                f"cover the {money(exit_result.payoff_due)} payoff due (commitment + payoff "
                f"fees); shortfall {money(exit_result.shortfall)}."
            ),
        )
    ]


def downside_flags(downside: DownsideResult, config: Config) -> list[Flag]:
    """DOWNSIDE_COVER_BELOW_FLOOR when recovery / exposure is below the floor.  # SPEC §8.6"""
    if downside.passed:
        return []
    return [
        Flag(
            code=UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR,
            severity=config.flags.underwrite_severities[UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR],
            message=(
                f"REO downside cover {downside.cover:.2f}x (recovery {money(downside.recovery)} "
                f"/ exposure {money(downside.exposure)}) is below the "
                f"{downside.cover_floor:.2f}x floor."
            ),
        )
    ]


def underwrite(inputs: UnderwriteInputs, config: Config) -> UnderwriteResult:
    """Run the Stage 2 underwrite and assemble the result.  # SPEC §8.7"""
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    sizing = size_deal(inputs.deal, credit.tranche, experience.tier, config)
    loan = loan_terms(sizing, inputs.term_months, inputs.extension_fee_pct, config)
    solved_rate = solve_rate(loan, config)
    lender_at_solve = lender_return(loan, loan.term_months, solved_rate, config)
    exit_result = dscr_takeout(inputs, loan, config)
    downside = reo_downside(inputs, loan, config)
    flags = order_flags(
        credit.flags
        + experience.flags
        + leverage_flags(sizing)
        + exit_flags(exit_result, config)
        + downside_flags(downside, config)
    )
    return UnderwriteResult(
        engine_version=ENGINE_VERSION,
        config_hash=config.config_hash,
        term_months=loan.term_months,
        rehab_months=loan.rehab_months,
        sizing=sizing,
        solved_rate=solved_rate,
        lender_yield_at_solve=lender_at_solve.annualized_yield,
        lender_at_solve=lender_at_solve,
        grid_lender=yield_grid(loan, solved_rate, config),
        borrower_at_solve=borrower_economics(inputs, loan, loan.term_months, solved_rate, config),
        exit=exit_result,
        downside=downside,
        flags=flags,
    )
