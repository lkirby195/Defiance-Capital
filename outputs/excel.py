"""Write one run to an .xlsx workbook for manual verification of the math.  # SPEC §8

Not a client deliverable: it is the engine's own arithmetic laid out so a person can check
it against a spreadsheet of their own. Every figure is written as a number with a currency,
percent or multiple format - never as preformatted text - so the cells add up, chart, and
compare. Sheets, in the order the math runs:

    Inputs    what went in, screen and underwrite
    Sizing    cost stack, commitment split, each metric against its cap
    Lender    interest, the fee schedule, the yield at term, r*
    Grid      lender_yield(month, rate) over the whole grid
    Borrower  the borrower's project at (term, r*), information only
    Exit      the DSCR takeout
    Downside  the REO recovery against exposure
    Flags     every flag with its severity and the threshold it names
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from config.config import Config
from schema.models import (
    Product,
    ScreenInputs,
    SizingResult,
    UnderwriteInputs,
    UnderwriteResult,
    ValueBasis,
    ValueSource,
)

MONEY = '"$"#,##0.00'
PCT1 = "0.0%"
PCT4 = "0.0000%"
RATIO = '0.00"x"'
INTEGER = "0"

HEAD = Font(bold=True)
TITLE = Font(bold=True, size=13)

# (label, value, number format, note); a value of None writes an empty cell.
Row = tuple[str, Any, str | None, str]


def _source(source: ValueSource | None) -> str:
    """The provenance note that sits beside a valuation cell.  # SPEC §6"""
    if source is None:
        return "no value available"
    return "team, by hand" if source is ValueSource.TEAM else "adapter / paid pull"


def _title(sheet: Worksheet, text: str) -> None:
    sheet["A1"] = text
    sheet["A1"].font = TITLE


def _rows(sheet: Worksheet, rows: list[Row], start: int = 3) -> int:
    """Write label / value / note rows; returns the next free row."""
    at = start
    for label, value, fmt, note in rows:
        sheet.cell(row=at, column=1, value=label)
        cell = sheet.cell(row=at, column=2, value=value)
        if fmt is not None:
            cell.number_format = fmt
        if note:
            sheet.cell(row=at, column=3, value=note)
        at += 1
    return at


def _widths(sheet: Worksheet, *widths: int) -> None:
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width


def _header(sheet: Worksheet, at: int, labels: list[str]) -> int:
    for column, label in enumerate(labels, start=1):
        cell = sheet.cell(row=at, column=column, value=label)
        cell.font = HEAD
        cell.alignment = Alignment(horizontal="right" if column > 1 else "left")
    return at + 1


def _sheet_inputs(
    book: Workbook,
    screen_inputs: ScreenInputs,
    underwrite_inputs: UnderwriteInputs,
    config: Config,
) -> None:
    sheet = book.create_sheet("Inputs")
    _title(sheet, "Inputs")
    deal = underwrite_inputs.deal
    borrower = underwrite_inputs.borrower
    rows: list[Row] = [
        ("Product", deal.product.value, None, ""),
        ("State", underwrite_inputs.state.value, None, ""),
        ("Purchase price", deal.purchase_price, MONEY, ""),
        ("Rehab budget", deal.rehab_budget, MONEY, "before contingency"),
        ("Loan requested", deal.loan_requested, MONEY, ""),
        (
            "As-is value",
            deal.as_is_value,
            MONEY,
            _source(deal.as_is_value_source),
        ),
        ("ARV", deal.arv, MONEY, _source(deal.arv_source)),
        ("Purchase portion override", deal.purchase_portion_override, MONEY, "team override"),
        ("Term (months)", underwrite_inputs.term_months, INTEGER, ""),
        ("Market rent (monthly)", underwrite_inputs.market_rent_monthly, MONEY, ""),
        ("Annual taxes", underwrite_inputs.annual_taxes_usd, MONEY, "blank = config default"),
        (
            "Annual insurance",
            underwrite_inputs.annual_insurance_usd,
            MONEY,
            "blank = config default",
        ),
        ("Annual utilities", underwrite_inputs.annual_utilities_usd, MONEY, "team input"),
        ("Extension fee", underwrite_inputs.extension_fee_pct, PCT1, "blank = config default"),
        ("Exit price", underwrite_inputs.exit_price, MONEY, "blank = ARV"),
        (
            "Asset type",
            underwrite_inputs.asset_type.value if underwrite_inputs.asset_type else None,
            None,
            "drives the exit inference",
        ),
        ("Stated exit", underwrite_inputs.stated_exit.value, None, "a stated exit always wins"),
        (
            "Court records",
            underwrite_inputs.court_records.source.value
            if underwrite_inputs.court_records
            else None,
            None,
            "SPEC §7.2 tests re-run here; blank when no source was checked",
        ),
        ("Credit (self-reported)", borrower.credit_range_self_reported.value, None, ""),
        ("Credit score (verified)", borrower.verified_credit_score, INTEGER, ""),
        ("Experience (self-reported)", borrower.experience_bucket_self_reported.value, None, ""),
        ("Deals in 36 mo (verified)", borrower.verified_deals_36mo, INTEGER, ""),
        ("Repeat borrower (self)", borrower.repeat_borrower_self_reported, None, ""),
        (
            "Screen: as-is value",
            screen_inputs.deal.as_is_value,
            MONEY,
            _source(screen_inputs.deal.as_is_value_source),
        ),
        ("Screen: ARV", screen_inputs.deal.arv, MONEY, _source(screen_inputs.deal.arv_source)),
        (
            "Screen: court records",
            screen_inputs.court_records.source.value if screen_inputs.court_records else None,
            None,
            "blank when no source was checked",
        ),
        ("", None, None, ""),
        ("Config: target IRR", config.returns.target_irr, PCT1, ""),
        ("Config: origination", config.fees.origination_pct, PCT1, "half at close, half at payoff"),
        ("Config: contingency", config.fees.contingency_pct, PCT1, ""),
        ("Config: buy-side closing", config.fees.borrower_closing_pct_of_price, PCT1, ""),
        ("Config: selling cost", config.fees.selling_cost_pct, PCT1, ""),
        ("Config: listing months", config.draws.listing_months, INTEGER, ""),
        ("Config: draw utilization", config.draws.draw_avg_utilization, PCT1, ""),
    ]
    _rows(sheet, rows)
    _widths(sheet, 28, 18, 34)


def _sheet_sizing(book: Workbook, sizing: SizingResult, config: Config) -> None:
    sheet = book.create_sheet("Sizing")
    _title(sheet, "Sizing")
    price = sizing.total_cost - sizing.rehab_adj - sizing.buy_closing
    rows: list[Row] = [
        ("Purchase price", price, MONEY, ""),
        ("Rehab + contingency", sizing.rehab_adj, MONEY, "lender funded"),
        ("Buy-side closing", sizing.buy_closing, MONEY, "borrower cash"),
        ("Total cost", sizing.total_cost, MONEY, "LTC denominator"),
        ("Loan requested", sizing.loan_requested, MONEY, ""),
        ("Commitment", sizing.commitment, MONEY, ""),
        ("Funded at close", sizing.funded_at_close, MONEY, ""),
    ]
    if sizing.split is not None:
        purchase, rehab = (
            ("Principal Note", "Tranche A")
            if sizing.product is Product.SPLIT_PRINCIPAL
            else ("Purchase portion", "Rehab holdback")
        )
        note = "team override" if sizing.split.purchase_portion_overridden else ""
        rows += [
            (purchase, sizing.split.purchase_portion, MONEY, note),
            (rehab, sizing.split.rehab_portion, MONEY, ""),
        ]
    at = _rows(sheet, rows) + 1
    at = _header(sheet, at, ["Metric", "Actual", "Cap", "Limit", "Status", "Basis"])
    for metric, check in sizing.metrics.items():
        sheet.cell(row=at, column=1, value=metric.value)
        actual = sheet.cell(row=at, column=2, value=check.actual)
        actual.number_format = PCT1
        cap = sheet.cell(row=at, column=3, value=check.cap)
        cap.number_format = PCT1
        limit = sheet.cell(row=at, column=4, value=check.cap + check.tolerance_band)
        limit.number_format = PCT1
        sheet.cell(row=at, column=5, value=check.status.value)
        basis = check.basis.value if check.basis is not None else ""
        if check.basis is ValueBasis.PURCHASE_PRICE:
            basis += " (as-is value unavailable)"
        sheet.cell(row=at, column=6, value=basis)
        at += 1
    at += 1
    sheet.cell(row=at, column=1, value="Caps cell").font = HEAD
    sheet.cell(
        row=at,
        column=2,
        value=f"{sizing.product.value} / {sizing.credit_tranche.value}"
        f" / {sizing.experience_tier.value}",
    )
    sheet.cell(row=at + 1, column=1, value="Tolerance band")
    band = sheet.cell(row=at + 1, column=2, value=config.screen.tolerance_band)
    band.number_format = PCT1
    _widths(sheet, 24, 16, 12, 12, 18, 34)


def _sheet_lender(book: Workbook, result: UnderwriteResult, config: Config) -> None:
    sheet = book.create_sheet("Lender")
    _title(sheet, "Lender")
    lender = result.lender_at_solve
    fees = lender.fees
    rows: list[Row] = [
        ("Term (months)", result.term_months, INTEGER, ""),
        ("Rehab months", result.rehab_months, INTEGER, "term - listing months"),
        ("Target IRR", config.returns.target_irr, PCT1, ""),
        ("Solved rate r*", result.solved_rate, PCT4, "lender_yield(term, r*) = target"),
        ("Average outstanding", lender.avg_outstanding, MONEY, ""),
        ("Interest at r*", lender.interest, MONEY, "avg outstanding x r* x term / 12"),
        ("Origination at close", fees.origination_at_close, MONEY, ""),
        ("Origination at payoff", fees.origination_at_payoff, MONEY, ""),
        ("Extension fee", fees.extension, MONEY, "only past the term"),
        ("Fees total", fees.total, MONEY, ""),
        ("Commitment", result.sizing.commitment, MONEY, "yield denominator"),
        ("Yield at (term, r*)", result.lender_yield_at_solve, PCT4, "= target by construction"),
    ]
    _rows(sheet, rows)
    _widths(sheet, 24, 18, 36)


def _sheet_grid(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Grid")
    _title(sheet, "Lender yield grid")
    grid = result.grid_lender
    sheet.cell(row=2, column=1, value="bold = at or above target; r* column marked")
    at = 4
    head = sheet.cell(row=at, column=1, value="month")
    head.font = HEAD
    for column, rate in enumerate(grid.rates, start=2):
        cell = sheet.cell(row=at, column=column, value=rate)
        cell.number_format = PCT4
        cell.font = HEAD
        if rate == grid.solved_rate:
            sheet.cell(row=at - 1, column=column, value="r*").font = HEAD
    at += 1
    for grid_row in grid.rows:
        sheet.cell(row=at, column=1, value=grid_row.month).number_format = INTEGER
        for column, cell_value in enumerate(grid_row.cells, start=2):
            cell = sheet.cell(row=at, column=column, value=cell_value.annualized_yield)
            cell.number_format = PCT1
            if cell_value.meets_target:
                cell.font = HEAD
        at += 1
    at += 1
    sheet.cell(row=at, column=1, value="target")
    target = sheet.cell(row=at, column=2, value=grid.target)
    target.number_format = PCT1
    sheet.cell(row=at + 1, column=1, value="r*")
    solved = sheet.cell(row=at + 1, column=2, value=grid.solved_rate)
    solved.number_format = PCT4
    sheet.cell(
        row=at + 1,
        column=3,
        value="inserted into the grid" if grid.solved_rate_inserted else "already a column",
    )
    _widths(sheet, 10, *[11] * len(grid.rates))


def _sheet_borrower(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Borrower")
    _title(sheet, "Borrower economics (information only)")
    borrower = result.borrower_at_solve
    rows: list[Row] = [
        ("Payoff month", borrower.month, INTEGER, ""),
        ("Rate", borrower.rate, PCT4, "r*"),
        ("Purchase price", borrower.purchase_price, MONEY, ""),
        ("Rehab + contingency", borrower.rehab_adj, MONEY, ""),
        ("Buy-side closing", borrower.buy_closing, MONEY, "borrower cash"),
        ("Total project cost", borrower.total_project_cost, MONEY, ""),
        ("Interest paid", borrower.interest_paid, MONEY, ""),
        ("Fees paid", borrower.fees_paid, MONEY, ""),
        ("Holding costs", borrower.holding_costs, MONEY, "taxes + insurance + utilities"),
        ("Exit price", borrower.exit_price, MONEY, ""),
        ("Exit net of selling costs", borrower.exit_net, MONEY, ""),
        ("Profit", borrower.profit, MONEY, ""),
        ("Cash in", borrower.cash_in, MONEY, "ignores draw reimbursement timing"),
        ("Cash on cash", borrower.cash_on_cash, PCT1, "blank when cash in <= 0"),
    ]
    _rows(sheet, rows)
    _widths(sheet, 26, 18, 36)


def _sheet_exit(book: Workbook, result: UnderwriteResult, config: Config) -> None:
    sheet = book.create_sheet("Exit")
    _title(sheet, "DSCR takeout (runs on every deal)")
    exit_result = result.exit
    takeout = config.takeout
    rows: list[Row] = [
        ("Exit type", exit_result.type.value, None, exit_result.exit_source.value.lower()),
        ("Gross rent (annual)", exit_result.gross_rent_annual, MONEY, ""),
        (
            "Annual taxes",
            exit_result.annual_taxes,
            MONEY,
            exit_result.annual_taxes_source.value.lower(),
        ),
        (
            "Annual insurance",
            exit_result.annual_insurance,
            MONEY,
            exit_result.annual_insurance_source.value.lower(),
        ),
        ("Opex (annual)", exit_result.opex_annual, MONEY, "rent percentages + taxes + insurance"),
        ("NOI (annual)", exit_result.noi_annual, MONEY, ""),
        ("Takeout LTV", takeout.ltv, PCT1, "of ARV"),
        ("ARV x takeout LTV", exit_result.ltv_takeout, MONEY, ""),
        ("DSCR floor", takeout.dscr_floor, RATIO, ""),
        ("Takeout rate", takeout.rate, PCT1, f"{takeout.amortization_years}-yr amortization"),
        ("Loan at the DSCR floor", exit_result.dscr_takeout, MONEY, ""),
        ("Max takeout", exit_result.max_takeout, MONEY, "lesser of the two"),
        ("Payoff due", exit_result.payoff_due, MONEY, "commitment + payoff fees"),
        ("DSCR at payoff due", exit_result.dscr_at_payoff, RATIO, ""),
        ("Refi covers", exit_result.refi_covers, None, ""),
        ("Shortfall", exit_result.shortfall, MONEY, ""),
    ]
    _rows(sheet, rows)
    _widths(sheet, 26, 18, 38)


def _sheet_downside(book: Workbook, result: UnderwriteResult, config: Config) -> None:
    sheet = book.create_sheet("Downside")
    _title(sheet, "REO downside (runs on every deal)")
    downside = result.downside
    rows: list[Row] = [
        ("Recovery basis", downside.recovery_basis, MONEY, "min(as-is + rehab_adj, ARV)"),
        ("REO haircut", config.downside.reo_haircut, PCT1, ""),
        ("Liquidation", downside.liquidation, MONEY, ""),
        ("Selling costs", downside.selling_costs, MONEY, ""),
        ("Foreclosure cost", downside.foreclosure_cost, MONEY, ""),
        ("Foreclosure months", downside.foreclosure_months, INTEGER, "by state"),
        ("Monthly holding cost", downside.monthly_holding_cost, MONEY, ""),
        ("Holding through foreclosure", downside.holding_through_foreclosure, MONEY, ""),
        ("Recovery", downside.recovery, MONEY, ""),
        ("Unpaid fees", downside.unpaid_fees, MONEY, "the payoff half of origination"),
        ("Exposure", downside.exposure, MONEY, "commitment + unpaid fees"),
        ("Cover", downside.cover, RATIO, "recovery / exposure"),
        ("Cover floor", downside.cover_floor, RATIO, ""),
        ("Passed", downside.passed, None, ""),
    ]
    _rows(sheet, rows)
    _widths(sheet, 28, 18, 36)


def _sheet_flags(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Flags")
    _title(sheet, "Flags")
    at = _header(sheet, 3, ["Code", "Severity", "Message"])
    for flag in result.flags:
        sheet.cell(row=at, column=1, value=flag.code.value)
        sheet.cell(row=at, column=2, value=flag.severity.value)
        sheet.cell(row=at, column=3, value=flag.message)
        at += 1
    if not result.flags:
        sheet.cell(row=at, column=1, value="(none)")
    _widths(sheet, 34, 10, 120)


def build_workbook(
    screen_inputs: ScreenInputs,
    underwrite_inputs: UnderwriteInputs,
    result: UnderwriteResult,
    config: Config,
) -> Workbook:
    """The workbook for one underwritten deal, sheets in the order the math runs."""
    book = Workbook()
    book.remove(book.active)
    _sheet_inputs(book, screen_inputs, underwrite_inputs, config)
    _sheet_sizing(book, result.sizing, config)
    _sheet_lender(book, result, config)
    _sheet_grid(book, result)
    _sheet_borrower(book, result)
    _sheet_exit(book, result, config)
    _sheet_downside(book, result, config)
    _sheet_flags(book, result)
    return book


def write_workbook(
    path: Path,
    screen_inputs: ScreenInputs,
    underwrite_inputs: UnderwriteInputs,
    result: UnderwriteResult,
    config: Config,
) -> Path:
    """Build the workbook and save it; returns the path written."""
    build_workbook(screen_inputs, underwrite_inputs, result, config).save(path)
    return path
