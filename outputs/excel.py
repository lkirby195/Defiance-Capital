"""Write one run to an .xlsx workbook for manual verification of the math.  # SPEC §8, §9.6

Not a client deliverable: it is the engine's own arithmetic laid out so a person can check
it against a spreadsheet of their own. Every figure is written as a number with a currency,
percent, date or multiple format - never as preformatted text - so the cells add up, chart,
and compare. Sheets, in the SPEC §9 order:

    Inputs           what went in, screen and underwrite, plus the sizing it produced
    Return Overview  the monthly ledger, its totals, and an XIRR formula over it
    Flip             the project's margin if it sells
    Rental           whether the rent carries a takeout loan
    Take-Back        whether the rent carries what the loan cost GLENWOOD
    Flags            every flag with its severity and the threshold it names

The ``Return Overview`` sheet carries an **``XIRR`` formula** over the ledger's dates and its
``net`` column, beside the engine's own number. That is the point of the sheet: the workbook
recomputes the IRR itself, on the same Excel convention the engine solves to
(``engine/calc/irr.py``), so a reader sees the two agree instead of taking the engine's word
for it. ``tests/test_excel_export.py`` reads the dates and the net column back out and
checks the engine against an independent solve to 1e-6.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from config.config import Config
from schema.models import (
    SPLIT_PRODUCTS,
    AnalysisStatus,
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
DATE = "yyyy-mm-dd"

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
        sheet.column_dimensions[get_column_letter(index)].width = width


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
    result: UnderwriteResult,
    config: Config,
) -> None:
    sheet = book.create_sheet("Inputs")
    _title(sheet, "Inputs")
    deal = underwrite_inputs.deal
    borrower = underwrite_inputs.borrower
    economics = result.economics
    rows: list[Row] = [
        ("OVERVIEW", None, None, ""),
        ("Loan purpose", _value(underwrite_inputs.loan_purpose), None, ""),
        ("Loan type", deal.product.value, None, "the SPEC §3 product"),
        ("State", underwrite_inputs.state.value, None, ""),
        ("Closing date", underwrite_inputs.closing_date, DATE, "month 0 of the ledger"),
        ("Term (months)", result.term_months, INTEGER, ""),
        ("Payoff date", result.payoff_date, DATE, "closing date + term"),
        ("Rehab months", result.rehab_months, INTEGER, "term - listing months"),
        ("Credit (self-reported)", borrower.credit_range_self_reported.value, None, ""),
        ("Credit score (verified)", borrower.verified_credit_score, INTEGER, ""),
        ("Experience (self-reported)", borrower.experience_bucket_self_reported.value, None, ""),
        ("Deals in 36 mo (verified)", borrower.verified_deals_36mo, INTEGER, ""),
        ("Repeat borrower (self)", borrower.repeat_borrower_self_reported, None, ""),
        ("", None, None, ""),
        ("DEAL ECONOMICS", None, None, ""),
        ("Purchase price", economics.purchase_price, MONEY, ""),
        ("Rehab costs", economics.rehab_costs, MONEY, "before contingency"),
        ("Contingency", economics.contingency_pct, PCT1, "of the rehab costs"),
        ("Rehab + contingency", economics.rehab_adj, MONEY, "what the rehab portion is capped at"),
        ("Closing costs", economics.closing_costs, MONEY, "the lender's; in the LTC denominator"),
        (
            "Holding costs (total)",
            economics.holding_costs_total,
            MONEY,
            "over the whole hold",
        ),
        ("Holding costs (monthly)", economics.holding_costs_monthly, MONEY, "total / term"),
        ("Origination fee", economics.origination_fee_pct, PCT1, "half at close, half at payoff"),
        ("Interest rate", economics.interest_rate, PCT1, "annual"),
        ("Loan requested", economics.loan_requested, MONEY, ""),
        ("Loan purchase portion", economics.loan_purchase_portion, MONEY, "split products only"),
        ("Loan rehab portion", economics.loan_rehab_portion, MONEY, "holdback / Tranche A"),
        ("", None, None, ""),
        ("VALUATION, RENT AND TOGGLES", None, None, ""),
        ("As-is value", deal.as_is_value, MONEY, _source(deal.as_is_value_source)),
        (
            "Estimated sale price",
            deal.estimated_sale_price,
            MONEY,
            _source(deal.estimated_sale_price_source),
        ),
        ("Monthly rent", underwrite_inputs.monthly_rent, MONEY, "blank = no DSCR at all"),
        ("Asset type", _value(underwrite_inputs.asset_type), None, "drives the exit inference"),
        ("Stated exit", underwrite_inputs.stated_exit.value, None, "a stated exit always wins"),
        ("Exit in force", result.exit.type.value, None, result.exit.exit_source.value.lower()),
        (
            "Flip analysis",
            "on" if result.exit.flip_analysis else "off",
            None,
            f"default for this exit: {'on' if result.exit.flip_analysis_default else 'off'}",
        ),
        (
            "Rental analysis",
            "on" if result.exit.rental_analysis else "off",
            None,
            f"default for this exit: {'on' if result.exit.rental_analysis_default else 'off'}",
        ),
        (
            "Court records",
            _value(
                underwrite_inputs.court_records.source if underwrite_inputs.court_records else None
            ),
            None,
            "SPEC §7.2 tests re-run here; blank when no source was checked",
        ),
        ("", None, None, ""),
        ("SIZING", None, None, ""),
        ("Total cost", result.sizing.total_cost, MONEY, "LTC denominator"),
        ("Commitment", result.sizing.commitment, MONEY, ""),
        ("Funded at close", economics.funded_at_close, MONEY, ""),
    ]
    rows += _split_rows(result.sizing)
    rows += [
        ("", None, None, ""),
        ("SCREEN INPUTS", None, None, ""),
        (
            "Screen: as-is value",
            screen_inputs.deal.as_is_value,
            MONEY,
            _source(screen_inputs.deal.as_is_value_source),
        ),
        (
            "Screen: estimated sale price",
            screen_inputs.deal.estimated_sale_price,
            MONEY,
            _source(screen_inputs.deal.estimated_sale_price_source),
        ),
        (
            "Screen: court records",
            _value(screen_inputs.court_records.source if screen_inputs.court_records else None),
            None,
            "blank when no source was checked",
        ),
        ("", None, None, ""),
        ("CONFIG", None, None, ""),
        ("Config: broker selling", config.fees.broker_selling_pct, PCT1, "flip only"),
        ("Config: listing months", config.draws.listing_months, INTEGER, ""),
        ("Config: rental expenses", config.rental.expenses_pct_of_rent, PCT1, "of rent"),
        ("Config: rental takeout rate", config.rental.takeout_rate, PCT1, ""),
        ("Config: rental DSCR floor", config.rental.dscr_floor, RATIO, ""),
        ("Config: lost interest months", config.take_back.lost_interest_months, INTEGER, ""),
        ("Config: legal costs", config.take_back.legal_costs_usd, MONEY, ""),
        ("Config: take-back DSCR floor", config.take_back.dscr_floor, RATIO, ""),
    ]
    at = _rows(sheet, rows) + 1
    at = _header(sheet, at, ["Metric", "Actual", "Cap", "Limit", "Status", "Basis"])
    for metric, check in result.sizing.metrics.items():
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
    _widths(sheet, 30, 18, 42, 12, 18, 34)


def _value(value: Any) -> Any:
    """An enum as its stored value; anything else unchanged, None included."""
    return None if value is None else getattr(value, "value", value)


def _split_rows(sizing: SizingResult) -> list[Row]:
    """The purchase / rehab split, or a line saying there is not one."""
    if sizing.split is None:
        if sizing.product in SPLIT_PRODUCTS:
            return [
                ("Loan split", None, None, "not entered; sized on the loan requested (SPEC §8.2)")
            ]
        return []
    purchase, rehab = (
        ("Principal Note", "Tranche A")
        if sizing.product is Product.SPLIT_PRINCIPAL
        else ("Purchase portion", "Rehab holdback")
    )
    note = (
        f"entered {sizing.split.rehab_portion_requested}, capped at the "
        "contingency-adjusted rehab cost"
        if sizing.split.rehab_portion_capped
        else ""
    )
    return [
        (purchase, sizing.split.purchase_portion, MONEY, ""),
        (rehab, sizing.split.rehab_portion, MONEY, note),
    ]


def _sheet_return_overview(book: Workbook, result: UnderwriteResult) -> None:
    """The ledger, its totals, and an XIRR formula the workbook solves itself.  # SPEC §8.3"""
    sheet = book.create_sheet("Return Overview")
    _title(sheet, "Return Overview — the lender's monthly cash flows")
    sheet.cell(
        row=2,
        column=1,
        value=(
            f"{result.term_months} months from {result.closing_date.isoformat()} to "
            f"{result.payoff_date.isoformat()}; money out is negative. The XIRR below is "
            "Excel's own, over column B and column H."
        ),
    )
    at = _header(
        sheet, 4, ["Date", "Month", "Funding", "Draws", "Interest", "Fees", "Payoff", "Net"]
    )
    first = at
    for entry in result.return_overview.entries:
        sheet.cell(row=at, column=1, value=entry.date).number_format = DATE
        sheet.cell(row=at, column=2, value=entry.month).number_format = INTEGER
        for column, amount in enumerate(
            (entry.funding, entry.draws, entry.interest, entry.fees, entry.payoff, entry.net),
            start=3,
        ):
            sheet.cell(row=at, column=column, value=amount).number_format = MONEY
        at += 1
    last = at - 1

    sheet.cell(row=at, column=1, value="Total").font = HEAD
    overview = result.return_overview
    for column, total in enumerate(
        (
            overview.total_funding,
            overview.total_draws,
            overview.total_interest,
            overview.total_fees,
            overview.total_payoff,
            overview.total_profit,
        ),
        start=3,
    ):
        cell = sheet.cell(row=at, column=column, value=total)
        cell.number_format = MONEY
        cell.font = HEAD
    at += 2

    rows: list[Row] = [
        ("Total interest", overview.total_interest, MONEY, ""),
        ("Total fees", overview.total_fees, MONEY, "both origination halves"),
        ("Total profit", overview.total_profit, MONEY, "the sum of the Net column"),
        ("IRR (engine)", overview.irr, PCT4, "engine/calc/irr.py, Newton + bisection"),
    ]
    at = _rows(sheet, rows, start=at)
    sheet.cell(row=at, column=1, value="IRR (this workbook)")
    formula = sheet.cell(
        row=at,
        column=2,
        value=f"=XIRR(H{first}:H{last},A{first}:A{last})",
    )
    formula.number_format = PCT4
    sheet.cell(
        row=at,
        column=3,
        value="Excel solves this itself from the dates and the Net column; it should equal "
        "the engine's IRR above.",
    )
    _widths(sheet, 14, 8, 15, 15, 13, 12, 15, 15)


def _sheet_flip(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Flip")
    _title(sheet, "Flip Analysis — the project's margin if it sells")
    flip = result.flip
    rows: list[Row] = [
        ("Status", flip.status.value, None, _flip_note(flip.status)),
        ("Estimated sale price", flip.estimated_sale_price, MONEY, ""),
        ("Broker selling", flip.broker_selling_pct, PCT1, "of the sale price"),
        ("Broker selling costs", flip.broker_costs, MONEY, "off the price, not in total costs"),
        ("Purchase price", flip.purchase_price, MONEY, ""),
        ("Closing costs", flip.closing_costs, MONEY, ""),
        ("Holding costs", flip.holding_costs_total, MONEY, "total over the hold"),
        ("Rehab costs", flip.rehab_costs, MONEY, ""),
        ("Contingency", flip.contingency, MONEY, ""),
        ("Financing costs", flip.financing_costs, MONEY, "total interest + both fee halves"),
        ("Total costs", flip.total_costs, MONEY, ""),
        ("Net profit", flip.net_profit, MONEY, "sale price - broker - total costs"),
        (
            "Yield (Profit / Costs)",
            flip.profit_yield,
            PCT1,
            "a project margin, not an annualized return",
        ),
    ]
    _rows(sheet, rows)
    _widths(sheet, 26, 18, 44)


def _flip_note(status: AnalysisStatus) -> str:
    if status is AnalysisStatus.OFF:
        return "the exit is not a resale, so the flip was not asked for"
    if status is AnalysisStatus.NOT_EVALUATED:
        return "no estimated sale price; the cost stack below still stands"
    return ""


def _rent_note(status: AnalysisStatus, off: str) -> str:
    if status is AnalysisStatus.OFF:
        return off
    if status is AnalysisStatus.NOT_EVALUATED:
        return "no monthly rent, so there is no net monthly income and no DSCR to test"
    return ""


def _sheet_rental(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Rental")
    _title(sheet, "Rental Analysis — does the rent carry a takeout loan")
    rental = result.rental
    rows: list[Row] = [
        (
            "Status",
            rental.status.value,
            None,
            _rent_note(rental.status, "the exit is not a hold and no rent was entered"),
        ),
        ("Monthly rent", rental.monthly_rent, MONEY, ""),
        ("Expenses", rental.expenses_pct, PCT1, "of rent"),
        ("Expenses (monthly)", rental.expenses, MONEY, ""),
        ("Holding costs (monthly)", rental.holding_costs_monthly, MONEY, ""),
        ("Net monthly income", rental.net_monthly_income, MONEY, "rent - expenses - holding"),
        ("Loan amount", rental.loan_amount, MONEY, "the commitment"),
        ("Takeout rate", rental.takeout_rate, PCT1, ""),
        ("Amortization (years)", rental.amortization_years, INTEGER, ""),
        ("Debt service (monthly)", rental.debt_service_monthly, MONEY, "level payment"),
        ("DSCR", rental.dscr, RATIO, ""),
        ("DSCR floor", rental.dscr_floor, RATIO, ""),
        ("Passed", rental.passed, None, ""),
    ]
    _rows(sheet, rows)
    _widths(sheet, 26, 18, 44)


def _sheet_take_back(book: Workbook, result: UnderwriteResult) -> None:
    sheet = book.create_sheet("Take-Back")
    _title(sheet, "Take-Back Analysis — does the rent carry what the loan cost GLENWOOD")
    take_back = result.take_back
    rows: list[Row] = [
        ("Status", take_back.status.value, None, _rent_note(take_back.status, "")),
        ("Loan amount", take_back.loan_amount, MONEY, "the commitment"),
        ("Interest rate", take_back.interest_rate, PCT1, "the deal's own, not a takeout rate"),
        ("Lost interest (months)", take_back.lost_interest_months, INTEGER, ""),
        ("Lost interest", take_back.lost_interest, MONEY, "interest GLENWOOD stops collecting"),
        ("Legal costs", take_back.legal_costs, MONEY, ""),
        ("Total cost", take_back.total_cost, MONEY, "loan + lost interest + legal"),
        ("Amortization (years)", take_back.amortization_years, INTEGER, ""),
        ("Debt service (monthly)", take_back.debt_service_monthly, MONEY, "on the total cost"),
        ("Net monthly income", take_back.net_monthly_income, MONEY, "as in the Rental sheet"),
        ("DSCR at loan cost", take_back.dscr, RATIO, ""),
        ("DSCR floor", take_back.dscr_floor, RATIO, ""),
        ("Passed", take_back.passed, None, ""),
    ]
    _rows(sheet, rows)
    _widths(sheet, 26, 18, 44)


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
    """The workbook for one underwritten deal, sheets in the SPEC §9 order."""
    book = Workbook()
    book.remove(book.active)
    _sheet_inputs(book, screen_inputs, underwrite_inputs, result, config)
    _sheet_return_overview(book, result)
    _sheet_flip(book, result)
    _sheet_rental(book, result)
    _sheet_take_back(book, result)
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
