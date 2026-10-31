"""Underwrite: sizing on verified values, the monthly ledger and its XIRR, and the three
analyses.  # SPEC §8

Pure: ``UnderwriteInputs`` and ``Config`` in, ``UnderwriteResult`` out. The caps cell comes
from the verified credit score and deal count exactly as in the screen (``credit_check``,
``experience_check``), and their flags - credit below floor, self-reported vs. verified
mismatches, repeat-borrower notes - carry into the underwrite flags alongside the leverage
flags (SPEC §8.2), the rental's two flags and the take-back's (SPEC §8.5, §8.6) and the
borrower's cash-on-cash flag (SPEC §8.4).

The order below is the order the math runs and the order every output shows it (SPEC §9):
size the deal, resolve the §8.1 economics, lay out the ledger, re-run it across the
Lender and Borrower Sensitivity grids (SPEC §8.9) and the pricing matrix (SPEC §8.10), then
ask the three questions the ledger makes answerable - sell it, let it, or own it - on every
deal (there are no toggles from engine 2.0.0), and lay the money out as Sources & Uses
(SPEC §8.11).

The SPEC §7.2 court and filing tests run again here, on whatever source is in force at
underwrite time - adapter over team, the same precedence as the screen (SPEC §6.1). They are
not copied from the screens row: weeks can pass between Stage 1 and Stage 2, and a pull that
has since landed supersedes the hand search the screen ran on. The stored underwrite is
therefore self-contained, which is what the credit memo (SPEC §9.3) needs.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.borrower import borrower_sensitivity_table
from engine.calc.flip import flip_analysis
from engine.calc.ledger import return_overview
from engine.calc.pricing import pricing_matrix
from engine.calc.rental import rental_analysis
from engine.calc.sensitivity import Grid, sensitivity_table
from engine.calc.sources_uses import sources_and_uses
from engine.calc.take_back import take_back_analysis
from engine.calc.terms import LoanTerms, loan_terms
from engine.screen import (
    court_flags,
    credit_check,
    experience_check,
    leverage_flags,
    market_data_flags,
    money,
    order_flags,
    pct,
    provenance_flags,
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
    State,
    TakeBackAnalysis,
    UnderwriteFlag,
    UnderwriteInputs,
    UnderwriteResult,
)

ZERO = Decimal(0)


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


def rent_flags(rental: RentalAnalysis) -> list[Flag]:
    """MONTHLY_RENT_MISSING when there is no market rent.  # SPEC §8.5, §8.6, §8.8

    One flag for both analyses, fixed INFO. Nothing stands in for what a property lets for,
    so the lines that read the market rent - the Rental's gap, its two DSCRs and its rent
    test, the Take-Back's required sale price at the market rent - are NOT_EVALUATED rather
    than computed on a zero, which would report a shortfall the deal has not been shown to
    have. Everything else in both analyses runs without it.
    """
    if rental.market_rent is not None:
        return []
    required = (
        f"The {money(rental.required_rent)} required rent and the "
        f"{money(rental.spread_to_loan)} spread to loan are reported regardless"
        if rental.required_rent is not None and rental.spread_to_loan is not None
        else "The takeout itself is not sized either, for want of an exit value"
    )
    return [
        Flag(
            code=UnderwriteFlag.MONTHLY_RENT_MISSING,
            severity=Severity.INFO,
            message=(
                "No market rent on the deal, so the Rental analysis's market-rent lines - the "
                "gap, the two DSCRs at the market rent and the rent test - and the Take-Back's "
                "required sale price at the market rent were not evaluated: nothing stands in "
                f"for what a property lets for. {required}; enter a rent and re-run for the "
                "rest."
            ),
        )
    ]


def sale_price_flags(flip: FlipAnalysis, rental: RentalAnalysis) -> list[Flag]:
    """SALE_PRICE_MISSING when the flip had no price to sell at.  # SPEC §8.4, §8.8

    Fixed INFO, and the sibling of MONTHLY_RENT_MISSING: the underwrite runs without an
    estimated sale price, and what it costs is the figures the sale price feeds - the flip's
    four, the Take-Back's solves (its haircut and its bounds are set against the projected
    sale price) and, unless the team typed an exit value, the Rental's takeout. The whole
    cost stack is still reported, so the flag says what was lost rather than that something
    failed - a flip nobody could price has not lost money. The screen's own
    ESTIMATED_SALE_PRICE_MISSING (SOFT) says the leverage side of the same absence.
    """
    if flip.status is not AnalysisStatus.NOT_EVALUATED:
        return []
    takeout = (
        "The Rental's takeout is sized on the exit value the team entered."
        if rental.status is AnalysisStatus.EVALUATED
        else "The Rental's exit value defaults to it, so the takeout is not sized either."
    )
    return [
        Flag(
            code=UnderwriteFlag.SALE_PRICE_MISSING,
            severity=Severity.INFO,
            message=(
                "No estimated sale price on the deal, so the Flip analysis was not "
                f"evaluated: the {money(flip.total_costs)} cost stack is reported, and the "
                "broker's cut, the net profit and the yield are not. LTV is not computed "
                "either (SPEC §7.4), and the Take-Back's haircut and hold-and-rent solves are "
                f"set against the projected sale price, so they did not run. {takeout} Enter "
                "a price and re-run for all of them."
            ),
        )
    ]


def foreclosure_flags(take_back: TakeBackAnalysis) -> list[Flag]:
    """FORECLOSURE_MONTHS_MISSING when config has no timeline for the deal's own state.

    # SPEC §8.6, §8.8. Fixed INFO. A state outside the footprint has no entry of its own:
    config's ``OTHER`` entry stands in and the analysis runs on it, and the flag says so
    (v0.20); without an ``OTHER`` entry either, the take-back is not evaluated. A deal in a
    served state raises nothing.
    """
    months = take_back.foreclosure_months
    if months is not None and take_back.foreclosure_months_from is not State.OTHER:
        return []
    if months is not None and take_back.state is not State.OTHER:  # pragma: no cover
        return []  # a served state read its own entry (the key is the state itself)
    if months is not None:
        return [
            Flag(
                code=UnderwriteFlag.FORECLOSURE_MONTHS_MISSING,
                severity=Severity.INFO,
                message=(
                    f"No foreclosure months in config for {enum_label(take_back.state)}, so the "
                    f"OTHER placeholder stood in: the Take-Back analysis ran on {months} "
                    "months of foreclosure carry (take_back.foreclosure_months.OTHER). Confirm "
                    "the timeline for this state before relying on it."
                ),
            )
        ]
    return [
        Flag(
            code=UnderwriteFlag.FORECLOSURE_MONTHS_MISSING,
            severity=Severity.INFO,
            message=(
                f"No foreclosure months in config for {enum_label(take_back.state)}, so the "
                "Take-Back analysis was not evaluated: the lender's timeline needs how long "
                "it carries the property before it can sell or let it. The default month, "
                f"the {money(take_back.lost_interest)} of lost interest and the "
                f"{money(take_back.legal_costs)} legal bill are reported regardless."
            ),
        )
    ]


def cash_on_cash_flags(flip: FlipAnalysis, config: Config) -> list[Flag]:
    """BORROWER_COC_BELOW_TARGET when the flip's cash-on-cash is under the target.

    # SPEC §8.4, §8.8. Severity from config (placeholder SOFT). Not tested without a sale
    price - there is no profit - nor when the loan covers every cost, where there is no
    equity to earn a return on.
    """
    if flip.cash_on_cash_passed is not False:
        return []
    assert flip.cash_on_cash is not None and flip.cash_on_cash_target is not None
    assert flip.net_profit is not None and flip.borrower_equity is not None
    assert flip.loan_amount is not None
    return [
        Flag(
            code=UnderwriteFlag.BORROWER_COC_BELOW_TARGET,
            severity=config.flags.underwrite_severities[UnderwriteFlag.BORROWER_COC_BELOW_TARGET],
            message=(
                f"Borrower cash-on-cash {pct(flip.cash_on_cash)} is below the "
                f"{pct(flip.cash_on_cash_target)} target: {money(flip.net_profit)} profit on "
                f"{money(flip.borrower_equity)} of equity (the {money(flip.total_costs)} total "
                f"deal cost less the {money(flip.loan_amount)} loan)."
            ),
        )
    ]


def rental_flags(rental: RentalAnalysis, config: Config) -> list[Flag]:
    """The rental's two tests: the market rent against the required rent, and the takeout
    loan against our payoff due.  # SPEC §8.5, §8.8

    MARKET_RENT_BELOW_REQUIRED (severity from config, placeholder Soft) when the market rent
    is under the rent the takeout needs; not tested without a market rent. TAKEOUT_BELOW_PAYOFF
    (config, placeholder Soft) when the takeout loan is short of what this loan collects at
    payoff; it needs only the exit value.
    """
    flags: list[Flag] = []
    severities = config.flags.underwrite_severities
    if rental.rent_passed is False:
        assert rental.market_rent is not None and rental.required_rent is not None
        assert rental.rent_gap is not None and rental.dscr_amortizing is not None
        assert rental.takeout_loan is not None and rental.debt_service_amortizing is not None
        flags.append(
            Flag(
                code=UnderwriteFlag.MARKET_RENT_BELOW_REQUIRED,
                severity=severities[UnderwriteFlag.MARKET_RENT_BELOW_REQUIRED],
                message=(
                    f"Market rent {money(rental.market_rent)} is below the "
                    f"{money(rental.required_rent)} the takeout requires, a gap of "
                    f"{money(-rental.rent_gap)}: a {money(rental.takeout_loan)} takeout "
                    f"({pct(rental.takeout_ltv)} of the {money(rental.exit_value or ZERO)} "
                    f"exit value) at {pct(rental.takeout_rate)} over "
                    f"{rental.amortization_years} years costs "
                    f"{money(rental.debt_service_amortizing)} a month, and at the "
                    f"{rental.target_dscr:.2f}x target DSCR needs "
                    f"{money(rental.required_noi or ZERO)} of NOI; the market rent nets "
                    f"{money(rental.market_noi or ZERO)} after {pct(rental.expenses_pct)} "
                    f"expenses, a {rental.dscr_amortizing:.2f}x DSCR amortizing."
                ),
            )
        )
    if rental.spread_passed is False:
        assert rental.takeout_loan is not None and rental.payoff_due is not None
        assert rental.spread_to_loan is not None
        flags.append(
            Flag(
                code=UnderwriteFlag.TAKEOUT_BELOW_PAYOFF,
                severity=severities[UnderwriteFlag.TAKEOUT_BELOW_PAYOFF],
                message=(
                    f"The {money(rental.takeout_loan)} takeout loan is "
                    f"{money(-rental.spread_to_loan)} short of the {money(rental.payoff_due)} "
                    "payoff due on this loan (the "
                    "commitment and the payoff half of the origination fee): a refinance "
                    "would not take us out."
                ),
            )
        )
    return flags


def take_back_flags(take_back: TakeBackAnalysis, config: Config) -> list[Flag]:
    """TAKE_BACK_PRICE_ABOVE_PURCHASE when the lender needs more than the house cost.

    # SPEC §8.6, §8.8. Severity from config (placeholder Hard). Tested only when the haircut
    price was solved.
    """
    if take_back.price_above_purchase is not True:
        return []
    assert take_back.haircut_price is not None and take_back.haircut_price.value is not None
    assert take_back.projected_sale_price is not None and take_back.haircut_pct is not None
    assert take_back.price_pct_of_purchase is not None
    return [
        Flag(
            code=UnderwriteFlag.TAKE_BACK_PRICE_ABOVE_PURCHASE,
            severity=config.flags.underwrite_severities[
                UnderwriteFlag.TAKE_BACK_PRICE_ABOVE_PURCHASE
            ],
            message=(
                f"The take-back needs a {money(take_back.haircut_price.value)} sale "
                f"({pct(take_back.price_pct_of_purchase)} of the "
                f"{money(take_back.purchase_price)} purchase price, a "
                f"{pct(take_back.haircut_pct)} haircut from the "
                f"{money(take_back.projected_sale_price)} projected sale price) for the "
                f"lender's XIRR to reach {pct(take_back.target_irr)} after a default in month "
                f"{take_back.default_month}, {take_back.lost_interest_months} months' lost "
                f"interest, {money(take_back.legal_costs)} of legal costs and "
                f"{take_back.foreclosure_months} months' foreclosure carry: it needs more than "
                "the house cost to come out whole."
            ),
        )
    ]


def underwrite(inputs: UnderwriteInputs, config: Config) -> UnderwriteResult:
    """Run the Stage 2 underwrite and assemble the result.  # SPEC §8.7"""
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    sizing = size_deal(inputs.deal, credit.tranche, experience.tier, config)
    loan = loan_terms(sizing, inputs, config)
    overview = return_overview(loan)
    # Every cell of the grid re-sizes and re-ledgers the deal in the same caps cell (SPEC §8.9).
    # The three tables read one set of ledgers, each laid out once (SPEC §8.9, §8.10).
    grid = Grid(inputs, credit.tranche, experience.tier, config)
    sensitivity = sensitivity_table(inputs, credit.tranche, experience.tier, config, grid)
    borrower = borrower_sensitivity_table(inputs, credit.tranche, experience.tier, config, grid)
    pricing = pricing_matrix(inputs, credit.tranche, experience.tier, config, grid)
    flip = flip_analysis(inputs, loan, overview, config)
    rental = rental_analysis(inputs, loan, config)
    take_back = take_back_analysis(inputs, loan, config)
    flags = order_flags(
        credit.flags
        + experience.flags
        + leverage_flags(sizing)
        + court_flags(inputs.court_records, config)
        + provenance_flags(sizing, inputs.court_records, inputs.monthly_rent_source)
        + market_data_flags(inputs.deal, inputs.market_data, config)
        + structure_flags(loan, config)
        + sale_price_flags(flip, rental)
        + cash_on_cash_flags(flip, config)
        + rent_flags(rental)
        + foreclosure_flags(take_back)
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
        sizing=sizing,
        economics=deal_economics(loan),
        return_overview=overview,
        sensitivity=sensitivity,
        borrower_sensitivity=borrower,
        pricing=pricing,
        sources_and_uses=sources_and_uses(loan, flip),
        flip=flip,
        rental=rental,
        take_back=take_back,
        flags=flags,
    )
