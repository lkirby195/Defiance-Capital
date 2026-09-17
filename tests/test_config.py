"""Config loader tests: the placeholder yaml loads, and bad yaml fails fast.  # SPEC §10"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from schema.models import ExperienceTier, Product, Severity, State, Tranche, UnderwriteFlag


@pytest.fixture
def data() -> dict[str, Any]:
    return copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))


def test_placeholder_yaml_loads() -> None:
    cfg = Config.load()
    assert cfg.credit.floor_tranche is Tranche.T4
    assert cfg.fees.origination_pct == Decimal("0.02")
    assert cfg.returns.target_irr == Decimal("0.175")
    assert cfg.draws.draw_avg_utilization == Decimal("0.50")
    assert cfg.draws.listing_months == 3
    assert cfg.fees.borrower_closing_pct_of_price == Decimal("0.03")
    assert cfg.downside.foreclosure_months == {State.OK: 8, State.CO: 4, State.OTHER: 8}
    assert cfg.downside.cover_floor == Decimal("1.0")
    assert cfg.returns.month_window.after_term == 6
    assert cfg.states.served == [State.OK, State.CO]
    assert cfg.leverage_caps[Product.SPLIT_PRINCIPAL][Tranche.T5][ExperienceTier.E3].ltarv <= 1


def test_yaml_numbers_are_exact_decimals() -> None:
    parsed = load_yaml("a: 0.1\nb: 0.02\nc: 3\n")
    assert parsed["a"] == Decimal("0.1") and str(parsed["a"]) == "0.1"
    assert parsed["b"] == Decimal("0.02")
    assert parsed["c"] == 3 and isinstance(parsed["c"], int)


def test_hash_is_stable_and_ignores_comments_and_order(tmp_path: Path) -> None:
    baseline = Config.load().config_hash
    assert len(baseline) == 64
    text = DEFAULT_PATH.read_text(encoding="utf-8")
    reordered = tmp_path / "g.yaml"
    reordered.write_text("# extra comment\n" + text + "\n# trailing\n", encoding="utf-8")
    assert Config.load(reordered).config_hash == baseline
    assert Config.load(reordered) == Config.load()


def test_hash_changes_when_a_value_changes(data: dict[str, Any]) -> None:
    data["fees"]["selling_cost_pct"] = Decimal("0.07")
    assert Config.from_dict(data).config_hash != Config.load().config_hash


def test_missing_key_fails(data: dict[str, Any]) -> None:
    del data["fees"]["selling_cost_pct"]
    with pytest.raises(ConfigError, match="selling_cost_pct"):
        Config.from_dict(data)


def test_missing_section_fails(data: dict[str, Any]) -> None:
    del data["downside"]
    with pytest.raises(ConfigError, match="downside"):
        Config.from_dict(data)


def test_unknown_key_fails(data: dict[str, Any]) -> None:
    data["fees"]["junk_fee_pct"] = Decimal("0.01")
    with pytest.raises(ConfigError, match="junk_fee_pct"):
        Config.from_dict(data)


@pytest.mark.parametrize(
    "path, value",
    [
        (("screen", "tolerance_band"), Decimal("1.5")),
        (("screen", "tolerance_band"), Decimal("-0.01")),
        (("credit", "tranche_cutoffs", "T1"), 900),
        (("takeout", "amortization_years"), 0),
        (("takeout", "dscr_floor"), Decimal("0")),
        (("downside", "foreclosure_cost_usd"), Decimal("-5")),
        (("downside", "foreclosure_months", "OK"), 61),
        (("downside", "cover_floor"), Decimal("0")),
        (("fees", "borrower_closing_pct_of_price"), Decimal("1.5")),
        (("flags", "thresholds", "unsatisfied_judgments_aggregate_usd"), Decimal("-0.01")),
        (("returns", "rate_grid", "step"), Decimal("0")),
        (("draws", "listing_months"), 61),
        (("draws", "draw_avg_utilization"), Decimal("1.01")),
    ],
)
def test_out_of_range_fails(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ConfigError, match=path[-1]):
        Config.from_dict(data)


def test_incomplete_caps_grid_fails(data: dict[str, Any]) -> None:
    del data["leverage_caps"]["WHOLETAIL"]["T3"]["E1"]
    with pytest.raises(ConfigError, match="WHOLETAIL.T3.E1"):
        Config.from_dict(data)


def test_missing_product_in_caps_grid_fails(data: dict[str, Any]) -> None:
    del data["leverage_caps"]["NO_DRAW"]
    with pytest.raises(ConfigError, match="20 cell"):
        Config.from_dict(data)


def test_origination_split_must_sum(data: dict[str, Any]) -> None:
    data["fees"]["origination_at_close_pct"] = Decimal("0.015")
    with pytest.raises(ConfigError, match="must equal origination_pct"):
        Config.from_dict(data)


def test_rate_grid_step_must_divide(data: dict[str, Any]) -> None:
    data["returns"]["rate_grid"]["step"] = Decimal("0.003")
    with pytest.raises(ConfigError, match="divide"):
        Config.from_dict(data)


def test_tranche_cutoffs_must_descend(data: dict[str, Any]) -> None:
    data["credit"]["tranche_cutoffs"]["T2"] = 750
    with pytest.raises(ConfigError, match="descending"):
        Config.from_dict(data)


def test_tranche_cutoffs_must_be_exactly_t1_to_t4(data: dict[str, Any]) -> None:
    data["credit"]["tranche_cutoffs"]["T5"] = 500
    with pytest.raises(ConfigError, match="exactly T1, T2, T3, T4"):
        Config.from_dict(data)


def test_every_flag_needs_a_severity(data: dict[str, Any]) -> None:
    del data["flags"]["severities"]["OPEN_TAX_LIEN"]
    with pytest.raises(ConfigError, match="OPEN_TAX_LIEN"):
        Config.from_dict(data)


def test_served_state_needs_court_adapter(data: dict[str, Any]) -> None:
    del data["states"]["court_adapter"]["CO"]
    with pytest.raises(ConfigError, match="court_adapter is missing: CO"):
        Config.from_dict(data)


def test_served_cannot_include_other(data: dict[str, Any]) -> None:
    data["states"]["served"].append("OTHER")
    with pytest.raises(ConfigError, match="OTHER"):
        Config.from_dict(data)


def test_unknown_court_adapter_fails(data: dict[str, Any]) -> None:
    data["states"]["court_adapter"]["OK"] = "zillow"
    with pytest.raises(ConfigError, match="court_adapter"):
        Config.from_dict(data)


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        Config.load(tmp_path / "nope.yaml")


def test_malformed_yaml_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("credit: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed"):
        Config.load(bad)


def test_config_is_immutable() -> None:
    cfg = Config.load()
    with pytest.raises(Exception, match="frozen"):
        cfg.fees.origination_pct = Decimal("0.03")  # type: ignore[misc]


# --- mechanics decisions (2026-09-10): keys added, renamed, and removed -----------------------


def test_every_state_needs_foreclosure_months(data: dict[str, Any]) -> None:
    del data["downside"]["foreclosure_months"]["OTHER"]
    with pytest.raises(ConfigError, match="foreclosure_months is missing: OTHER"):
        Config.from_dict(data)


def test_underwrite_severities_required_and_complete(data: dict[str, Any]) -> None:
    severities = Config.load().flags.underwrite_severities
    assert severities[UnderwriteFlag.REFI_SHORTFALL] is Severity.SOFT
    assert severities[UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR] is Severity.HARD
    del data["flags"]["underwrite_severities"]["REFI_SHORTFALL"]
    with pytest.raises(ConfigError, match="underwrite_severities is missing: REFI_SHORTFALL"):
        Config.from_dict(data)


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("returns", "coc_floor", Decimal("0.20")),
        ("draws", "s_curve_avg_utilization", Decimal("0.50")),
        ("draws", "default_rehab_months", {"NO_DRAW": 0}),
        ("downside", "cap_rates", {"OK": {"default": Decimal("0.08"), "metros": {}}}),
        ("downside", "foreclosure_costs_usd", Decimal("10000")),
        ("flags", "underwrite_flag_floor", Decimal("1")),
    ],
)
def test_removed_keys_are_rejected(
    data: dict[str, Any], section: str, key: str, value: Any
) -> None:
    data[section][key] = value
    with pytest.raises(ConfigError, match=key):
        Config.from_dict(data)


def test_month_window_before_term_is_rejected(data: dict[str, Any]) -> None:
    data["returns"]["month_window"]["before_term"] = 1
    with pytest.raises(ConfigError, match="before_term"):
        Config.from_dict(data)


def test_open_tax_lien_threshold_is_rejected(data: dict[str, Any]) -> None:
    data["flags"]["thresholds"]["open_tax_liens_aggregate_usd"] = Decimal("0")
    with pytest.raises(ConfigError, match="open_tax_liens_aggregate_usd"):
        Config.from_dict(data)


# --- court thresholds: aggregates for judgments and tax liens, per matter for litigation ------


def test_threshold_keys_are_the_aggregate_ones() -> None:
    thresholds = Config.load().flags.thresholds
    assert thresholds.unsatisfied_judgments_aggregate_usd == Decimal("10000")
    assert thresholds.active_civil_litigation_usd == Decimal("25000")


def test_old_per_matter_judgment_key_is_rejected(data: dict[str, Any]) -> None:
    thresholds = data["flags"]["thresholds"]
    thresholds["unsatisfied_judgment_usd"] = thresholds.pop("unsatisfied_judgments_aggregate_usd")
    with pytest.raises(ConfigError, match="unsatisfied_judgment"):
        Config.from_dict(data)


# --- Phase 2b review decisions (SPEC §7.4, §8.6, §8.7) ------------------------------------------


def test_retired_screen_closing_key_is_rejected(data: dict[str, Any]) -> None:
    """One buy-side closing number now does both jobs; the 2% screen estimate is gone."""
    assert "est_closing_pct_of_price" not in data["fees"]
    data["fees"]["est_closing_pct_of_price"] = Decimal("0.02")
    with pytest.raises(ConfigError, match="est_closing_pct_of_price"):
        Config.from_dict(data)


def test_opex_defaults_are_named_for_the_as_is_value(data: dict[str, Any]) -> None:
    """Renamed with the basis change so a stale yaml fails loudly rather than silently."""
    cfg = Config.from_dict(data)
    assert cfg.takeout.opex_defaults.taxes_pct_of_as_is_value == Decimal("0.012")
    assert cfg.takeout.opex_defaults.insurance_pct_of_as_is_value == Decimal("0.005")
    stale = copy.deepcopy(data)
    stale["takeout"]["opex_defaults"]["taxes_pct_of_value"] = stale["takeout"]["opex_defaults"].pop(
        "taxes_pct_of_as_is_value"
    )
    with pytest.raises(ConfigError, match="taxes_pct_of_value"):
        Config.from_dict(stale)


def test_underwrite_severities_cover_the_graded_codes_only(data: dict[str, Any]) -> None:
    graded = {UnderwriteFlag.REFI_SHORTFALL, UnderwriteFlag.DOWNSIDE_COVER_BELOW_FLOOR}
    assert set(Config.from_dict(data).flags.underwrite_severities) == graded


def test_config_cannot_grade_an_informational_underwrite_flag(data: dict[str, Any]) -> None:
    """NO_REHAB_PERIOD and SOLVED_RATE_BELOW_GRID are fixed Info in code (SPEC §8.7)."""
    data["flags"]["underwrite_severities"]["NO_REHAB_PERIOD"] = "HARD"
    with pytest.raises(ConfigError, match="NO_REHAB_PERIOD"):
        Config.from_dict(data)


def test_missing_graded_severity_still_fails(data: dict[str, Any]) -> None:
    del data["flags"]["underwrite_severities"]["REFI_SHORTFALL"]
    with pytest.raises(ConfigError, match="REFI_SHORTFALL"):
        Config.from_dict(data)


def test_exit_boundaries_load_and_must_not_overlap(data: dict[str, Any]) -> None:
    cfg = Config.from_dict(data)
    assert cfg.exit.resale_max_term_months == 9
    assert cfg.exit.hold_min_term_months == 12
    data["exit"]["hold_min_term_months"] = 9
    with pytest.raises(ConfigError, match="below exit.hold_min_term_months"):
        Config.from_dict(data)


def test_exit_section_is_required(data: dict[str, Any]) -> None:
    del data["exit"]
    with pytest.raises(ConfigError, match="exit"):
        Config.from_dict(data)
