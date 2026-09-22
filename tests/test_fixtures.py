"""Synthetic deals screen, and where they carry a block underwrite, as expected.  # SPEC §7, §8

Each fixture holds ``expected`` (verdict, flag codes with severities, components, sizing
numbers, substrings the reasons and the suggested reply must contain) beside either an
``inputs`` block handed straight to the engine or a ``team_entry`` block assembled the way
the API assembles a stored deal (``cli/fixtures.py``). Both go through the same loader the
CLI uses, so a fixture cannot pass here and behave differently under ``glenwood run``.
Fixtures that also carry an ``underwrite`` block run the underwrite too; at least one per
product does. Adding a fixture adds a test case.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cli.fixtures import run_fixture
from config.config import Config
from engine.version import ENGINE_VERSION
from schema.models import (
    CapStatus,
    LeverageMetric,
    Product,
    ScreenResult,
    TakeoutStatus,
    UnderwriteResult,
    ValueSource,
    Verdict,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))
CONFIG = Config.load()
D = Decimal


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


UNDERWRITE_FILES = [p for p in FIXTURE_FILES if "underwrite" in load(p)]


def cents(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"))


def rate(value: Decimal) -> Decimal:
    return value.quantize(D("0.000001"))


def run(path: Path) -> tuple[dict[str, Any], ScreenResult]:
    return load(path), run_fixture(path, CONFIG, with_underwrite=False).screen_result


def test_fixture_set_covers_every_product_and_verdict() -> None:
    assert len(FIXTURE_FILES) >= 6
    products = {run(p)[1].sizing.product for p in FIXTURE_FILES}
    verdicts = {load(p)["expected"]["verdict"] for p in FIXTURE_FILES}
    assert products == set(Product)
    assert verdicts == {v.value for v in Verdict}


def test_every_fixture_states_where_its_valuation_came_from() -> None:
    """A fixture that names sources is asserted on; one that does not stands in for a pull."""
    for path in FIXTURE_FILES:
        fixture, result = run(path)
        want = fixture["expected"].get("sources")
        sizing = result.sizing
        if want is None:
            for source in (sizing.as_is_value_source, sizing.arv_source):
                assert source in (None, ValueSource.ADAPTER), path.stem
            continue
        assert sizing.as_is_value_source == ValueSource(want["as_is_value"]), path.stem
        assert sizing.arv_source == ValueSource(want["arv"]), path.stem
        assert result.components.court_records_source == ValueSource(want["court_records"])


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_verdict_flags_and_reasons(path: Path) -> None:
    fixture, result = run(path)
    expected = fixture["expected"]
    assert fixture["name"] == path.stem

    assert result.verdict.value == expected["verdict"]
    got = Counter((f.code.value, f.severity.value) for f in result.flags)
    want = Counter((f["code"], f["severity"]) for f in expected["flags"])
    assert got == want, f"flags differ: got {got}, want {want}"
    assert len(result.reasons) == len(result.flags)
    for needle in expected["reasons_contain"]:
        assert any(needle in reason for reason in result.reasons), needle
    for needle in expected["reply_contains"]:
        assert needle in result.suggested_reply, needle


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_components_and_sizing(path: Path) -> None:
    fixture, result = run(path)
    expected = fixture["expected"]
    components, sizing = result.components, result.sizing

    assert components.credit_tranche.value == expected["credit_tranche"]
    assert components.experience_tier.value == expected["experience_tier"]
    assert (
        components.repeat_borrower_override_applied is expected["repeat_borrower_override_applied"]
    )
    assert sizing.all_pass is expected["all_pass"]
    assert sizing.commitment == Decimal(expected["commitment"])
    assert sizing.funded_at_close == Decimal(expected["funded_at_close"])
    if "split" in expected:
        assert sizing.split is not None
        assert sizing.split.purchase_portion == Decimal(expected["split"]["purchase_portion"])
        assert sizing.split.rehab_portion == Decimal(expected["split"]["rehab_portion"])
    else:
        assert sizing.split is None
    for name, status in expected["metrics"].items():
        assert sizing.metrics[LeverageMetric(name)].status is CapStatus(status), name


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_result_is_recorded_and_serializable(path: Path) -> None:
    _, result = run(path)
    assert result.engine_version == ENGINE_VERSION
    assert result.config_hash == CONFIG.config_hash
    for flag in result.flags:
        assert flag.message.strip(), flag.code
    again = ScreenResult.model_validate_json(result.model_dump_json())
    assert again == result
    for check in again.sizing.metrics.values():
        assert isinstance(check.cap, Decimal)
        assert check.actual is None or isinstance(check.actual, Decimal)


# --- underwrite blocks (SPEC §8) -----------------------------------------------------------------


def run_underwrite(path: Path) -> tuple[dict[str, Any], UnderwriteResult]:
    run = run_fixture(path, CONFIG, with_underwrite=True)
    assert run.underwrite_result is not None
    return load(path)["underwrite"]["expected"], run.underwrite_result


def test_underwrite_fixtures_cover_every_product() -> None:
    products = {run_underwrite(p)[1].sizing.product for p in UNDERWRITE_FILES}
    assert products == set(Product)


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_solve_and_grid(path: Path) -> None:
    expected, result = run_underwrite(path)
    assert result.rehab_months == expected["rehab_months"]
    assert cents(result.sizing.commitment) == D(expected["commitment"])
    assert rate(result.solved_rate) == D(expected["solved_rate"])
    assert rate(result.lender_yield_at_solve) == D(expected["lender_yield_at_solve"])
    assert rate(result.lender_yield_at_solve) == CONFIG.returns.target_irr
    lender = result.lender_at_solve
    assert cents(lender.interest) == D(expected["interest_at_solve"])
    assert cents(lender.fees.total) == D(expected["fees_at_solve"])
    assert cents(lender.avg_outstanding) == D(expected["avg_outstanding_at_term"])
    # independent check: at r*, interest + fees equal target x commitment x term / 12 to the cent
    target_income = CONFIG.returns.target_irr * result.sizing.commitment * result.term_months / 12
    assert cents(lender.interest + lender.fees.total) == cents(target_income)
    grid = result.grid_lender
    assert len(grid.rates) == expected["grid"]["columns"]
    assert len(grid.rows) == expected["grid"]["rows"]
    assert grid.solved_rate_inserted is expected["grid"]["solved_rate_inserted"]
    assert grid.months[0] == result.term_months
    assert grid.months[-1] == result.term_months + CONFIG.returns.month_window.after_term
    term_row = grid.rows[0]
    assert (
        sum(c.meets_target for c in term_row.cells)
        == (expected["grid"]["term_row_cells_meeting_target"])
    )
    assert [c.is_solved_rate for c in term_row.cells].count(True) == 1


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_borrower_exit_downside(path: Path) -> None:
    expected, result = run_underwrite(path)
    borrower, want = result.borrower_at_solve, expected["borrower"]
    assert cents(borrower.total_project_cost) == D(want["total_project_cost"])
    assert cents(borrower.holding_costs) == D(want["holding_costs"])
    assert cents(borrower.exit_net) == D(want["exit_net"])
    assert cents(borrower.profit) == D(want["profit"])
    assert cents(borrower.cash_in) == D(want["cash_in"])
    if want["cash_on_cash"] is None:
        assert borrower.cash_on_cash is None
    else:
        assert borrower.cash_on_cash is not None
        assert rate(borrower.cash_on_cash) == D(want["cash_on_cash"])

    exit_result, want = result.exit, expected["exit"]
    assert exit_result.type.value == want["type"]
    assert exit_result.exit_source.value == want["exit_source"]
    assert exit_result.status.value == want.get("status", "EVALUATED")
    assert cents(exit_result.ltv_takeout) == D(want["ltv_takeout"])
    assert cents(exit_result.payoff_due) == D(want["payoff_due"])
    if exit_result.status is TakeoutStatus.NOT_EVALUATED:
        # no rent, so nothing the rent feeds exists; refi_covers is unknown, not false
        assert want["refi_covers"] is None
        for name in ("noi_annual", "dscr_takeout", "max_takeout", "shortfall"):
            assert want[name] is None and getattr(exit_result, name) is None, name
        assert exit_result.dscr_at_payoff is None and exit_result.refi_covers is None
    else:
        assert cents(exit_result.noi_annual) == D(want["noi_annual"])
        assert cents(exit_result.dscr_takeout) == D(want["dscr_takeout"])
        assert cents(exit_result.max_takeout) == D(want["max_takeout"])
        assert exit_result.dscr_at_payoff is not None
        assert exit_result.dscr_at_payoff.quantize(D("0.001")) == D(want["dscr_at_payoff"])
        assert exit_result.refi_covers is want["refi_covers"]
        assert cents(exit_result.shortfall) == D(want["shortfall"])

    downside, want = result.downside, expected["downside"]
    assert cents(downside.recovery_basis) == D(want["recovery_basis"])
    assert cents(downside.liquidation) == D(want["liquidation"])
    assert downside.foreclosure_months == want["foreclosure_months"]
    assert cents(downside.recovery) == D(want["recovery"])
    assert cents(downside.exposure) == D(want["exposure"])
    assert rate(downside.cover) == D(want["cover"])
    assert downside.passed is want["passed"]


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_opex_sources(path: Path) -> None:
    """Where a fixture names the opex defaults, they come off the as-is value.  # SPEC §8.6"""
    expected, result = run_underwrite(path)
    want = expected.get("opex_defaults")
    if want is None:
        return
    exit_result = result.exit
    assert cents(exit_result.annual_taxes) == D(want["annual_taxes"])
    assert exit_result.annual_taxes_source.value == want["annual_taxes_source"]
    assert cents(exit_result.annual_insurance) == D(want["annual_insurance"])
    assert exit_result.annual_insurance_source.value == want["annual_insurance_source"]


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_flags_and_recording(path: Path) -> None:
    expected, result = run_underwrite(path)
    got = Counter((f.code.value, f.severity.value) for f in result.flags)
    want = Counter((f["code"], f["severity"]) for f in expected["flags"])
    assert got == want, f"flags differ: got {got}, want {want}"
    for flag in result.flags:
        assert flag.message.strip(), flag.code
    assert result.engine_version == ENGINE_VERSION
    assert result.config_hash == CONFIG.config_hash
    again = UnderwriteResult.model_validate_json(result.model_dump_json())
    assert again == result
