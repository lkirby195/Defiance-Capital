"""``glenwood run`` on every fixture, with no database and no network.  # SPEC §12 Phase 2"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.fixtures import FixtureError, run_fixture
from cli.main import main
from config.config import Config

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))
IDS = [p.stem for p in FIXTURE_FILES]
CONFIG = Config.load()


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


UNDERWRITE_FILES = [p for p in FIXTURE_FILES if "underwrite" in load(p)]
UNDERWRITE_IDS = [p.stem for p in UNDERWRITE_FILES]


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=IDS)
def test_run_prints_the_screen_for_every_fixture(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(path)]) == 0
    out = capsys.readouterr().out
    fixture = load(path)
    assert fixture["name"] in out
    assert f"verdict: {fixture['expected']['verdict']}" in out
    assert "SIZING (screen)" in out
    assert "Commitment" in out
    # every reason the screen gave is on the page
    for flag in fixture["expected"]["flags"]:
        assert flag["code"] in out or flag["severity"].title() in out


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=UNDERWRITE_IDS)
def test_run_underwrite_prints_every_section(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(path), "--underwrite"]) == 0
    out = capsys.readouterr().out
    for section in (
        "DEAL ECONOMICS",
        "RETURN OVERVIEW",
        "FLIP ANALYSIS",
        "RENTAL ANALYSIS",
        "TAKE-BACK ANALYSIS",
        "FLAGS",
    ):
        assert section in out, section
    expected = load(path)["underwrite"]["expected"]
    assert "IRR" in out and "Yield (Profit / Costs)" in out
    assert "DSCR at loan cost" in out
    # the numbers on the page are the numbers the fixture pins
    plain = out.replace("$", "").replace(",", "")
    assert expected["ledger"]["total_interest"] in plain
    assert expected["ledger"]["total_profit"] in plain
    assert expected["take_back"]["total_cost"] in plain
    assert expected["payoff_date"] in out
    for flag in expected["flags"]:
        assert flag["code"] in out


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=UNDERWRITE_IDS)
def test_run_underwrite_matches_the_engine(path: Path) -> None:
    run = run_fixture(path, CONFIG, with_underwrite=True)
    expected = load(path)["underwrite"]["expected"]
    assert run.underwrite_result is not None
    assert run.underwrite_result.term_months == run.underwrite_inputs.term_months  # type: ignore[union-attr]
    assert run.underwrite_result.rehab_months == expected["rehab_months"]


def test_underwrite_is_refused_where_the_fixture_has_no_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    screen_only = next(p for p in FIXTURE_FILES if "underwrite" not in load(p))
    assert main(["run", str(screen_only), "--underwrite"]) == 2
    assert "no 'underwrite' block" in capsys.readouterr().err


def test_a_missing_or_unreadable_file_is_reported_not_traced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(tmp_path / "nope.json")]) == 2
    assert "cannot read" in capsys.readouterr().err

    not_json = tmp_path / "bad.json"
    not_json.write_text("{ not json", encoding="utf-8")
    assert main(["run", str(not_json)]) == 2
    assert "not valid JSON" in capsys.readouterr().err

    wrong_shape = tmp_path / "other.json"
    wrong_shape.write_text('{"hello": 1}', encoding="utf-8")
    assert main(["run", str(wrong_shape)]) == 2
    assert "not a deal fixture" in capsys.readouterr().err


def test_the_irr_is_printed_at_full_precision(capsys: pytest.CaptureFixture[str]) -> None:
    """The IRR is a solve, so it is not rounded to a whole basis point."""
    denver = FIXTURE_DIR / "go_split_draw_denver.json"
    assert main(["run", str(denver), "--underwrite"]) == 0
    assert "17.6312%" in capsys.readouterr().out


def test_the_ledger_prints_a_row_for_every_month(capsys: pytest.CaptureFixture[str]) -> None:
    denver = FIXTURE_DIR / "go_split_draw_denver.json"
    assert main(["run", str(denver), "--underwrite"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-01" in out and "2027-07-01" in out  # closing and payoff
    for month in range(10):
        assert f"  {month:>3}" in out or f"{month} 20" in out
    assert "Total" in out


def test_a_term_with_no_rehab_period_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    short = FIXTURE_DIR / "go_split_principal_no_rehab_period_okc.json"
    assert main(["run", str(short), "--underwrite"]) == 0
    out = capsys.readouterr().out
    assert "NO_REHAB_PERIOD" in out
    assert "advanced at close rather than drawn" in out


def test_an_analysis_that_did_not_run_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    no_rent = FIXTURE_DIR / "conditional_no_rent_no_draw.json"
    assert main(["run", str(no_rent), "--underwrite"]) == 0
    out = capsys.readouterr().out
    assert "RENTAL ANALYSIS" in out and "OFF" in out
    assert "NOT EVALUATED: no monthly rent" in out
    assert "MONTHLY_RENT_MISSING" in out


def test_run_fixture_refuses_an_underwrite_it_was_not_given(tmp_path: Path) -> None:
    screen_only = next(p for p in FIXTURE_FILES if "underwrite" not in load(p))
    with pytest.raises(FixtureError):
        run_fixture(screen_only, CONFIG, with_underwrite=True)
    run = run_fixture(screen_only, CONFIG, with_underwrite=False)
    assert run.underwrite_result is None


# --- team-entry fixtures run through the service assembly (SPEC §6) ------------------------------

TEAM_FIXTURES = [p for p in FIXTURE_FILES if "team_entry" in load(p)]
TEAM_IDS = [p.stem for p in TEAM_FIXTURES]


def test_at_least_one_fixture_goes_through_the_team_entry_path() -> None:
    assert TEAM_FIXTURES, "no fixture exercises services.assemble"


@pytest.mark.parametrize("path", TEAM_FIXTURES, ids=TEAM_IDS)
def test_a_team_entry_fixture_is_assembled_not_handed_straight_in(path: Path) -> None:
    """It goes through normalize -> a deals row -> services.assemble, as the API does."""
    run = run_fixture(path, CONFIG, with_underwrite=False)
    assert run.deal is not None
    assert run.deal.product is not None  # inferred by the normalizer, not stated in the fixture
    assert run.screen_inputs.deal.purchase_price == run.deal.purchase_price
    assert run.screen_inputs.state is run.deal.property.state


@pytest.mark.parametrize("path", TEAM_FIXTURES, ids=TEAM_IDS)
def test_a_team_entry_fixture_reaches_its_expected_verdict(path: Path) -> None:
    fixture = load(path)
    run = run_fixture(path, CONFIG, with_underwrite=False)
    assert run.screen_result.verdict.value == fixture["expected"]["verdict"]
    sources = fixture["expected"]["sources"]
    assert run.screen_result.sizing.as_is_value_source is not None
    assert run.screen_result.sizing.as_is_value_source.value == sources["as_is_value"]
    assert run.screen_result.components.court_records_source is not None
    assert run.screen_result.components.court_records_source.value == sources["court_records"]


def test_the_team_override_fixture_screens_go_and_says_where_its_numbers_came_from(
    capsys: pytest.CaptureFixture[str],
) -> None:
    team = FIXTURE_DIR / "go_team_overrides_tulsa.json"
    assert main(["run", str(team), "--underwrite"]) == 0
    out = capsys.readouterr().out
    assert "verdict: GO" in out
    assert "As-is / sale price from" in out and "team / team" in out
    assert "Court records from" in out
    # the underwrite request names no valuation, so the deal's own team numbers carry it
    assert "Estimated sale price" in out and "200,000.00" in out
    assert "TEAM_SOURCED_VALUES" in out


def test_a_team_entry_fixture_needs_a_request_not_an_inputs_block(tmp_path: Path) -> None:
    team = json.loads((FIXTURE_DIR / "go_team_overrides_tulsa.json").read_text(encoding="utf-8"))
    team["underwrite"] = {"inputs": {}}
    broken = tmp_path / "wrong_block.json"
    broken.write_text(json.dumps(team), encoding="utf-8")
    with pytest.raises(FixtureError, match="needs a 'request'"):
        run_fixture(broken, CONFIG, with_underwrite=True)


def test_a_file_with_neither_block_is_not_a_fixture(tmp_path: Path) -> None:
    neither = tmp_path / "neither.json"
    neither.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(FixtureError, match="no 'inputs' or 'team_entry' block"):
        run_fixture(neither, CONFIG, with_underwrite=False)
