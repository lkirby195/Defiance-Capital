"""outputs/excel.py: the workbook a person checks the math in.  # SPEC §9.6

Two things are being asserted. First that every figure is written as a *number* under a
format - a workbook of preformatted strings adds up to nothing and is the failure mode this
export exists to avoid. Second, and the reason the Return Overview sheet is shaped the way it
is, that the ledger it writes down really does produce the IRR the engine claims: the test
reads the dates and the ``net`` column back out of the sheet, solves the XIRR itself with an
implementation that shares no code with ``engine/calc/irr.py``, and requires the two to agree
to 1e-6. The sheet carries Excel's own ``XIRR`` formula over the same two ranges, so a reader
opening the file sees the same check recomputed in front of them.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from cli.fixtures import FixtureRun, run_fixture
from config.config import Config
from outputs.excel import build_workbook, write_workbook

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
CONFIG = Config.load()
D = Decimal

SHEETS = ["Inputs", "Return Overview", "Flip", "Rental", "Take-Back", "Flags"]


def underwritten() -> list[Path]:
    return [
        path
        for path in sorted(FIXTURE_DIR.glob("*.json"))
        if "underwrite" in json.loads(path.read_text(encoding="utf-8"))
    ]


UNDERWRITE_FILES = underwritten()


def run(path: Path) -> FixtureRun:
    return run_fixture(path, CONFIG, with_underwrite=True)


def book_for(path: Path) -> Any:
    fixture = run(path)
    assert fixture.underwrite_inputs is not None and fixture.underwrite_result is not None
    return build_workbook(
        fixture.screen_inputs, fixture.underwrite_inputs, fixture.underwrite_result, CONFIG
    )


def xirr_by_secant(flows: list[tuple[date, float]]) -> float:
    """An independent XIRR: plain floats, the secant method, no shared code with the engine.

    Deliberately not Newton and deliberately not Decimal - if this and the engine agree to a
    millionth, they are agreeing about the arithmetic rather than about an implementation.
    """
    start = flows[0][0]
    years = [(when - start).days / 365.0 for when, _ in flows]
    amounts = [amount for _, amount in flows]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** t for a, t in zip(amounts, years, strict=True))

    low, high = 0.0, 0.2
    f_low, f_high = npv(low), npv(high)
    for _ in range(200):
        if f_high == f_low:
            break
        nxt = high - f_high * (high - low) / (f_high - f_low)
        if nxt <= -0.999:
            nxt = (high - 0.999) / 2
        low, f_low, high = high, f_high, nxt
        f_high = npv(high)
        if abs(high - low) < 1e-14:
            break
    return high


def ledger_rows(sheet: Worksheet) -> list[tuple[date, float]]:
    """The dates and the net column, read back out of the sheet as a person would."""
    rows: list[tuple[date, float]] = []
    for row in sheet.iter_rows(min_row=5, max_col=9):
        when, net = row[0].value, row[8].value
        if not isinstance(when, datetime | date) or not isinstance(net, int | float | Decimal):
            break
        rows.append((when.date() if isinstance(when, datetime) else when, float(net)))
    return rows


def find_row(sheet: Worksheet, label: str) -> tuple[Any, Any]:
    for row in sheet.iter_rows(min_col=1, max_col=3):
        if row[0].value == label:
            return row[1].value, row[1].number_format
    raise AssertionError(f"no row labelled {label!r} on {sheet.title}")


# --- the sheets (SPEC §9.6) ----------------------------------------------------------------------


def test_the_workbook_has_the_six_sheets_in_the_spec_order() -> None:
    assert book_for(UNDERWRITE_FILES[0]).sheetnames == SHEETS


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_every_fixture_exports(path: Path) -> None:
    assert book_for(path).sheetnames == SHEETS


def test_write_workbook_saves_a_file_that_opens(tmp_path: Path) -> None:
    fixture = run(UNDERWRITE_FILES[0])
    assert fixture.underwrite_inputs is not None and fixture.underwrite_result is not None
    out = tmp_path / "deal.xlsx"
    written = write_workbook(
        out, fixture.screen_inputs, fixture.underwrite_inputs, fixture.underwrite_result, CONFIG
    )
    assert written == out and out.exists()
    assert load_workbook(out).sheetnames == SHEETS


# --- numbers as numbers --------------------------------------------------------------------------


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_the_ledger_is_written_as_dates_and_numbers(path: Path) -> None:
    sheet = book_for(path)["Return Overview"]
    result = run(path).underwrite_result
    assert result is not None
    at = 5
    for entry in result.return_overview.entries:
        when = sheet.cell(row=at, column=1)
        assert isinstance(when.value, datetime | date)
        assert when.number_format == "yyyy-mm-dd"
        assert sheet.cell(row=at, column=2).value == entry.month
        # The stub column is blank on a whole period and carries its days on the short one.
        assert sheet.cell(row=at, column=3).value == (entry.stub_days or None)
        for column, amount in enumerate(
            (entry.funding, entry.draws, entry.interest, entry.fees, entry.payoff, entry.net),
            start=4,
        ):
            cell = sheet.cell(row=at, column=column)
            assert isinstance(cell.value, Decimal | int | float), (at, column)
            assert Decimal(str(cell.value)) == amount
            assert cell.number_format == '"$"#,##0.00'
        at += 1


def test_percentages_are_percentages_and_ratios_are_ratios() -> None:
    book = book_for(UNDERWRITE_FILES[0])
    _, fmt = find_row(book["Inputs"], "Interest rate")
    assert fmt == "0.0%"
    _, fmt = find_row(book["Flip"], "Yield (Profit / Costs)")
    assert fmt == "0.0%"
    _, fmt = find_row(book["Rental"], "DSCR floor")
    assert fmt == '0.00"x"'
    _, fmt = find_row(book["Inputs"], "Closing date")
    assert fmt == "yyyy-mm-dd"


def test_no_money_cell_is_a_string() -> None:
    """The failure this export exists to avoid: a workbook of text that adds up to nothing."""
    for path in UNDERWRITE_FILES:
        book = book_for(path)
        for name in SHEETS:
            for row in book[name].iter_rows():
                for cell in row:
                    if cell.number_format == '"$"#,##0.00' and cell.value is not None:
                        assert isinstance(cell.value, Decimal | int | float), (
                            name,
                            cell.coordinate,
                        )


# --- the IRR the workbook recomputes for itself (SPEC §9.6) --------------------------------------


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_the_sheet_carries_an_xirr_formula_over_its_own_ledger(path: Path) -> None:
    sheet = book_for(path)["Return Overview"]
    result = run(path).underwrite_result
    assert result is not None
    last = 4 + len(result.return_overview.entries)
    formula, fmt = find_row(sheet, "IRR (this workbook)")
    assert formula == f"=XIRR(I5:I{last},A5:A{last})"
    assert fmt == "0.0000%"
    # ...over exactly the rows the ledger occupies, and no others
    assert len(ledger_rows(sheet)) == len(result.return_overview.entries)


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_the_workbook_ledger_solves_to_the_engines_irr(path: Path) -> None:
    """Read the sheet's own dates and net column back out and solve them independently."""
    sheet = book_for(path)["Return Overview"]
    result = run(path).underwrite_result
    assert result is not None and result.return_overview.irr is not None

    from_sheet = xirr_by_secant(ledger_rows(sheet))
    assert abs(from_sheet - float(result.return_overview.irr)) < 1e-6, path.stem

    engine_irr, fmt = find_row(sheet, "IRR (engine)")
    assert fmt == "0.0000%"
    assert abs(float(engine_irr) - from_sheet) < 1e-6


# --- the rest of the sheets ----------------------------------------------------------------------


def test_the_inputs_sheet_names_where_each_valuation_came_from() -> None:
    book = book_for(FIXTURE_DIR / "go_team_overrides_tulsa.json")
    sheet = book["Inputs"]
    notes = {row[0].value: row[2].value for row in sheet.iter_rows(min_col=1, max_col=3)}
    assert notes["Estimated sale price"] == "team, by hand"
    assert "As-is value" not in notes  # SPEC §7.4: one value ratio, one valuation


def test_an_analysis_that_did_not_run_says_why_on_its_own_sheet() -> None:
    book = book_for(FIXTURE_DIR / "conditional_no_price_no_rent.json")
    status, _ = find_row(book["Rental"], "Status")
    assert status == "OFF"
    status, _ = find_row(book["Take-Back"], "Status")
    assert status == "NOT_EVALUATED"
    take_back = book["Take-Back"]
    notes = {row[0].value: row[2].value for row in take_back.iter_rows(min_col=1, max_col=3)}
    assert "no monthly rent" in notes["Status"]
    assert notes["Toggle"] == "it runs on every deal (SPEC §8.6)"
    flip = book["Flip"]
    status, _ = find_row(flip, "Status")
    assert status == "NOT_EVALUATED"
    flip_notes = {row[0].value: row[2].value for row in flip.iter_rows(min_col=1, max_col=3)}
    assert "no estimated sale price" in flip_notes["Status"]


def test_the_flip_sheet_reports_the_cost_stack_even_when_the_toggle_is_off() -> None:
    book = book_for(FIXTURE_DIR / "go_hold_flip_off_denver.json")
    status, _ = find_row(book["Flip"], "Status")
    assert status == "OFF"
    total_costs, fmt = find_row(book["Flip"], "Total costs")
    assert total_costs > 0 and fmt == '"$"#,##0.00'
    net_profit, _ = find_row(book["Flip"], "Net profit")
    assert net_profit is None


def test_the_flags_sheet_lists_every_flag_with_its_severity_and_message() -> None:
    path = FIXTURE_DIR / "go_no_draw_wichita.json"
    result = run(path).underwrite_result
    assert result is not None
    sheet = book_for(path)["Flags"]
    rows = [
        (row[0].value, row[1].value, row[2].value)
        for row in sheet.iter_rows(min_row=4, max_col=3)
        if row[0].value
    ]
    assert len(rows) == len(result.flags)
    for (code, severity, message), flag in zip(rows, result.flags, strict=True):
        assert code == flag.code.value
        assert severity == flag.severity.value
        assert message == flag.message


def test_a_run_with_no_flags_says_so() -> None:
    sheet = book_for(FIXTURE_DIR / "go_hold_flip_off_denver.json")["Flags"]
    assert sheet.cell(row=4, column=1).value == "(none)"
