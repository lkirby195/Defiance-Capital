"""One word for each §8.1 input on every team surface.  # SPEC §8.1, §9, §10 (v0.27)

``schema/labels.INPUT_LABELS`` is the one source. One fixture - the Tulsa team entry, which
the deal page, the workbook and the CLI all take - is rendered on each surface and searched:
the sales cost is "Sales cost" (or "Sales Cost") everywhere, its dollar line included, and
neither "Broker selling" nor a "Broker" caption is anywhere. The borrower's pages are not
among them; they read ``api/i18n.py``, where "Origination fee" reads better to a borrower.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cli.fixtures import run_fixture
from cli.main import main
from config.config import Config
from db.models import Deal
from outputs.excel import build_workbook
from schema.labels import INPUT_LABELS, input_label
from services.defaults import ECONOMICS
from tests.conftest import TEAM_ENTRY_WITH_OVERRIDES, QueueClient, requires_db

CONFIG = Config.load()
# "Broker" opening a caption: a page's text after a tag, a cell's or a report line's start.
BROKER_CAPTION = re.compile(r"(?:^|>)\s*Broker\b", re.MULTILINE)


def test_the_labels_are_the_economics_and_the_sales_cost_by_deals_column() -> None:
    assert set(INPUT_LABELS) == {*ECONOMICS, "broker_selling_pct"}
    assert set(INPUT_LABELS) <= set(Deal.__table__.columns.keys())
    assert input_label("broker_selling_pct") == "Sales cost"
    assert input_label("broker_selling_pct", title=True) == "Sales Cost"
    assert input_label("origination_fee_pct") == "Origination"
    assert input_label("holding_costs_pct_of_cost", title=True) == "Holding Costs"
    with pytest.raises(KeyError):
        input_label("monthly_rent")


def workbook_text(path: Path) -> str:
    """Every cell of the fixture's workbook, one per line."""
    run = run_fixture(path, CONFIG, with_underwrite=True)
    assert run.underwrite_inputs is not None and run.underwrite_result is not None
    book = build_workbook(run.screen_inputs, run.underwrite_inputs, run.underwrite_result, CONFIG)
    return "\n".join(
        str(cell.value)
        for sheet in book.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    )


@requires_db
def test_the_sales_cost_reads_the_same_on_every_team_surface(
    client: QueueClient,
    db_session: Session,
    deal_with_overrides: Deal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran = client.post(f"/queue/deals/{deal_with_overrides.id}/run", follow_redirects=False)
    assert ran.status_code == 303
    deal_page = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    capsys.readouterr()
    assert main(["run", str(TEAM_ENTRY_WITH_OVERRIDES), "--underwrite"]) == 0
    surfaces = {
        "the Defaults page": client.get("/defaults").text,
        "the deal page": deal_page,
        "the workbook": workbook_text(TEAM_ENTRY_WITH_OVERRIDES),
        "the CLI report": capsys.readouterr().out,
    }
    for name, text in surfaces.items():
        assert "broker selling" not in text.lower(), name
        assert "sales cost" in text.lower(), name
        # the caption the origination had on four of them; the dollar lines keep their words
        assert "Origination fee" not in text and "Origination Fee" not in text, name
        # the sales cost's dollar line is "Sales cost" too, not "Broker"
        assert not BROKER_CAPTION.search(text), (name, BROKER_CAPTION.findall(text))
    # each in its own case: the Flip and Take-Back cards are sentence case, the panel is not
    assert "<dt>Sales cost</dt>" in deal_page and "Sales Cost" in deal_page
    # the Flip card's two: the percentage on top, the dollar line in the profit
    flip = deal_page[deal_page.index('id="flip"') : deal_page.index('id="flip-profit"') + 400]
    assert flip.count("<dt>Sales cost</dt>") == 2
    # the CLI's one line is the dollar line, its note the percentage (no row of its own)
    (line,) = [
        one
        for one in surfaces["the CLI report"].splitlines()
        if one.strip().startswith("Sales cost")
    ]
    assert line.startswith("  ") and "%" in line
    del db_session
