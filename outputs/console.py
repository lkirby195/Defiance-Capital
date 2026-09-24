"""Render a screen and an underwrite as plain text for the terminal.  # SPEC §9.1, §9.6

For manual verification of the math, not a client deliverable: every number the engine
produced is shown at the precision it was computed to, next to the threshold it was tested
against. Pure - text in, text out, no I/O - so the CLI stays a thin wrapper and the layout
can be asserted in a test.

The underwrite prints the SPEC §9 sections in order - Deal Economics, Return Overview, Flip,
Rental, Take-Back, Flags - which is the same order the deal page shows and the workbook
writes, so a person checking one against another reads down the same list.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal

from config.config import Config
from schema.labels import enum_label
from schema.models import (
    SPLIT_PRODUCTS,
    AnalysisStatus,
    FlipAnalysis,
    LeverageMetric,
    Product,
    RentalAnalysis,
    ReturnOverview,
    ScreenComponents,
    ScreenResult,
    SizingResult,
    TakeBackAnalysis,
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
    # up (``leverage_caps.SPLIT_DRAW.T3.E2``) rather than the words they read elsewhere.
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
        purchase, rehab = (
            ("Principal Note", "Tranche A")
            if sizing.product is Product.SPLIT_PRINCIPAL
            else ("Advance at closing", "Rehab holdback")
        )
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
    """How a value reached the engine: an adapter, the team by hand, or not at all."""
    if source is None:
        return missing
    return "team" if source is ValueSource.TEAM else "adapter"


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
            "team = entered by hand; an adapter value wins",
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
        f"{day(result.closing_date)} to {day(result.payoff_date)}",
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


def _status_note(status: AnalysisStatus, off: str, missing: str) -> str:
    if status is AnalysisStatus.OFF:
        return off
    if status is AnalysisStatus.NOT_EVALUATED:
        return missing
    return ""


def render_flip(flip: FlipAnalysis) -> list[str]:
    """The project's margin if the property is sold.  # SPEC §8.4"""
    lines = heading("FLIP ANALYSIS", flip.status.value)
    note = _status_note(
        flip.status,
        "  OFF: the toggle is off, so the flip was not asked for.",
        "  NOT EVALUATED: no estimated sale price, so there is nothing to sell at.",
    )
    if note:
        lines.append(note)
    lines += [
        row("Estimated sale price", money(flip.estimated_sale_price)),
        row("Broker selling costs", money(flip.broker_costs), pct1(flip.broker_selling_pct)),
        row("Purchase price", money(flip.purchase_price)),
        row("Closing costs", money(flip.closing_costs)),
        row("Holding costs", money(flip.holding_costs_total)),
        row("Rehab costs", money(flip.rehab_costs)),
        row("Contingency", money(flip.contingency)),
        row("Financing costs", money(flip.financing_costs), "interest + origination"),
        row("Total costs", money(flip.total_costs)),
        row("Net profit", money(flip.net_profit)),
        row("Yield (Profit / Costs)", pct1(flip.profit_yield), "a project margin, not an IRR"),
    ]
    return lines


def _passed(passed: bool | None) -> str:
    if passed is None:
        return "not tested"
    return "PASS" if passed else "FAIL"


def render_rental(rental: RentalAnalysis) -> list[str]:
    """Whether the rent carries a takeout loan on the commitment.  # SPEC §8.5"""
    lines = heading("RENTAL ANALYSIS", rental.status.value)
    note = _status_note(
        rental.status,
        "  OFF: the toggle is off.",
        "  NOT EVALUATED: no monthly rent, so there is no net monthly income.",
    )
    if note:
        lines.append(note)
    lines += [
        row("Monthly rent", money(rental.monthly_rent)),
        row("Expenses", money(rental.expenses), f"{pct1(rental.expenses_pct)} of rent"),
        row("Monthly holding costs", money(rental.holding_costs_monthly)),
        row("Net monthly income", money(rental.net_monthly_income)),
        row(
            "Debt service (monthly)",
            money(rental.debt_service_monthly),
            f"{money(rental.loan_amount)} at {pct1(rental.takeout_rate)}, "
            f"{rental.amortization_years}-yr",
        ),
        row(
            "DSCR",
            ratio2(rental.dscr),
            f"floor {ratio2(rental.dscr_floor)}; {_passed(rental.passed)}",
        ),
    ]
    return lines


def render_take_back(take_back: TakeBackAnalysis) -> list[str]:
    """Whether the rent carries what the loan cost GLENWOOD.  # SPEC §8.6"""
    lines = heading("TAKE-BACK ANALYSIS", f"{take_back.status.value}  (always on)")
    if take_back.status is AnalysisStatus.NOT_EVALUATED:
        lines.append("  NOT EVALUATED: no monthly rent, so there is no DSCR to test.")
    lines += [
        row("Loan amount", money(take_back.loan_amount), "the commitment"),
        row(
            "Lost interest",
            money(take_back.lost_interest),
            f"{take_back.lost_interest_months} mo at {pct1(take_back.interest_rate)}",
        ),
        row("Legal costs", money(take_back.legal_costs)),
        row("Total cost", money(take_back.total_cost)),
        row(
            "Debt service (monthly)",
            money(take_back.debt_service_monthly),
            f"on the total cost, {take_back.amortization_years}-yr",
        ),
        row("Net monthly income", money(take_back.net_monthly_income)),
        row(
            "DSCR at loan cost",
            ratio2(take_back.dscr),
            f"floor {ratio2(take_back.dscr_floor)}; {_passed(take_back.passed)}",
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


def render_underwrite(
    result: UnderwriteResult, config: Config, screen_sizing: SizingResult | None = None
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
    exit_note = (
        f"exit {enum_label(result.exit.type)} ({result.exit.exit_source.value}); "
        f"flip {'on' if result.exit.flip_analysis else 'off'}, "
        f"rental {'on' if result.exit.rental_analysis else 'off'}, "
        "take-back always on"
    )
    return [
        *sizing,
        *heading("EXIT", exit_note),
        "  Informational: all it does is default the two analysis toggles (SPEC §3).",
        *render_economics(result),
        *render_return_overview(result.return_overview),
        *render_flip(result.flip),
        *render_rental(result.rental),
        *render_take_back(result.take_back),
        *render_flags(result),
    ]


def render(
    name: str,
    description: str,
    screen_result: ScreenResult,
    underwrite_result: UnderwriteResult | None,
    config: Config,
) -> str:
    """The whole report: the screen, then the underwrite when one was run."""
    stamp = f"engine {screen_result.engine_version} - config {screen_result.config_hash[:12]}"
    lines = [*banner(f"{name}   [{stamp}]", description)]
    lines += render_screen(screen_result, config)
    if underwrite_result is not None:
        lines += render_underwrite(underwrite_result, config, screen_result.sizing)
    lines.append("")
    return "\n".join(lines)
