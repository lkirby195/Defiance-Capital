"""Underwrite: sizing on verified values, rate solve, yield grid, borrower economics,
DSCR takeout, REO downside, flags.  # SPEC §8

Pure: ``UnderwriteInputs`` and ``Config`` in, ``UnderwriteResult`` out. The caps cell comes
from the verified credit score and deal count exactly as in the screen (``credit_check``,
``experience_check``), and their flags (credit below floor, self-reported vs. verified
mismatches, repeat-borrower notes) carry into the underwrite flags alongside the leverage
flags (SPEC §8.2), the takeout shortfall, and the downside cover (SPEC §8.6). Borrower
profit and cash-on-cash are information only: no floor, no flag.

The SPEC §7.2 court and filing tests run again here, on whatever source is in force at
underwrite time - adapter over team, the same precedence as the screen (SPEC §6.1). They are
not copied from the screens row: weeks can pass between Stage 1 and Stage 2, and a pull that
has since landed supersedes the hand search the screen ran on. The stored underwrite is
therefore self-contained, which is what the credit memo (SPEC §9.2) needs.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.borrower import borrower_economics
from engine.calc.downside import reo_downside
from engine.calc.exit import dscr_takeout
from engine.calc.lender import lender_return
from engine.calc.outstanding import LoanTerms, loan_terms
from engine.grids import yield_grid
from engine.screen import (
    court_flags,
    credit_check,
    experience_check,
    leverage_flags,
    money,
    order_flags,
    pct,
    team_sourced_flags,
)
from engine.sizing import size_deal
from engine.solve import solve_rate
from engine.version import ENGINE_VERSION
from schema.models import (
    DownsideResult,
    ExitResult,
    Flag,
    Product,
    Severity,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
)


def structure_flags(loan: LoanTerms, config: Config) -> list[Flag]:
    """NO_REHAB_PERIOD when a SPLIT_PRINCIPAL term leaves no rehab period.  # SPEC §8.3, §8.7

    Fixed INFO: the deal is still sized and priced normally, but Tranche A is fully drawn
    from close, so the draw curve buys the borrower nothing and the two-note structure is
    worth a second look. The message names the listing-months threshold it was tested
    against.
    """
    if loan.product is not Product.SPLIT_PRINCIPAL or loan.rehab_months > 0:
        return []
    return [
        Flag(
            code=UnderwriteFlag.NO_REHAB_PERIOD,
            severity=Severity.INFO,
            message=(
                f"SPLIT_PRINCIPAL term of {loan.term_months} month(s) is at or below the "
                f"{config.draws.listing_months}-month listing period, so there is no rehab "
                f"period: Tranche A is fully drawn from close and its average utilization "
                f"({pct(config.draws.draw_avg_utilization)}) never applies."
            ),
        )
    ]


def solve_flags(solved_rate: Decimal, config: Config) -> list[Flag]:
    """SOLVED_RATE_BELOW_GRID when r* lands under the configured grid.  # SPEC §8.4, §8.7

    Fixed INFO: r* is reported as computed and inserted into the grid in rate order, below
    the first configured column. On a short enough term the fees alone clear the target
    income and r* comes out negative; the message says so.
    """
    floor = config.returns.rate_grid.min
    if solved_rate >= floor:
        return []
    negative = (
        " Fees alone exceed the target income at this term, so the solved rate is negative."
        if solved_rate < 0
        else ""
    )
    return [
        Flag(
            code=UnderwriteFlag.SOLVED_RATE_BELOW_GRID,
            severity=Severity.INFO,
            message=(
                f"Solved rate {pct(solved_rate)} is below the {pct(floor)} rate-grid minimum; "
                f"it is reported as computed and inserted as the first grid column.{negative}"
            ),
        )
    ]


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
        + court_flags(inputs.court_records, config)
        + team_sourced_flags(sizing, inputs.court_records)
        + structure_flags(loan, config)
        + solve_flags(solved_rate, config)
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
