"""Config loader tests: the placeholder yaml loads, and bad yaml fails fast.  # SPEC §10"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml, normalize_metro
from schema.models import ExperienceTier, Product, State, Tranche


@pytest.fixture
def data() -> dict[str, Any]:
    return copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))


def test_placeholder_yaml_loads() -> None:
    cfg = Config.load()
    assert cfg.credit.floor_tranche is Tranche.T4
    assert cfg.fees.origination_pct == Decimal("0.02")
    assert cfg.returns.target_irr == Decimal("0.175")
    assert cfg.draws.s_curve_avg_utilization == Decimal("0.5")
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
        (("downside", "foreclosure_costs_usd"), Decimal("-5")),
        (("flags", "thresholds", "open_tax_liens_aggregate_usd"), Decimal("-1")),
        (("flags", "thresholds", "unsatisfied_judgments_aggregate_usd"), Decimal("-0.01")),
        (("returns", "rate_grid", "step"), Decimal("0")),
        (("draws", "default_rehab_months", "SPLIT_DRAW"), 61),
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


# --- cap rates: metro first, state default as fallback (SPEC §8.6, §10) ---------------------


def test_cap_rate_uses_metro_then_falls_back_to_state() -> None:
    cfg = Config.load()
    assert cfg.downside.cap_rate(State.OK, "Tulsa") == (Decimal("0.08"), "metro", "TULSA")
    assert cfg.downside.cap_rate(State.OK, "oklahoma city").level == "metro"
    assert cfg.downside.cap_rate(State.CO, "Boulder") == (Decimal("0.065"), "state", "CO")
    assert cfg.downside.cap_rate(State.CO, "") == (Decimal("0.065"), "state", "CO")
    assert cfg.downside.cap_rate(State.OTHER) == (Decimal("0.075"), "state", "OTHER")


def test_placeholder_metro_rates_equal_their_state_default() -> None:
    for tree in Config.load().downside.cap_rates.values():
        assert all(rate == tree.default for rate in tree.metros.values())


def test_normalize_metro() -> None:
    assert normalize_metro("  colorado-springs ") == "COLORADO_SPRINGS"
    assert normalize_metro("Oklahoma  City") == "OKLAHOMA_CITY"
    assert normalize_metro("TULSA") == "TULSA"


def test_metro_keys_must_be_upper_snake(data: dict[str, Any]) -> None:
    data["downside"]["cap_rates"]["OK"]["metros"]["Tulsa Metro"] = Decimal("0.08")
    with pytest.raises(ConfigError, match="UPPER_SNAKE"):
        Config.from_dict(data)


def test_state_cap_rate_default_is_required(data: dict[str, Any]) -> None:
    del data["downside"]["cap_rates"]["CO"]["default"]
    with pytest.raises(ConfigError, match="default"):
        Config.from_dict(data)


def test_every_state_needs_a_cap_rate_tree(data: dict[str, Any]) -> None:
    del data["downside"]["cap_rates"]["OTHER"]
    with pytest.raises(ConfigError, match="cap_rates is missing: OTHER"):
        Config.from_dict(data)


def test_metro_cap_rate_out_of_range_fails(data: dict[str, Any]) -> None:
    data["downside"]["cap_rates"]["CO"]["metros"]["DENVER"] = Decimal("1.5")
    with pytest.raises(ConfigError, match="DENVER"):
        Config.from_dict(data)


def test_flat_cap_rates_are_rejected(data: dict[str, Any]) -> None:
    data["downside"]["cap_rates"]["OK"] = Decimal("0.08")
    with pytest.raises(ConfigError, match="cap_rates"):
        Config.from_dict(data)


# --- court thresholds: aggregates for judgments and tax liens, per matter for litigation ------


def test_threshold_keys_are_the_aggregate_ones() -> None:
    thresholds = Config.load().flags.thresholds
    assert thresholds.unsatisfied_judgments_aggregate_usd == Decimal("10000")
    assert thresholds.open_tax_liens_aggregate_usd == Decimal("0")
    assert thresholds.active_civil_litigation_usd == Decimal("25000")


def test_old_per_matter_judgment_key_is_rejected(data: dict[str, Any]) -> None:
    thresholds = data["flags"]["thresholds"]
    thresholds["unsatisfied_judgment_usd"] = thresholds.pop("unsatisfied_judgments_aggregate_usd")
    with pytest.raises(ConfigError, match="unsatisfied_judgment"):
        Config.from_dict(data)


def test_tax_lien_aggregate_threshold_is_required(data: dict[str, Any]) -> None:
    del data["flags"]["thresholds"]["open_tax_liens_aggregate_usd"]
    with pytest.raises(ConfigError, match="open_tax_liens_aggregate_usd"):
        Config.from_dict(data)
