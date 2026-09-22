"""The exported workbook carries the engine's numbers, as numbers.  # SPEC §8

Opening the file back up and reading the cells is the only assertion worth making here:
the workbook exists to be checked by hand, so a cell that says "$105,000.00" but holds a
string, or holds a value the engine never produced, is the failure mode.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from cli.fixtures import run_fixture
from cli.main import main
from config.config import Config
from schema.models import TakeoutStatus, UnderwriteResult

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
CONFIG = Config.load()
D = Decimal
SHEETS = ["Inputs", "Sizing", "Lender", "Grid", "Borrower", "Exit", "Downside", "Flags"]


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


UNDERWRITE_FILES = [p for p in sorted(FIXTURE_DIR.glob("*.json")) if "underwrite" in load(p)]
IDS = [p.stem for p in UNDERWRITE_FILES]


def labelled(sheet: Worksheet) -> dict[str, Any]:
    """Every label / value pair on a label-value sheet."""
    found: dict[str, Any] = {}
    for row in sheet.iter_rows(min_col=1, max_col=2):
        label, value = row[0].value, row[1].value
        if isinstance(label, str) and label and label not in found:
            found[label] = value
    return found


def formats(sheet: Worksheet) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in sheet.iter_rows(min_col=1, max_col=2):
        label, cell = row[0].value, row[1]
        if isinstance(label, str) and label and label not in found:
            found[label] = cell.number_format
    return found


def close(value: Any, expected: Decimal) -> bool:
    """Excel holds ~15 significant digits; compare at the precision a person would read."""
    assert value is not None
    return abs(D(str(value)) - expected) <= D("0.0000001") * max(D(1), abs(expected))


@pytest.fixture(params=UNDERWRITE_FILES, ids=IDS)
def exported(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Any, UnderwriteResult]:
    """Export one fixture through the CLI and hand back the workbook and the engine result."""
    path: Path = request.param
    out = tmp_path / f"{path.stem}.xlsx"
    assert main(["export", str(path), str(out)]) == 0
    run = run_fixture(path, CONFIG, with_underwrite=True)
    assert run.underwrite_result is not None
    return load_workbook(out), run.underwrite_result


def test_workbook_has_every_sheet(exported: tuple[Any, UnderwriteResult]) -> None:
    book, _ = exported
    assert book.sheetnames == SHEETS


def test_sizing_sheet_matches_the_engine(exported: tuple[Any, UnderwriteResult]) -> None:
    book, result = exported
    sheet = book["Sizing"]
    values = labelled(sheet)
    sizing = result.sizing
    assert close(values["Commitment"], sizing.commitment)
    assert close(values["Total cost"], sizing.total_cost)
    assert close(values["Buy-side closing"], sizing.buy_closing)
    assert close(values["Rehab + contingency"], sizing.rehab_adj)
    assert close(values["Funded at close"], sizing.funded_at_close)
    assert formats(sheet)["Commitment"] == '"$"#,##0.00'
    # the metric block carries actual, cap and limit for each metric
    rows = {row[0].value: row for row in sheet.iter_rows(min_col=1, max_col=5)}
    for metric, check in sizing.metrics.items():
        row = rows[metric.value]
        if check.actual is None:
            assert row[1].value is None
        else:
            assert close(row[1].value, check.actual)
        assert close(row[2].value, check.cap)
        assert close(row[3].value, check.cap + check.tolerance_band)
        assert row[4].value == check.status.value


def test_lender_sheet_matches_the_engine(exported: tuple[Any, UnderwriteResult]) -> None:
    book, result = exported
    sheet = book["Lender"]
    values, fmts = labelled(sheet), formats(sheet)
    lender = result.lender_at_solve
    assert values["Term (months)"] == result.term_months
    assert values["Rehab months"] == result.rehab_months
    assert close(values["Solved rate r*"], result.solved_rate)
    assert close(values["Interest at r*"], lender.interest)
    assert close(values["Fees total"], lender.fees.total)
    assert close(values["Origination at close"], lender.fees.origination_at_close)
    assert close(values["Origination at payoff"], lender.fees.origination_at_payoff)
    assert close(values["Extension fee"], lender.fees.extension)
    assert close(values["Average outstanding"], lender.avg_outstanding)
    assert close(values["Yield at (term, r*)"], result.lender_yield_at_solve)
    assert fmts["Solved rate r*"] == "0.0000%"
    assert fmts["Interest at r*"] == '"$"#,##0.00'
    # numbers, not strings: the yield cell can be compared to the target arithmetically
    assert D(str(values["Yield at (term, r*)"])).quantize(D("0.000001")) == D("0.175000")


def test_grid_sheet_reproduces_every_cell(exported: tuple[Any, UnderwriteResult]) -> None:
    book, result = exported
    sheet = book["Grid"]
    grid = result.grid_lender
    header = [cell.value for cell in sheet[4]][1:]
    assert len(header) == len(grid.rates)
    for written, rate in zip(header, grid.rates, strict=True):
        assert close(written, rate)
    for index, grid_row in enumerate(grid.rows):
        row = [cell.value for cell in sheet[5 + index]]
        assert row[0] == grid_row.month
        for written, cell in zip(row[1:], grid_row.cells, strict=True):
            assert close(written, cell.annualized_yield)
    # the r* column is marked and the target is written as a number
    marks = [cell.value for cell in sheet[3]]
    assert marks.count("r*") == 1
    assert close(labelled(sheet)["target"], grid.target)
    assert close(labelled(sheet)["r*"], grid.solved_rate)


def test_borrower_exit_and_downside_sheets_match_the_engine(
    exported: tuple[Any, UnderwriteResult],
) -> None:
    book, result = exported

    borrower = labelled(book["Borrower"])
    economics = result.borrower_at_solve
    assert close(borrower["Total project cost"], economics.total_project_cost)
    assert close(borrower["Interest paid"], economics.interest_paid)
    assert close(borrower["Holding costs"], economics.holding_costs)
    assert close(borrower["Exit net of selling costs"], economics.exit_net)
    assert close(borrower["Profit"], economics.profit)
    assert close(borrower["Cash in"], economics.cash_in)
    if economics.cash_on_cash is None:
        assert borrower["Cash on cash"] is None
    else:
        assert close(borrower["Cash on cash"], economics.cash_on_cash)

    exit_values = labelled(book["Exit"])
    exit_result = result.exit
    assert exit_values["Exit type"] == exit_result.type.value
    assert exit_values["Takeout"] == exit_result.status.value
    assert close(exit_values["Annual taxes"], exit_result.annual_taxes)
    assert close(exit_values["Annual insurance"], exit_result.annual_insurance)
    assert close(exit_values["Payoff due"], exit_result.payoff_due)
    if exit_result.status is TakeoutStatus.NOT_EVALUATED:
        # the workbook leaves them blank rather than writing a zero somebody would read
        for label in ("NOI (annual)", "Max takeout", "Shortfall", "Refi covers"):
            assert exit_values[label] is None, label
    else:
        assert close(exit_values["NOI (annual)"], exit_result.noi_annual)
        assert close(exit_values["Max takeout"], exit_result.max_takeout)
        assert close(exit_values["Shortfall"], exit_result.shortfall)
        assert exit_values["Refi covers"] is exit_result.refi_covers

    downside = labelled(book["Downside"])
    reo = result.downside
    assert close(downside["Recovery basis"], reo.recovery_basis)
    assert close(downside["Liquidation"], reo.liquidation)
    assert close(downside["Recovery"], reo.recovery)
    assert close(downside["Exposure"], reo.exposure)
    assert close(downside["Cover"], reo.cover)
    assert downside["Foreclosure months"] == reo.foreclosure_months
    assert downside["Passed"] is reo.passed


def test_flags_sheet_lists_every_flag(exported: tuple[Any, UnderwriteResult]) -> None:
    book, result = exported
    sheet = book["Flags"]
    written = [
        (row[0].value, row[1].value, row[2].value)
        for row in sheet.iter_rows(min_row=4, min_col=1, max_col=3)
        if row[0].value not in (None, "(none)")
    ]
    assert written == [(f.code.value, f.severity.value, f.message) for f in result.flags]


def test_inputs_sheet_carries_what_went_in(exported: tuple[Any, UnderwriteResult]) -> None:
    book, result = exported
    values = labelled(book["Inputs"])
    assert values["Product"] == result.sizing.product.value
    assert values["Term (months)"] == result.term_months
    assert close(values["Config: target IRR"], CONFIG.returns.target_irr)
    assert close(values["Config: buy-side closing"], CONFIG.fees.borrower_closing_pct_of_price)


def test_export_refuses_a_fixture_with_no_underwrite_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    screen_only = next(p for p in sorted(FIXTURE_DIR.glob("*.json")) if "underwrite" not in load(p))
    out = tmp_path / "nope.xlsx"
    assert main(["export", str(screen_only), str(out)]) == 2
    assert "no 'underwrite' block" in capsys.readouterr().err
    assert not out.exists()


def test_every_money_and_rate_cell_is_a_number_not_a_string(
    exported: tuple[Any, UnderwriteResult],
) -> None:
    book, _ = exported
    for name in ("Sizing", "Lender", "Borrower", "Exit", "Downside", "Grid"):
        for row in book[name].iter_rows():
            for cell in row:
                if cell.number_format in ('"$"#,##0.00', "0.0%", "0.0000%", '0.00"x"'):
                    assert cell.value is None or isinstance(cell.value, int | float | Decimal), (
                        f"{name}!{cell.coordinate} is {type(cell.value)}"
                    )
