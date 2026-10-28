"""Render a screen and an underwrite as plain text for the terminal.  # SPEC §9.1, §9.6

For manual verification of the math, not a client deliverable: every number the engine
produced is shown at the precision it was computed to, next to the threshold it was tested
against. Pure - text in, text out, no I/O - so the CLI stays a thin wrapper and the layout
can be asserted in a test.

The underwrite prints the SPEC §9 sections in order - Deal Economics, Return Overview,
Lender Sensitivity, Borrower Sensitivity, Pricing, Flip, Rental, Take-Back, Sources & Uses,
Flags - which is the order the workbook writes, so a person checking one against the other
reads down the same list.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal

from config.config import Config
from schema.models import (
    SPLIT_PRODUCTS,
    AnalysisStatus,
    BorrowerSensitivityTable,
    BoundHit,
    ExitValueSource,
    FlipAnalysis,
    LegacyRentalAnalysis,
    LegacyTakeBackAnalysis,
    LeverageMetric,
    PricingMatrix,
    RentalAnalysis,
    ReturnOverview,
    ScreenComponents,
    ScreenResult,
    SensitivityTable,
    SizingResult,
    Solve,
    SolveStatus,
    SourcesAndUses,
    State,
    TakeBackAnalysis,
    UnderwriteInputs,
    UnderwriteResult,
    ValueSource,
)

WIDTH = 92
LABEL = 34
_METRIC_LABEL = {
    LeverageMetric.LTC: "LTC",
    LeverageMetric.LTV: "LTV (sale price)",
}


def money(amount: Decimal | None) -> str:
    """``$1,234.56``; ``n/a`` for a figure that was not computed (SPEC §8.4-§8.6)."""
    return "n/a" if amount is None else f"${amount:,.2f}"


def pct1(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def pct4(value: Decimal | None) -> str:
    """A rate at four decimal places of a percent: an IRR is a solve, not a round number."""
    return "n/a" if value is None else f"{value * 100:.4f}%"


def ratio2(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def day(value: date | None) -> str:
    return "n/a" if value is None else value.isoformat()


def term_text(result: UnderwriteResult) -> str:
    """The term as a person reads it: ``9 mo``, or ``9 mo + 11 d`` with a stub.  # SPEC §8.1"""
    text = f"{result.term_months} mo"
    return f"{text} + {result.term_stub_days} d" if result.term_stub_days else text


def months(result: UnderwriteResult) -> str:
    """The term as the one number the monthly carry divides by.  # SPEC §8.1"""
    exact = result.term_months_decimal.quantize(Decimal("0.01"))
    shown = exact.normalize() if exact == exact.to_integral() else exact
    return f"{shown:f} months"


def row(label: str, value: str, note: str = "") -> str:
    line = f"  {label:<{LABEL}}{value:>16}"
    return f"{line}   {note}".rstrip()


def heading(title: str, right: str = "") -> list[str]:
    bar = "-" * WIDTH
    head = f"{title}{right:>{WIDTH - len(title)}}" if right else title
    return ["", head, bar]


def banner(title: str, subtitle: str) -> list[str]:
    lines = ["=" * WIDTH, f"  {title}"]
    lines += [f"  {line}" for line in textwrap.wrap(subtitle, WIDTH - 4)]
    lines.append("=" * WIDTH)
    return lines


def render_sizing(sizing: SizingResult, config: Config, title: str = "SIZING") -> list[str]:
    """Cost stack, commitment and split, then each metric against its cap.  # SPEC §7.4, §8.2"""
    del config  # every figure below is on the result itself
    # The caps cell is the config grid coordinate, so it prints the keys a person would look
    # up (``leverage_caps.ACQUISITION_REHAB.T3.E2``) rather than the words they read elsewhere.
    cell = f"{sizing.product.value}  {sizing.credit_tranche.value}/{sizing.experience_tier.value}"
    lines = heading(title, cell)
    lines += [
        row("Purchase price", money(sizing.purchase_price)),
        row("Rehab costs", money(sizing.rehab_costs)),
        row(
            "Rehab + contingency",
            money(sizing.rehab_adj),
            f"{pct1(sizing.contingency_pct)} contingency, lender funded",
        ),
        row("Closing costs", money(sizing.closing_costs), "the lender's, in the LTC denominator"),
        row("Total cost", money(sizing.total_cost)),
        row("Loan amount", money(sizing.loan_requested)),
        row("Commitment", money(sizing.commitment)),
        row("Funded at close", money(sizing.funded_at_close)),
    ]
    if sizing.split is not None:
        purchase, rehab = ("Advance at closing", "Rehab holdback")
        capped = (
            f"entered {money(sizing.split.rehab_portion_requested)}, capped at rehab_adj"
            if sizing.split.rehab_portion_capped
            else ""
        )
        lines += [
            row(f"  {purchase}", money(sizing.split.purchase_portion)),
            row(f"  {rehab}", money(sizing.split.rehab_portion), capped),
        ]
    elif sizing.product in SPLIT_PRODUCTS:
        lines.append(row("  Loan split", "not entered", "sized on the loan amount"))
    lines += ["", f"  {'Metric':<16}{'Actual':>10}{'Cap':>10}{'Limit':>10}   Status"]
    for metric, check in sizing.metrics.items():
        actual = pct1(check.actual)
        limit = pct1(check.cap + check.tolerance_band)
        label = _METRIC_LABEL[metric]
        lines.append(
            f"  {label:<16}{actual:>10}{pct1(check.cap):>10}{limit:>10}   {check.status.value}"
        )
    return lines


def source_label(source: ValueSource | None, missing: str = "none") -> str:
    """How a value reached the engine: an adapter, the team by hand, the borrower, or not at all."""
    if source is None:
        return missing
    if source is ValueSource.TEAM:
        return "team"
    if source is ValueSource.BORROWER:
        return "borrower"
    return "adapter"


def render_provenance(sizing: SizingResult, components: ScreenComponents) -> list[str]:
    """Where the sale price and the court record came from.  # SPEC §6

    Worth its own two lines: a Go that rests on a hand-entered value and a hand-done court
    search is a different thing from a Go that rests on a pull, and the verdict alone does
    not say which it is.
    """
    return [
        row(
            "Estimated sale price from",
            source_label(sizing.estimated_sale_price_source),
            "team = by hand, borrower = the public form; adapter > team > borrower",
        ),
        row(
            "Court records from",
            source_label(components.court_records_source, missing="not checked"),
        ),
    ]


def render_screen(result: ScreenResult, config: Config) -> list[str]:
    """Verdict, the credit and experience cell it was scored in, and every reason."""
    components = result.components
    lines = heading("SCREEN", f"verdict: {result.verdict.value}")
    credit_source = "verified" if components.credit_tranche_verified else "self-reported"
    lines += [
        row(
            "Credit tranche",
            components.credit_tranche.value,
            f"{credit_source}; floor {components.floor_tranche.value}, "
            f"{'meets' if components.credit_meets_floor else 'BELOW'}",
        ),
        row(
            "Experience tier",
            components.experience_tier.value,
            "repeat-borrower override applied"
            if components.repeat_borrower_override_applied
            else f"self-reported {components.experience_tier_self_reported.value}"
            + (
                f", verified {components.experience_tier_verified.value}"
                if components.experience_tier_verified is not None
                else ""
            ),
        ),
    ]
    lines += render_provenance(result.sizing, components)
    lines += ["", "  Reasons"]
    lines += [f"    - {reason}" for reason in result.reasons] or ["    (none)"]
    lines += render_sizing(result.sizing, config, title="SIZING (screen)")
    return lines


def render_economics(result: UnderwriteResult) -> list[str]:
    """The §8.1 Deal Economics, as the run resolved them."""
    economics = result.economics
    lines = heading(
        "DEAL ECONOMICS",
        f"{term_text(result)} ({result.rehab_months} rehab), "
        f"closes {day(result.closing_date)}, pays off {day(result.payoff_date)}",
    )
    lines += [
        row("Purchase price", money(economics.purchase_price)),
        row("Rehab costs", money(economics.rehab_costs)),
        row(
            "Contingency",
            money(economics.contingency),
            f"{pct1(economics.contingency_pct)} of rehab",
        ),
        row("Closing costs", money(economics.closing_costs)),
        row(
            "Holding costs (total)",
            money(economics.holding_costs_total),
            f"{pct1(economics.holding_costs_pct_of_cost)} of "
            f"{money(economics.holding_costs_basis)} (price + rehab); "
            f"{money(economics.holding_costs_monthly)}/mo over {months(result)}",
        ),
        row(
            "Origination fee",
            pct1(economics.origination_fee_pct),
            f"{money(economics.origination_at_close)} at close, "
            f"{money(economics.origination_at_payoff)} at payoff",
        ),
        row("Interest rate", pct1(economics.interest_rate), "annual"),
        row("Loan amount", money(economics.loan_requested)),
        row("Commitment", money(economics.commitment)),
        row("Funded at close", money(economics.funded_at_close)),
    ]
    return lines


def render_return_overview(overview: ReturnOverview) -> list[str]:
    """The lender's monthly ledger and its XIRR.  # SPEC §8.3"""
    lines = heading("RETURN OVERVIEW", f"IRR (XIRR on actual dates): {pct4(overview.irr)}")
    header = (
        f"  {'Date':<12}{'Mo':>4}{'Funding':>14}{'Draws':>14}"
        f"{'Interest':>12}{'Fees':>11}{'Payoff':>14}{'Net':>14}"
    )
    lines.append(header)
    for entry in overview.entries:
        month = f"{entry.month}*" if entry.stub_days else str(entry.month)
        lines.append(
            f"  {entry.date.isoformat():<12}{month:>4}"
            f"{entry.funding:>14,.2f}{entry.draws:>14,.2f}"
            f"{entry.interest:>12,.2f}{entry.fees:>11,.2f}"
            f"{entry.payoff:>14,.2f}{entry.net:>14,.2f}"
        )
    lines.append(
        f"  {'Total':<12}{'':>4}"
        f"{overview.total_funding:>14,.2f}{overview.total_draws:>14,.2f}"
        f"{overview.total_interest:>12,.2f}{overview.total_fees:>11,.2f}"
        f"{overview.total_payoff:>14,.2f}{overview.total_profit:>14,.2f}"
    )
    if any(entry.stub_days for entry in overview.entries):
        stub = next(entry for entry in overview.entries if entry.stub_days)
        lines.append(
            f"  * the last period is a stub of {stub.stub_days} days: its interest is one "
            "month's, prorated (SPEC 8.3)"
        )
    lines += [
        "",
        row("Total interest", money(overview.total_interest)),
        row("Total fees", money(overview.total_fees), "both origination halves"),
        row("Total profit", money(overview.total_profit), "the sum of the net column"),
        row("IRR", pct4(overview.irr), "annualized, compounded"),
    ]
    return lines


def render_sensitivity(table: SensitivityTable | None) -> list[str]:
    """IRR by loan amount (rows) and rate (columns); ``*`` is the deal's own cell.  # SPEC §8.9"""
    lines = heading("LENDER SENSITIVITY", "IRR by loan amount (rows) and annual rate (columns)")
    if table is None:
        return [*lines, "  (not on this run: the ledger predates the table)"]
    lines.append(f"  {'Loan amount':<16}" + "".join(f"{pct1(rate):>11}" for rate in table.rates))
    for row in table.rows:
        cells = "".join(
            f"{pct4(cell.irr) + ('*' if cell.is_deal else ''):>11}" for cell in row.cells
        )
        lines.append(f"  {money(row.loan_amount):<16}{cells}")
    lines.append(f"  reductions come off the advance at closing first; {marked(table)} (SPEC 8.9)")
    return lines


def marked(table: SensitivityTable | BorrowerSensitivityTable) -> str:
    """What the ``*`` means, or that there is none: the deal's rate is not a grid column."""
    if table.deal_cell is not None:
        return "* the deal as it stands"
    return f"the deal's own rate ({pct1(table.interest_rate)}) is not a column"


def render_borrower_sensitivity(table: BorrowerSensitivityTable | None) -> list[str]:
    """Profit, then project yield, by loan amount and rate; ``*`` is the deal's own.  # SPEC §8.9"""
    lines = heading("BORROWER SENSITIVITY", "profit and project yield by loan amount and rate")
    if table is None:
        return [*lines, "  (not on this run: the ledger predates the table)"]
    if table.status is not AnalysisStatus.EVALUATED:
        return [*lines, "  NOT EVALUATED: there is no sale price."]
    lines.append(f"  {'Loan amount':<16}" + "".join(f"{pct1(rate):>11}" for rate in table.rates))
    for row in table.rows:
        profits = "".join(
            f"{whole(cell.net_profit) + ('*' if cell.is_deal else ''):>11}" for cell in row.cells
        )
        ratios = "".join(
            f"{'n/a' if cell.project_yield is None else pct1(cell.project_yield):>11}"
            for cell in row.cells
        )
        lines.append(f"  {money(row.loan_amount):<16}{profits}")
        lines.append(f"  {'  project yield':<16}{ratios}")
    lines.append(
        f"  profit per the flip; yield = profit / total deal cost; {marked(table)} (SPEC 8.9)"
    )
    return lines


def whole(amount: Decimal) -> str:
    """Whole dollars, for a grid cell: ``$26,400``."""
    return f"${amount:,.0f}" if amount >= 0 else f"-${-amount:,.0f}"


def render_pricing(matrix: PricingMatrix | None) -> list[str]:
    """One line per loan amount step, in plain English.  # SPEC §8.10"""
    if matrix is None:
        lines = heading("PRICING", "lowest rate that earns the target lender IRR")
        return [*lines, "  (not on this run: the ledger predates the matrix)"]
    method = "lowest rate, solved to 0.01%," if matrix.solved else "lowest grid rate"
    lines = heading(
        "PRICING", f"{method} whose lender IRR reaches {pct1(matrix.target_lender_irr)}"
    )
    for row in matrix.rows:
        irr = "" if row.lender_irr is None else f"  (lender IRR {pct4(row.lender_irr)})"
        lines.append(f"  {row.label:<10} {row.sentence}{irr}")
        if row.borrower_equity is not None:
            lines.append(f"  {'':<10} borrower equity {money(row.borrower_equity)}")
        if row.derisk:
            lines.append(f"  {'':<10} {row.derisk}")
    return lines


def _status_note(status: AnalysisStatus, off: str, missing: str) -> str:
    if status is AnalysisStatus.OFF:
        return off
    if status is AnalysisStatus.NOT_EVALUATED:
        return missing
    return ""


def render_flip(flip: FlipAnalysis) -> list[str]:
    """The project's margin if the property is sold, and the borrower's side.  # SPEC §8.4

    The profit line by line from the sale price, then the cost stack to the total deal cost,
    then the borrower's equity, cash-on-cash against its target, and the project yield.
    """
    lines = heading("FLIP ANALYSIS", flip.status.value)
    note = _status_note(
        flip.status,
        "  OFF: the toggle was off (a run before engine 2.0.0).",
        "  NOT EVALUATED: no estimated sale price, so there is nothing to sell at.",
    )
    if note:
        lines.append(note)
    if flip.interest is not None and flip.origination is not None:
        financing = [
            ("Interest", flip.interest, "financing"),
            ("Origination", flip.origination, "financing, both halves"),
        ]
    else:
        financing = [("Financing", flip.financing_costs, "interest + origination")]
    costs = [
        ("Purchase", flip.purchase_price, ""),
        ("Closing", flip.closing_costs, ""),
        ("Rehab", flip.rehab_costs, ""),
        ("Contingency", flip.contingency, ""),
        ("Holding", flip.holding_costs_total, ""),
        *financing,
    ]
    less = "" if flip.broker_costs is None else money(-flip.broker_costs)
    lines += [
        row("Sale price", money(flip.estimated_sale_price)),
        row("  Broker", less or money(None), pct1(flip.broker_selling_pct)),
        *(
            row(f"  {label}", money(None if flip.net_profit is None else -amount), note)
            for label, amount, note in costs
        ),
        row("Net profit", money(flip.net_profit)),
        "",
        *(row(f"  {label}", money(amount), note) for label, amount, note in costs),
        row("Total deal cost", money(flip.total_costs)),
        "",
        row("Borrower equity", money(flip.borrower_equity), "total deal cost - loan amount"),
        row(
            "Cash-on-cash",
            pct1(flip.cash_on_cash),
            f"target {pct1(flip.cash_on_cash_target)}; {_passed(flip.cash_on_cash_passed)}",
        ),
        row("Project yield", pct1(flip.project_yield), "profit / total deal cost; not an IRR"),
    ]
    return lines


def render_sources_uses(table: SourcesAndUses | None) -> list[str]:
    """The two advances and the borrower's equity against the cost stack.  # SPEC §8.11"""
    lines = heading("SOURCES & USES", "equal to the cent")
    if table is None:
        return [*lines, "  (not on this run: the ledger predates the table)"]
    return [
        *lines,
        row("Advance at closing", money(table.advance_at_closing)),
        row("Rehab holdback", money(table.rehab_holdback)),
        row("Borrower equity", money(table.borrower_equity), "total deal cost - loan amount"),
        row("Total sources", money(table.total_sources)),
        "",
        row("Purchase", money(table.purchase_price)),
        row("Closing", money(table.closing_costs)),
        row("Rehab", money(table.rehab_costs)),
        row("Contingency", money(table.contingency)),
        row("Holding", money(table.holding_costs)),
        row("Interest", money(table.interest)),
        row("Origination", money(table.origination)),
        row("Total uses", money(table.total_uses)),
    ]


def _passed(passed: bool | None) -> str:
    if passed is None:
        return "not tested"
    return "PASS" if passed else "FAIL"


def _solve_text(solve: Solve) -> str:
    """A solved figure as the report prints it, or why there is none.  # SPEC §8.6"""
    if solve.status is SolveStatus.SOLVED:
        return money(solve.value)
    if solve.status is SolveStatus.NOT_EVALUATED:
        return "not evaluated"
    if solve.bound_hit is BoundHit.LOWER:
        return f"none needed ({money(solve.lower)} already reaches the target)"
    return f"no solution (none up to {money(solve.upper)})"


def render_rental(rental: RentalAnalysis | LegacyRentalAnalysis) -> list[str]:
    """The takeout: what the property has to let for to refinance us out.  # SPEC §8.5"""
    lines = heading("RENTAL ANALYSIS", rental.status.value)
    if isinstance(rental, LegacyRentalAnalysis):
        return [
            *lines,
            "  (a run before engine 2.1.0: it recorded the DSCR test, which this report no "
            "longer prints; run the analysis again)",
        ]
    if rental.status is AnalysisStatus.NOT_EVALUATED:
        lines.append(
            "  NOT EVALUATED: no exit value - no estimated sale price and none entered - so "
            "there is no takeout to size."
        )
    elif rental.market_rent_status is AnalysisStatus.NOT_EVALUATED:
        lines.append(
            "  No market rent: the gap, the two DSCRs and the rent test are not evaluated."
        )
    source = ""
    if rental.exit_value_source is ExitValueSource.SALE_PRICE:
        source = "the estimated sale price (default)"
    elif rental.exit_value_source is ExitValueSource.ENTERED:
        source = "entered on the deal"
    gap_note = "market - required"
    if rental.rent_passed is not None:
        gap_note = f"{gap_note}; {_passed(rental.rent_passed)}"
    lines += [
        row("Exit value", money(rental.exit_value), source),
        row(
            "Takeout loan",
            money(rental.takeout_loan),
            f"{pct1(rental.takeout_ltv)} of the exit value",
        ),
        row(
            "Takeout rate",
            pct1(rental.takeout_rate),
            f"{rental.amortization_years}-yr amortization",
        ),
        row("Debt service, amortizing", money(rental.debt_service_amortizing), "monthly, level"),
        row("Debt service, interest-only", money(rental.debt_service_interest_only), "monthly"),
        row(
            "Required NOI",
            money(rental.required_noi),
            f"amortizing debt service x {ratio2(rental.target_dscr)} target DSCR",
        ),
        row(
            "Required rent",
            money(rental.required_rent),
            f"required NOI / (1 - {pct1(rental.expenses_pct)} expenses)",
        ),
        row("Market rent", money(rental.market_rent)),
        row("Gap", money(rental.rent_gap), gap_note),
        row("DSCR at market, amortizing", ratio2(rental.dscr_amortizing)),
        row("DSCR at market, interest-only", ratio2(rental.dscr_interest_only)),
        row("Our payoff due", money(rental.payoff_due), "commitment + the fee's payoff half"),
        row(
            "Spread to loan",
            money(rental.spread_to_loan),
            "takeout loan - payoff due"
            + ("" if rental.spread_passed is None else f"; {_passed(rental.spread_passed)}"),
        ),
    ]
    return lines


def render_take_back(take_back: TakeBackAnalysis | LegacyTakeBackAnalysis) -> list[str]:
    """The lender's own downside: what the property has to sell or let for.  # SPEC §8.6"""
    lines = heading("TAKE-BACK ANALYSIS", take_back.status.value)
    if isinstance(take_back, LegacyTakeBackAnalysis):
        return [
            *lines,
            "  (a run before engine 2.1.0: it recorded the DSCR test, which this report no "
            "longer prints; run the analysis again)",
        ]
    if take_back.foreclosure_months is None:
        lines.append(
            f"  NOT EVALUATED: no foreclosure months in config for {take_back.state.value}."
        )
    elif take_back.status is AnalysisStatus.NOT_EVALUATED:
        lines.append(
            "  NOT EVALUATED: no projected sale price to set the haircut and the bounds against."
        )
    elif take_back.market_rent_status is AnalysisStatus.NOT_EVALUATED:
        lines.append("  No market rent: the sale price at the market rent is not evaluated.")
    if take_back.foreclosure_months_from is State.OTHER:
        lines.append(
            f"  OTHER stood in: no foreclosure months in config for {take_back.state.value}; "
            f"the placeholder of {take_back.foreclosure_months} months was used."
        )
    carry = (
        "n/a"
        if take_back.foreclosure_months is None
        else f"{take_back.foreclosure_months} mo x {money(take_back.holding_costs_monthly)}"
    )
    lines += [
        row("Loan amount", money(take_back.loan_amount), "the commitment"),
        row(
            "Default month",
            str(take_back.default_month),
            f"{take_back.term_months}-month term less the listing period, floored at 1",
        ),
        row(
            "Interest received",
            money(take_back.interest_received),
            f"months 1-{take_back.default_month} at {pct1(take_back.interest_rate)}",
        ),
        row(
            "Lost interest",
            money(take_back.lost_interest),
            f"{take_back.lost_interest_months} mo of nothing after the default",
        ),
        row("Legal costs", money(take_back.legal_costs)),
        row("Foreclosure carry", money(take_back.foreclosure_carry), carry),
        row(
            "REO sale month",
            "n/a" if take_back.sale_month is None else str(take_back.sale_month),
            f"net of {pct1(take_back.broker_selling_pct)} broker",
        ),
        row("Target lender IRR", pct1(take_back.target_irr), "bisection on the XIRR"),
        "",
        "  Haircut to target",
        row("Projected sale price", money(take_back.projected_sale_price)),
    ]
    solve = take_back.haircut_price
    lines.append(
        row(
            "Sale price needed",
            "not evaluated" if solve is None else _solve_text(solve),
            "" if solve is None else f"searched {money(solve.lower)} to {money(solve.upper)}",
        )
    )
    if take_back.haircut_amount is not None:
        lines += [
            row(
                "Haircut",
                money(take_back.haircut_amount),
                f"{pct1(take_back.haircut_pct)} of the projected sale price",
            ),
            row(
                "vs purchase price",
                pct1(take_back.price_pct_of_purchase),
                f"of {money(take_back.purchase_price)}; "
                + ("ABOVE the purchase price" if take_back.price_above_purchase else "under it"),
            ),
        ]
        if take_back.as_is_value is not None:
            lines.append(
                row(
                    "vs as-is value",
                    money(take_back.price_vs_as_is),
                    f"{pct1(take_back.price_pct_of_as_is)} of {money(take_back.as_is_value)}",
                )
            )
    elif take_back.as_is_value is not None:
        lines.append(row("As-is value", money(take_back.as_is_value)))
    lines += ["", "  Hold and rent"]
    for hold in take_back.holds:
        lines += [
            row(
                f"{hold.hold_months}-month hold",
                _solve_text(hold.required_rent),
                "rent needed at the projected sale price",
            ),
            row(
                "",
                _solve_text(hold.required_sale_price),
                "sale price needed at the market rent",
            ),
        ]
    return lines


def render_flags(result: UnderwriteResult) -> list[str]:
    lines = heading("FLAGS", f"{len(result.flags)} raised")
    if not result.flags:
        return [*lines, "  (none)"]
    for flag in result.flags:
        lines.append(f"  [{flag.severity.value:<4}] {flag.code.value}")
        lines.append(f"         {flag.message}")
    return lines


def render_rent_provenance(inputs: UnderwriteInputs | None) -> list[str]:
    """Where the market rent the two analyses read came from.  # SPEC §4.2, §6.1

    A borrower's own figure for what their property lets for is the one number on the deal
    that nobody at GLENWOOD has looked at, so the report says when that is what it ran on.
    """
    if inputs is None or inputs.monthly_rent is None:
        return []
    return [
        row(
            "Monthly rent from",
            source_label(inputs.monthly_rent_source, missing="unstated"),
            "team = by hand, borrower = the public form; a team entry replaces it",
        )
    ]


def render_underwrite(
    result: UnderwriteResult,
    config: Config,
    screen_sizing: SizingResult | None = None,
    inputs: UnderwriteInputs | None = None,
) -> list[str]:
    """Everything the underwrite produced, in the SPEC §9 order.  # SPEC §8

    The sizing is repeated only when the verified values moved it: on a fixture that
    already carried the valuation it is the same table twice, so instead the report says so
    in one line.
    """
    sizing: list[str]
    if screen_sizing is not None and screen_sizing == result.sizing:
        sizing = [
            *heading("SIZING (underwrite)", "unchanged"),
            "  Identical to the screen above: same values, same caps cell.",
        ]
    else:
        sizing = render_sizing(result.sizing, config, title="SIZING (underwrite: verified)")
    return [
        *sizing,
        *render_economics(result),
        *render_return_overview(result.return_overview),
        *render_sensitivity(result.sensitivity),
        *render_borrower_sensitivity(result.borrower_sensitivity),
        *render_pricing(result.pricing),
        *render_flip(result.flip),
        *render_rental(result.rental),
        *render_rent_provenance(inputs),
        *render_take_back(result.take_back),
        *render_sources_uses(result.sources_and_uses),
        *render_flags(result),
    ]


def render(
    name: str,
    description: str,
    screen_result: ScreenResult,
    underwrite_result: UnderwriteResult | None,
    config: Config,
    underwrite_inputs: UnderwriteInputs | None = None,
) -> str:
    """The whole report: the screen, then the underwrite when one was run."""
    stamp = f"engine {screen_result.engine_version} - config {screen_result.config_hash[:12]}"
    lines = [*banner(f"{name}   [{stamp}]", description)]
    lines += render_screen(screen_result, config)
    if underwrite_result is not None:
        lines += render_underwrite(
            underwrite_result, config, screen_result.sizing, underwrite_inputs
        )
    lines.append("")
    return "\n".join(lines)
