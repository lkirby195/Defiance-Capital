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
from schema.dates import month_end
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
        # null: nobody searched (the public form asks for no court search, SPEC §4.2)
        court = None if want["court_records"] is None else ValueSource(want["court_records"])
        assert result.components.court_records_source == court


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
    assert result.term_months == expected["term_months"]
    assert result.term_stub_days == expected["term_stub_days"]
    assert rate(result.term_months_decimal) == D(expected["term_months_decimal"])
    assert result.rehab_months == expected["rehab_months"]
    assert cents(result.sizing.commitment) == D(expected["commitment"])
    assert cents(result.economics.funded_at_close) == D(expected["funded_at_close"])

    economics, want = result.economics, expected["economics"]
    assert cents(economics.rehab_adj) == D(want["rehab_adj"])
    assert cents(economics.contingency) == D(want["contingency"])
    assert cents(economics.closing_costs) == D(want["closing_costs"])
    assert rate(economics.holding_costs_pct_of_cost) == D(want["holding_costs_pct_of_cost"])
    assert cents(economics.holding_costs_basis) == D(want["holding_costs_basis"])
    assert cents(economics.holding_costs_total) == D(want["holding_costs_total"])
    assert cents(economics.holding_costs_monthly) == D(want["holding_costs_monthly"])
    # The three are one statement: the percentage of the basis, over the term (SPEC §8.1).
    assert economics.holding_costs_total == economics.holding_costs_basis * (
        economics.holding_costs_pct_of_cost
    )
    assert (
        economics.holding_costs_monthly * result.term_months_decimal
        == economics.holding_costs_total
    )
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
    rows = result.term_months + 1 + (1 if result.term_stub_days else 0)
    assert len(overview.entries) == want["rows"] == rows
    assert cents(overview.entries[0].net) == D(want["first_net"])
    assert cents(overview.entries[-1].net) == D(want["last_net"])
    for name in ("funding", "draws", "interest", "fees", "payoff"):
        assert cents(getattr(overview, f"total_{name}")) == D(want[f"total_{name}"]), name
    assert cents(overview.total_profit) == D(want["total_profit"])

    # the two identities the ledger rests on (SPEC §8.3)
    assert overview.total_profit == overview.total_interest + overview.total_fees
    assert -(overview.total_funding + overview.total_draws) == overview.total_payoff

    # A stub term ends on one short period, and only that row is short (SPEC §8.3).
    stubs = [entry for entry in overview.entries if entry.stub_days]
    assert [entry.stub_days for entry in stubs] == (
        [result.term_stub_days] if result.term_stub_days else []
    )
    if stubs:
        assert stubs[0] is overview.entries[-1]
        assert cents(stubs[0].interest) == D(want["stub_interest"])

    assert overview.irr is not None
    assert abs(overview.irr - D(want["irr"])) < D("1e-6")
    # ...and the rate really does zero the column it was solved on
    flows = [(entry.date, entry.net) for entry in overview.entries]
    assert abs(xnpv(overview.irr, flows)) < NPV_TOLERANCE


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_fixture_underwrite_dates_run_month_by_month(path: Path) -> None:
    _, result = run_underwrite(path)
    entries = result.return_overview.entries
    # month 0 is the end of the closing month, whatever day the deal closed on (SPEC §8.3)
    assert entries[0].date == month_end(result.closing_date)
    assert entries[0].date >= result.closing_date
    assert entries[-1].date == result.payoff_date
    assert all(entry.date == month_end(entry.date) for entry in entries if not entry.stub_days)
    assert [e.month for e in entries] == list(range(len(entries)))
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


# --- the borrower's own numbers, and a team correction on top of them (SPEC §4.2, §6.1) ----------

WEB_FILES = [p for p in FIXTURE_FILES if "web_entry" in load(p)]


def test_a_web_fixture_runs_through_the_public_form_s_own_parser() -> None:
    assert WEB_FILES, "no fixture exercises the WEB channel"
    for path in WEB_FILES:
        run = run_fixture(path, CONFIG, with_underwrite=False)
        assert run.deal is not None
        assert run.deal.channel.value == "WEB"
        assert run.deal.referral_note == load(path)["web_entry"]["referral_note"]


@pytest.mark.parametrize("path", UNDERWRITE_FILES, ids=[p.stem for p in UNDERWRITE_FILES])
def test_a_fixture_that_names_its_underwrite_sources_is_held_to_them(path: Path) -> None:
    run = run_fixture(path, CONFIG, with_underwrite=True)
    want = load(path)["underwrite"]["expected"].get("sources")
    if want is None:
        return
    assert run.underwrite_inputs is not None
    assert run.underwrite_inputs.deal.estimated_sale_price_source == ValueSource(
        want["estimated_sale_price"]
    )
    assert run.underwrite_inputs.monthly_rent_source == ValueSource(want["monthly_rent"])


def test_a_fixture_that_names_its_assumptions_is_held_to_them() -> None:
    """The five §8.4-§8.6 assumptions typed over on the panel reach the engine as the deal's
    own, and the ledger - which reads none of them - is the Tulsa ledger to the cent."""
    typed = FIXTURE_DIR / "go_team_assumptions_tulsa.json"
    plain = FIXTURE_DIR / "go_team_overrides_tulsa.json"
    want = load(typed)["underwrite"]["expected"]["assumptions"]
    run = run_fixture(typed, CONFIG, with_underwrite=True)
    assert run.underwrite_inputs is not None and run.underwrite_result is not None
    inputs, result = run.underwrite_inputs, run.underwrite_result
    assert rate(inputs.broker_selling_pct or D(0)) == D(want["broker_selling_pct"])
    assert rate(inputs.rental_expenses_pct_of_rent or D(0)) == D(
        want["rental_expenses_pct_of_rent"]
    )
    assert rate(inputs.rental_takeout_rate or D(0)) == D(want["rental_takeout_rate"])
    assert inputs.take_back_legal_costs_usd == D(want["take_back_legal_costs_usd"])
    assert inputs.take_back_lost_interest_months == want["take_back_lost_interest_months"]
    # ...and the result reports the numbers it ran on, not the config's
    assert rate(result.flip.broker_selling_pct) == D(want["broker_selling_pct"])
    assert rate(result.rental.expenses_pct) == D(want["rental_expenses_pct_of_rent"])
    assert rate(result.rental.takeout_rate) == D(want["rental_takeout_rate"])
    assert result.take_back.legal_costs == D(want["take_back_legal_costs_usd"])
    assert result.take_back.lost_interest_months == want["take_back_lost_interest_months"]
    # the ledger does not read any of the five
    untouched = run_fixture(plain, CONFIG, with_underwrite=True).underwrite_result
    assert untouched is not None
    assert result.return_overview == untouched.return_overview
    assert result.economics == untouched.economics
    # every other fixture types none of them over and runs on the config values - stored on
    # the deal as the defaults they are on an entry fixture, absent on an inputs fixture
    for path in UNDERWRITE_FILES:
        if path == typed:
            continue
        other = run_fixture(path, CONFIG, with_underwrite=True)
        assert other.underwrite_inputs is not None and other.underwrite_result is not None
        own = other.underwrite_inputs.broker_selling_pct
        assert own is None or own == CONFIG.fees.broker_selling_pct, path.stem
        assert other.underwrite_result.flip.broker_selling_pct == CONFIG.fees.broker_selling_pct
        assert other.underwrite_result.take_back.legal_costs == CONFIG.take_back.legal_costs_usd


def test_the_team_s_correction_replaces_the_borrower_s_number_and_keeps_it() -> None:
    """The override block is applied through the queue's own code; nothing is overwritten."""
    path = FIXTURE_DIR / "go_web_overridden_by_team_okc.json"
    run = run_fixture(path, CONFIG, with_underwrite=True)
    assert run.deal is not None and run.underwrite_inputs is not None
    retained = load(path)["expected"]["retained"]
    assert run.deal.estimated_sale_price_borrower == D(retained["estimated_sale_price_borrower"])
    assert run.deal.monthly_rent_borrower == D(retained["monthly_rent_borrower"])
    assert run.deal.estimated_sale_price_team == D("200000.00")
    # the engine ran on the team's price and the borrower's rent, and said so
    assert run.screen_inputs.deal.estimated_sale_price == D("200000.00")
    assert run.screen_inputs.deal.estimated_sale_price_source is ValueSource.TEAM
    assert run.underwrite_inputs.monthly_rent == D("1700.00")
    assert run.underwrite_inputs.monthly_rent_source is ValueSource.BORROWER
    codes = [flag.code.value for flag in run.underwrite_result.flags]  # type: ignore[union-attr]
    assert "TEAM_SOURCED_VALUES" in codes and "BORROWER_SOURCED_VALUES" in codes
