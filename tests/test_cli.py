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
    for section in ("LENDER", "GRID", "BORROWER", "EXIT", "DOWNSIDE", "FLAGS"):
        assert section in out, section
    expected = load(path)["underwrite"]["expected"]
    assert "Solved rate r*" in out
    assert "Cover" in out and "DSCR at payoff due" in out
    # the numbers on the page are the numbers the fixture pins
    plain = out.replace("$", "").replace(",", "")
    assert expected["borrower"]["profit"] in plain
    assert expected["exit"]["payoff_due"] in plain
    assert expected["downside"]["recovery"] in plain
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


def test_the_solved_rate_is_printed_at_full_precision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """r* is what the pricing hangs on, so it is not rounded to a whole basis point."""
    denver = FIXTURE_DIR / "go_split_draw_denver.json"
    assert main(["run", str(denver), "--underwrite"]) == 0
    assert "14.8333%" in capsys.readouterr().out


def test_a_below_grid_solved_rate_is_shown_in_place(capsys: pytest.CaptureFixture[str]) -> None:
    short = FIXTURE_DIR / "go_split_principal_no_rehab_period_okc.json"
    assert main(["run", str(short), "--underwrite"]) == 0
    out = capsys.readouterr().out
    assert "9.5000%" in out
    assert "SOLVED_RATE_BELOW_GRID" in out and "NO_REHAB_PERIOD" in out


def test_run_fixture_refuses_an_underwrite_it_was_not_given(tmp_path: Path) -> None:
    screen_only = next(p for p in FIXTURE_FILES if "underwrite" not in load(p))
    with pytest.raises(FixtureError):
        run_fixture(screen_only, CONFIG, with_underwrite=True)
    run = run_fixture(screen_only, CONFIG, with_underwrite=False)
    assert run.underwrite_result is None
