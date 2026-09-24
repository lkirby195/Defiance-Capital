"""Synthetic deals screen, and where they carry a block underwrite, as expected.  # SPEC §7, §8

Each fixture holds ``expected`` (verdict, flag codes with severities, components, sizing
numbers, substrings the reasons and the suggested reply must contain) beside either an
``inputs`` block handed straight to the engine or a ``team_entry`` block assembled the way
the API assembles a stored deal (``cli/fixtures.py``). Both go through the same loader the
CLI uses, so a fixture cannot pass here and behave differently under ``glenwood run``.
Fixtures that also carry an ``underwrite`` block run the underwrite too; at least one per
product does. Adding a fixture adds a test case.

Every number in an ``expected`` block was computed from the SPEC §8 formulas independently of
the engine before it was written down, so these are not recordings of what the engine said -
a change in the engine that moves one of them is a change somebody has to defend.
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
from engine.calc.irr import NPV_TOLERANCE, xnpv
from engine.version import ENGINE_VERSION
from schema.models import (
    AnalysisStatus,
    CapStatus,
    LeverageMetric,
    Product,
    ScreenResult,
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


def maybe_cents(value: Decimal | None) -> Decimal | None:
    return None if value is None else cents(value)


def maybe_rate(value: Decimal | None) -> Decimal | None:
    return None if value is None else rate(value)


def want_money(value: str | None) -> Decimal | None:
    return None if value is None else D(value)


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
            assert sizing.estimated_sale_price_source in (None, ValueSource.ADAPTER), path.stem
            continue
        assert sizing.estimated_sale_price_source == ValueSource(want["estimated_sale_price"])
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
    assert cents(sizing.commitment) == D(expected["commitment"])
    assert cents(sizing.funded_at_close) == D(expected["funded_at_close"])
    assert cents(sizing.total_cost) == D(expected["total_cost"])
    assert cents(sizing.rehab_adj) == D(expected["rehab_adj"])
    if "split" in expected:
        assert sizing.split is not None
        assert cents(sizing.split.purchase_portion) == D(expected["split"]["purchase_portion"])
        assert cents(sizing.split.rehab_portion) == D(expected["split"]["rehab_portion"])
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


def test_the_fixture_set_covers_every_analysis_status() -> None:
    """One with no rent, one with the flip off, one with no sale price, and the ordinary case."""
    statuses = {
        (result.flip.status, result.rental.status, result.take_back.status)
        for _, result in (run_underwrite(p) for p in UNDERWRITE_FILES)
    }
    flip = {status[0] for status in statuses}
    rental = {status[1] for status in statuses}
    take_back = {status[2] for status in statuses}
    assert flip == {
        AnalysisStatus.EVALUATED,
        AnalysisStatus.OFF,
        AnalysisStatus.NOT_EVALUATED,
    }
    assert AnalysisStatus.EVALUATED in rental and AnalysisStatus.OFF in rental
    assert take_back == {AnalysisStatus.EVALUATED, AnalysisStatus.NOT_EVALUATED}


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_term_and_economics(path: Path) -> None:
    expected, result = run_underwrite(path)
    assert result.payoff_date.isoformat() == expected["payoff_date"]
    assert result.rehab_months == expected["rehab_months"]
    assert cents(result.sizing.commitment) == D(expected["commitment"])
    assert cents(result.economics.funded_at_close) == D(expected["funded_at_close"])

    economics, want = result.economics, expected["economics"]
    assert cents(economics.rehab_adj) == D(want["rehab_adj"])
    assert cents(economics.contingency) == D(want["contingency"])
    assert cents(economics.closing_costs) == D(want["closing_costs"])
    assert cents(economics.holding_costs_total) == D(want["holding_costs_total"])
    assert cents(economics.holding_costs_monthly) == D(want["holding_costs_monthly"])
    assert cents(economics.origination_at_close) == D(want["origination_at_close"])
    assert cents(economics.origination_at_payoff) == D(want["origination_at_payoff"])
    # the two halves are exactly that, whatever the fee is
    assert economics.origination_at_close == economics.origination_at_payoff
    assert (
        economics.origination_at_close + economics.origination_at_payoff
        == economics.commitment * economics.origination_fee_pct
    )


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_ledger_and_irr(path: Path) -> None:
    expected, result = run_underwrite(path)
    overview, want = result.return_overview, expected["ledger"]
    assert len(overview.entries) == want["rows"] == result.term_months + 1
    assert cents(overview.entries[0].net) == D(want["first_net"])
    assert cents(overview.entries[-1].net) == D(want["last_net"])
    for name in ("funding", "draws", "interest", "fees", "payoff"):
        assert cents(getattr(overview, f"total_{name}")) == D(want[f"total_{name}"]), name
    assert cents(overview.total_profit) == D(want["total_profit"])

    # the two identities the ledger rests on (SPEC §8.3)
    assert overview.total_profit == overview.total_interest + overview.total_fees
    assert -(overview.total_funding + overview.total_draws) == overview.total_payoff

    assert overview.irr is not None
    assert abs(overview.irr - D(want["irr"])) < D("1e-6")
    # ...and the rate really does zero the column it was solved on
    flows = [(entry.date, entry.net) for entry in overview.entries]
    assert abs(xnpv(overview.irr, flows)) < NPV_TOLERANCE


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_dates_run_month_by_month(path: Path) -> None:
    _, result = run_underwrite(path)
    entries = result.return_overview.entries
    assert entries[0].date == result.closing_date
    assert entries[-1].date == result.payoff_date
    assert [e.month for e in entries] == list(range(result.term_months + 1))
    assert all(a.date < b.date for a, b in zip(entries, entries[1:], strict=False))


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_flip_rental_and_take_back(path: Path) -> None:
    expected, result = run_underwrite(path)

    flip, want = result.flip, expected["flip"]
    assert flip.status.value == want["status"]
    assert cents(flip.total_costs) == D(want["total_costs"])
    assert maybe_cents(flip.broker_costs) == want_money(want["broker_costs"])
    assert maybe_cents(flip.net_profit) == want_money(want["net_profit"])
    assert maybe_rate(flip.profit_yield) == want_money(want["profit_yield"])

    rental, want = result.rental, expected["rental"]
    assert rental.status.value == want["status"]
    assert cents(rental.debt_service_monthly) == D(want["debt_service_monthly"])
    assert maybe_cents(rental.expenses) == want_money(want["expenses"])
    assert maybe_cents(rental.net_monthly_income) == want_money(want["net_monthly_income"])
    assert maybe_rate(rental.dscr) == want_money(want["dscr"])
    assert rental.passed is want["passed"]

    take_back, want = result.take_back, expected["take_back"]
    assert take_back.status.value == want["status"]
    assert cents(take_back.lost_interest) == D(want["lost_interest"])
    assert cents(take_back.total_cost) == D(want["total_cost"])
    assert cents(take_back.debt_service_monthly) == D(want["debt_service_monthly"])
    assert maybe_cents(take_back.net_monthly_income) == want_money(want["net_monthly_income"])
    assert maybe_rate(take_back.dscr) == want_money(want["dscr"])
    assert take_back.passed is want["passed"]


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
