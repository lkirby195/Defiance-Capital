"""Render a screen and an underwrite as plain text for the terminal.  # SPEC §9.1

For manual verification of the math, not a client deliverable: every number the engine
produced is shown at the precision it was computed to, next to the threshold it was tested
against. Pure - text in, text out, no I/O - so the CLI stays a thin wrapper and the layout
can be asserted in a test.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal

from config.config import Config
from schema.models import (
    LeverageMetric,
    Product,
    ScreenComponents,
    ScreenResult,
    SizingResult,
    UnderwriteResult,
    ValueBasis,
    ValueSource,
    YieldGrid,
)

WIDTH = 92
LABEL = 34
_METRIC_LABEL = {
    LeverageMetric.LTC: "LTC",
    LeverageMetric.LTV_AS_IS: "LTV (as-is)",
    LeverageMetric.LTARV: "LTARV",
}


def money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def pct1(value: Decimal) -> str:
    return f"{value:.1%}"


def pct4(value: Decimal) -> str:
    """A rate at four decimal places of a percent: r* is exact, not rounded for display."""
    return f"{value * 100:.4f}%"


def ratio2(value: Decimal) -> str:
    return f"{value:.2f}x"


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
    cell = f"{sizing.product.value}  {sizing.credit_tranche.value}/{sizing.experience_tier.value}"
    lines = heading(title, cell)
    price = sizing.total_cost - sizing.rehab_adj - sizing.buy_closing
    lines += [
        row("Purchase price", money(price)),
        row(
            "Rehab budget + contingency",
            money(sizing.rehab_adj),
            f"{pct1(config.fees.contingency_pct)} contingency, lender funded",
        ),
        row(
            "Buy-side closing",
            money(sizing.buy_closing),
            f"{pct1(config.fees.borrower_closing_pct_of_price)} of price, borrower cash",
        ),
        row("Total cost", money(sizing.total_cost)),
        row("Loan requested", money(sizing.loan_requested)),
        row("Commitment", money(sizing.commitment)),
        row("Funded at close", money(sizing.funded_at_close)),
    ]
    if sizing.split is not None:
        purchase, rehab = (
            ("Principal Note", "Tranche A")
            if sizing.product is Product.SPLIT_PRINCIPAL
            else ("Purchase portion", "Rehab holdback")
        )
        override = "team override" if sizing.split.purchase_portion_overridden else ""
        lines += [
            row(f"  {purchase}", money(sizing.split.purchase_portion), override),
            row(f"  {rehab}", money(sizing.split.rehab_portion)),
        ]
    lines += ["", f"  {'Metric':<16}{'Actual':>10}{'Cap':>10}{'Limit':>10}   Status"]
    for metric, check in sizing.metrics.items():
        actual = pct1(check.actual) if check.actual is not None else "n/a"
        limit = pct1(check.cap + check.tolerance_band)
        label = _METRIC_LABEL[metric]
        if check.basis is ValueBasis.PURCHASE_PRICE:
            label = "LTV (on price)"  # the as-is value was unavailable (SPEC §7.4)
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
    """Where the valuation and the court record came from.  # SPEC §6

    Worth its own two lines: a Go that rests on a hand-entered value and a hand-done court
    search is a different thing from a Go that rests on a pull, and the verdict alone does
    not say which it is.
    """
    return [
        row(
            "As-is value / ARV from",
            f"{source_label(sizing.as_is_value_source)} / {source_label(sizing.arv_source)}",
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


def render_grid(grid: YieldGrid) -> list[str]:
    """The lender-yield grid: rates across, payoff months down.  # SPEC §8.5

    ``*`` marks a cell at or above the target; the r* column is marked in the header, so a
    solved rate below the configured grid shows up as the first column rather than hiding.
    """
    lines = heading("GRID", f"lender_yield(month, rate); * meets {pct1(grid.target)}")
    header = "  month "
    for rate in grid.rates:
        mark = "*" if rate == grid.solved_rate else " "
        header += f"{pct4(rate) + mark:>11}"
    lines.append(header)
    for grid_row in grid.rows:
        line = f"  {grid_row.month:>5} "
        for cell in grid_row.cells:
            mark = "*" if cell.meets_target else " "
            line += f"{pct1(cell.annualized_yield) + mark:>11}"
        lines.append(line)
    solved = "inserted" if grid.solved_rate_inserted else "already a grid column"
    lines.append(f"  r* = {pct4(grid.solved_rate)} ({solved})")
    return lines


def render_lender(result: UnderwriteResult, config: Config) -> list[str]:
    """Interest, fees, average outstanding and the yield the solve targets.  # SPEC §8.4"""
    lender = result.lender_at_solve
    fees = lender.fees
    lines = heading(
        "LENDER",
        f"term {result.term_months} mo, rehab {result.rehab_months} mo, "
        f"target {pct1(config.returns.target_irr)}",
    )
    lines += [
        row("Solved rate r*", pct4(result.solved_rate)),
        row("Average outstanding", money(lender.avg_outstanding), "over the term"),
        row("Interest at r*", money(lender.interest)),
        row("Origination at close", money(fees.origination_at_close)),
        row("Origination at payoff", money(fees.origination_at_payoff)),
        row("Extension fee", money(fees.extension), "charged only past the term"),
        row("Fees total", money(fees.total)),
        row("Yield at (term, r*)", pct4(result.lender_yield_at_solve), "= target by construction"),
    ]
    return lines


def render_borrower(result: UnderwriteResult) -> list[str]:
    """The borrower's project, at (term, r*). Information only: no floor, no flag."""
    borrower = result.borrower_at_solve
    lines = heading(
        "BORROWER",
        f"at ({borrower.month} mo, {pct4(borrower.rate)}) - information only",
    )
    coc = pct1(borrower.cash_on_cash) if borrower.cash_on_cash is not None else "n/a (cash_in <= 0)"
    lines += [
        row("Purchase price", money(borrower.purchase_price)),
        row("Rehab + contingency", money(borrower.rehab_adj)),
        row("Buy-side closing", money(borrower.buy_closing)),
        row("Total project cost", money(borrower.total_project_cost)),
        row("Interest paid", money(borrower.interest_paid)),
        row("Fees paid", money(borrower.fees_paid)),
        row("Holding costs", money(borrower.holding_costs)),
        row("Exit price", money(borrower.exit_price)),
        row("Exit net of selling costs", money(borrower.exit_net)),
        row("Profit", money(borrower.profit)),
        row("Cash in", money(borrower.cash_in)),
        row("Cash on cash", coc),
    ]
    return lines


def render_exit(result: UnderwriteResult, config: Config) -> list[str]:
    """DSCR takeout; runs on every deal whatever the exit.  # SPEC §8.6"""
    exit_result = result.exit
    lines = heading(
        "EXIT",
        f"{exit_result.type.value} ({exit_result.exit_source.value}); DSCR takeout runs regardless",
    )
    dscr = ratio2(exit_result.dscr_at_payoff) if exit_result.dscr_at_payoff is not None else "n/a"
    takeout = config.takeout
    lines += [
        row("Gross rent (annual)", money(exit_result.gross_rent_annual)),
        row(
            "Annual taxes",
            money(exit_result.annual_taxes),
            exit_result.annual_taxes_source.value.lower(),
        ),
        row(
            "Annual insurance",
            money(exit_result.annual_insurance),
            exit_result.annual_insurance_source.value.lower(),
        ),
        row("Opex (annual)", money(exit_result.opex_annual)),
        row("NOI (annual)", money(exit_result.noi_annual)),
        row(f"ARV x {pct1(takeout.ltv)} LTV", money(exit_result.ltv_takeout)),
        row(f"Loan at {takeout.dscr_floor:.2f}x DSCR", money(exit_result.dscr_takeout)),
        row("Max takeout", money(exit_result.max_takeout), "lesser of the two"),
        row("Payoff due", money(exit_result.payoff_due), "commitment + payoff fees"),
        row("DSCR at payoff due", dscr),
        row("Refi covers", "yes" if exit_result.refi_covers else "NO"),
        row("Shortfall", money(exit_result.shortfall)),
    ]
    return lines


def render_downside(result: UnderwriteResult) -> list[str]:
    """REO recovery against exposure; runs on every deal.  # SPEC §8.6"""
    downside = result.downside
    lines = heading("DOWNSIDE", "REO: no income or cap-rate valuation")
    lines += [
        row("Recovery basis", money(downside.recovery_basis), "min(as-is + rehab_adj, ARV)"),
        row("Liquidation after haircut", money(downside.liquidation)),
        row("Selling costs", money(downside.selling_costs)),
        row("Foreclosure cost", money(downside.foreclosure_cost)),
        row(
            "Holding through foreclosure",
            money(downside.holding_through_foreclosure),
            f"{downside.foreclosure_months} mo x {money(downside.monthly_holding_cost)}",
        ),
        row("Recovery", money(downside.recovery)),
        row("Exposure", money(downside.exposure), "commitment + unpaid fees"),
        row(
            "Cover",
            ratio2(downside.cover),
            f"floor {ratio2(downside.cover_floor)}; {'PASS' if downside.passed else 'FAIL'}",
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
    """Everything the underwrite produced, in the order the math runs.  # SPEC §8

    The sizing is repeated only when the verified values moved it: on a fixture that
    already carried the as-is value and the ARV it is the same table twice, so instead the
    report says so in one line.
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
        *render_lender(result, config),
        *render_grid(result.grid_lender),
        *render_borrower(result),
        *render_exit(result, config),
        *render_downside(result),
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
