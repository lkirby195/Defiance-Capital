"""Underwrite: sizing on verified values, the monthly ledger and its XIRR, and the three
analyses.  # SPEC §8

Pure: ``UnderwriteInputs`` and ``Config`` in, ``UnderwriteResult`` out. The caps cell comes
from the verified credit score and deal count exactly as in the screen (``credit_check``,
``experience_check``), and their flags - credit below floor, self-reported vs. verified
mismatches, repeat-borrower notes - carry into the underwrite flags alongside the leverage
flags (SPEC §8.2) and the two DSCR flags (SPEC §8.5, §8.6).

The order below is the order the math runs and the order every output shows it (SPEC §9):
size the deal, resolve the §8.1 economics, lay out the ledger, then ask the three questions
the ledger makes answerable - sell it, let it, or own it.

The SPEC §7.2 court and filing tests run again here, on whatever source is in force at
underwrite time - adapter over team, the same precedence as the screen (SPEC §6.1). They are
not copied from the screens row: weeks can pass between Stage 1 and Stage 2, and a pull that
has since landed supersedes the hand search the screen ran on. The stored underwrite is
therefore self-contained, which is what the credit memo (SPEC §9.3) needs.
"""

from __future__ import annotations

from config.config import Config
from engine.calc.exit import exit_inference
from engine.calc.flip import flip_analysis
from engine.calc.ledger import return_overview
from engine.calc.rental import rental_analysis, take_back_analysis
from engine.calc.terms import LoanTerms, loan_terms
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
from engine.version import ENGINE_VERSION
from schema.labels import enum_label
from schema.models import (
    SPLIT_PRODUCTS,
    AnalysisStatus,
    DealEconomics,
    Flag,
    FlipAnalysis,
    RentalAnalysis,
    Severity,
    TakeBackAnalysis,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
)


def deal_economics(loan: LoanTerms) -> DealEconomics:
    """The §8.1 Deal Economics group as the run resolved it."""
    sizing = loan.sizing
    split = sizing.split
    return DealEconomics(
        purchase_price=sizing.purchase_price,
        rehab_costs=sizing.rehab_costs,
        contingency_pct=sizing.contingency_pct,
        contingency=sizing.rehab_adj - sizing.rehab_costs,
        rehab_adj=sizing.rehab_adj,
        closing_costs=sizing.closing_costs,
        holding_costs_pct_of_cost=loan.holding_costs_pct_of_cost,
        holding_costs_basis=loan.holding_costs_basis,
        holding_costs_total=loan.holding_costs_total,
        holding_costs_monthly=loan.holding_costs_monthly,
        origination_fee_pct=loan.origination_fee_pct,
        origination_at_close=loan.origination_at_close,
        origination_at_payoff=loan.origination_at_payoff,
        interest_rate=loan.interest_rate,
        loan_requested=sizing.loan_requested,
        commitment=sizing.commitment,
        funded_at_close=loan.funded_at_close,
        loan_purchase_portion=split.purchase_portion if split is not None else None,
        loan_rehab_portion=split.rehab_portion if split is not None else None,
    )


def structure_flags(loan: LoanTerms, config: Config) -> list[Flag]:
    """NO_REHAB_PERIOD when the term leaves no rehab period to draw over.  # SPEC §8.3, §8.8

    Fixed INFO: the deal is still sized and priced normally, but the rehab money goes out at
    close instead of month by month, so the lender is exposed to all of it from day one and
    the two-part structure buys nobody anything. The message names the listing-months
    threshold it was tested against.
    """
    if loan.product not in SPLIT_PRODUCTS or loan.rehab_months > 0 or loan.rehab_portion <= 0:
        return []
    return [
        Flag(
            code=UnderwriteFlag.NO_REHAB_PERIOD,
            severity=Severity.INFO,
            message=(
                f"{enum_label(loan.product)} term of {loan.term_description} is at or inside "
                f"the {config.draws.listing_months}-month listing period, so there is no "
                f"rehab period: the {money(loan.rehab_portion)} rehab portion is advanced at "
                "close rather than drawn, and no draw is scheduled."
            ),
        )
    ]


def rent_flags(rental: RentalAnalysis, take_back: TakeBackAnalysis) -> list[Flag]:
    """MONTHLY_RENT_MISSING when neither DSCR could be computed.  # SPEC §8.5, §8.6, §8.8

    One flag for both analyses, fixed INFO. It replaces the two DSCR tests rather than
    joining them: with no rent there is no net monthly income, so there is no DSCR that could
    fall short, and reporting one would be reporting a failure the deal has not been shown to
    have. The Take-Back analysis is what makes this unconditional - it runs on every deal, so
    a missing rent always costs the run something, whatever the Rental toggle says.
    """
    if take_back.status is not AnalysisStatus.NOT_EVALUATED:
        return []
    not_evaluated = (
        "the Take-Back analysis was not evaluated (the Rental analysis is off in any case)"
        if rental.status is AnalysisStatus.OFF
        else "neither the Rental nor the Take-Back analysis was evaluated"
    )
    return [
        Flag(
            code=UnderwriteFlag.MONTHLY_RENT_MISSING,
            severity=Severity.INFO,
            message=(
                f"No monthly rent on the deal, so {not_evaluated}: there is no net monthly "
                "income to cover a debt service with, and nothing stands in for what a "
                f"property lets for. The {money(take_back.total_cost)} take-back cost and its "
                f"{money(take_back.debt_service_monthly)} monthly debt service are reported "
                "regardless; enter a rent and re-run to test either DSCR."
            ),
        )
    ]


def sale_price_flags(flip: FlipAnalysis) -> list[Flag]:
    """SALE_PRICE_MISSING when the flip had no price to sell at.  # SPEC §8.4, §8.8

    Fixed INFO, and the sibling of MONTHLY_RENT_MISSING: the underwrite runs without an
    estimated sale price, and what it costs is the four figures the sale price feeds. The
    whole cost stack is still reported, so the flag says what was lost rather than that
    something failed - a flip nobody could price has not lost money.

    It is raised only where the flip was asked for. A toggle somebody turned off is a
    decision, not a gap, and the screen's own ESTIMATED_SALE_PRICE_MISSING (SOFT) already
    says the leverage side of the same absence whatever the toggle is doing.
    """
    if flip.status is not AnalysisStatus.NOT_EVALUATED:
        return []
    return [
        Flag(
            code=UnderwriteFlag.SALE_PRICE_MISSING,
            severity=Severity.INFO,
            message=(
                "No estimated sale price on the deal, so the Flip analysis was not "
                f"evaluated: the {money(flip.total_costs)} cost stack is reported, and the "
                "broker's cut, the net profit and the yield are not. LTV is not computed "
                "either (SPEC §7.4). Enter a price and re-run to get all four."
            ),
        )
    ]


def rental_flags(rental: RentalAnalysis, config: Config) -> list[Flag]:
    """DSCR_BELOW_FLOOR when the rent does not carry a takeout loan.  # SPEC §8.5, §8.8"""
    if rental.status is not AnalysisStatus.EVALUATED or rental.passed:
        return []
    assert rental.dscr is not None and rental.net_monthly_income is not None
    return [
        Flag(
            code=UnderwriteFlag.DSCR_BELOW_FLOOR,
            severity=config.flags.underwrite_severities[UnderwriteFlag.DSCR_BELOW_FLOOR],
            message=(
                f"Rental DSCR {rental.dscr:.2f}x is below the "
                f"{rental.dscr_floor:.2f}x floor: net monthly income "
                f"{money(rental.net_monthly_income)} against "
                f"{money(rental.debt_service_monthly)} of monthly debt service on the "
                f"{money(rental.loan_amount)} commitment at {pct(rental.takeout_rate)} over "
                f"{rental.amortization_years} years."
            ),
        )
    ]


def take_back_flags(take_back: TakeBackAnalysis, config: Config) -> list[Flag]:
    """TAKE_BACK_DSCR_BELOW_FLOOR when the rent does not carry GLENWOOD's own cost.

    # SPEC §8.6, §8.8
    """
    if take_back.status is not AnalysisStatus.EVALUATED or take_back.passed:
        return []
    assert take_back.dscr is not None and take_back.net_monthly_income is not None
    return [
        Flag(
            code=UnderwriteFlag.TAKE_BACK_DSCR_BELOW_FLOOR,
            severity=config.flags.underwrite_severities[UnderwriteFlag.TAKE_BACK_DSCR_BELOW_FLOOR],
            message=(
                f"Take-back DSCR {take_back.dscr:.2f}x is below the "
                f"{take_back.dscr_floor:.2f}x floor: net monthly income "
                f"{money(take_back.net_monthly_income)} against "
                f"{money(take_back.debt_service_monthly)} of monthly debt service on a "
                f"{money(take_back.total_cost)} take-back cost (commitment "
                f"{money(take_back.loan_amount)} + {take_back.lost_interest_months} months' "
                f"lost interest {money(take_back.lost_interest)} + legal costs "
                f"{money(take_back.legal_costs)}) at {pct(take_back.interest_rate)} over "
                f"{take_back.amortization_years} years."
            ),
        )
    ]


def underwrite(inputs: UnderwriteInputs, config: Config) -> UnderwriteResult:
    """Run the Stage 2 underwrite and assemble the result.  # SPEC §8.7"""
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    sizing = size_deal(inputs.deal, credit.tranche, experience.tier, config)
    loan = loan_terms(sizing, inputs, config)
    exit_result = exit_inference(inputs, config)
    overview = return_overview(loan)
    flip = flip_analysis(inputs, loan, overview, exit_result.flip_analysis, config)
    rental = rental_analysis(inputs, loan, exit_result.rental_analysis, config)
    take_back = take_back_analysis(inputs, loan, config)
    flags = order_flags(
        credit.flags
        + experience.flags
        + leverage_flags(sizing)
        + court_flags(inputs.court_records, config)
        + team_sourced_flags(sizing, inputs.court_records)
        + structure_flags(loan, config)
        + sale_price_flags(flip)
        + rent_flags(rental, take_back)
        + rental_flags(rental, config)
        + take_back_flags(take_back, config)
    )
    return UnderwriteResult(
        engine_version=ENGINE_VERSION,
        config_hash=config.config_hash,
        loan_purpose=inputs.loan_purpose,
        closing_date=loan.closing_date,
        payoff_date=loan.payoff_date,
        term_months=loan.term_months,
        term_stub_days=loan.stub_days,
        term_months_decimal=loan.term_months_decimal,
        rehab_months=loan.rehab_months,
        exit=exit_result,
        sizing=sizing,
        economics=deal_economics(loan),
        return_overview=overview,
        flip=flip,
        rental=rental,
        take_back=take_back,
        flags=flags,
    )
