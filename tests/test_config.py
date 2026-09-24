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
    assert cfg.fees.origination_default_pct == Decimal("0.02")
    assert cfg.fees.contingency_default_pct == Decimal("0.00")
    assert cfg.fees.closing_costs_default_usd == Decimal("1000.00")
    assert cfg.fees.holding_costs_default_pct_of_cost == Decimal("0.02")
    assert cfg.fees.broker_selling_pct == Decimal("0.04")
    assert cfg.draws.listing_months == 3
    assert cfg.rental.expenses_pct_of_rent == Decimal("0.35")
    assert cfg.rental.takeout_rate == Decimal("0.065")
    assert cfg.rental.amortization_years == 30
    assert cfg.rental.dscr_floor == Decimal("1.20")
    assert cfg.take_back.lost_interest_months == 3
    assert cfg.take_back.legal_costs_usd == Decimal("5000.00")
    assert cfg.take_back.dscr_floor == Decimal("1.00")
    assert cfg.states.served == [State.OK, State.CO]
    cell = cfg.leverage_caps[Product.SPLIT_PRINCIPAL][Tranche.T5][ExperienceTier.E3]
    # SPEC §7.4: two caps and no more, and the LTC placeholder is no cap at all yet.
    assert cell.ltc == Decimal("1.00") and cell.ltv <= 1
    assert not hasattr(cell, "ltarv") and not hasattr(cell, "ltv_as_is")


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
    data["fees"]["broker_selling_pct"] = Decimal("0.07")
    assert Config.from_dict(data).config_hash != Config.load().config_hash


def test_missing_key_fails(data: dict[str, Any]) -> None:
    del data["fees"]["broker_selling_pct"]
    with pytest.raises(ConfigError, match="broker_selling_pct"):
        Config.from_dict(data)


def test_missing_section_fails(data: dict[str, Any]) -> None:
    del data["take_back"]
    with pytest.raises(ConfigError, match="take_back"):
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
        (("rental", "amortization_years"), 0),
        (("rental", "dscr_floor"), Decimal("0")),
        (("rental", "expenses_pct_of_rent"), Decimal("1.01")),
        (("take_back", "legal_costs_usd"), Decimal("-5")),
        (("take_back", "lost_interest_months"), 61),
        (("take_back", "dscr_floor"), Decimal("0")),
        (("fees", "broker_selling_pct"), Decimal("1.5")),
        (("fees", "closing_costs_default_usd"), Decimal("-1")),
        (("flags", "thresholds", "unsatisfied_judgments_aggregate_usd"), Decimal("-0.01")),
        (("draws", "listing_months"), 61),
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


def test_the_origination_split_is_no_longer_a_tunable(data: dict[str, Any]) -> None:
    """Half at close and half at payoff is fixed in code (SPEC §3), so the two halves that
    used to be config are rejected rather than quietly ignored."""
    for key in ("origination_at_close_pct", "origination_at_payoff_pct"):
        stale = copy.deepcopy(data)
        stale["fees"][key] = Decimal("0.01")
        with pytest.raises(ConfigError, match=key):
            Config.from_dict(stale)


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


def test_the_take_back_replaced_the_reo_downside_entirely(data: dict[str, Any]) -> None:
    """No haircut, no foreclosure cost, no foreclosure months by state, no cover floor."""
    assert "downside" not in data
    for key, value in (
        ("reo_haircut", Decimal("0.15")),
        ("foreclosure_cost_usd", Decimal("10000")),
        ("cover_floor", Decimal("1.0")),
    ):
        stale = copy.deepcopy(data)
        stale["take_back"][key] = value
        with pytest.raises(ConfigError, match=key):
            Config.from_dict(stale)


def test_underwrite_severities_required_and_complete(data: dict[str, Any]) -> None:
    severities = Config.load().flags.underwrite_severities
    assert severities[UnderwriteFlag.DSCR_BELOW_FLOOR] is Severity.SOFT
    assert severities[UnderwriteFlag.TAKE_BACK_DSCR_BELOW_FLOOR] is Severity.HARD
    del data["flags"]["underwrite_severities"]["DSCR_BELOW_FLOOR"]
    with pytest.raises(ConfigError, match="underwrite_severities is missing: DSCR_BELOW_FLOOR"):
        Config.from_dict(data)


@pytest.mark.parametrize(
    "section, key, value",
    [
        # gone with the v0.2 §8: the rate solve, the grid, the draw-curve constant, the
        # extension fee, the DSCR takeout and the borrower's buy-side closing percentage
        ("fees", "extension_default_pct", Decimal("0.0")),
        ("fees", "selling_cost_pct", Decimal("0.06")),
        ("fees", "contingency_pct", Decimal("0.10")),
        ("fees", "borrower_closing_pct_of_price", Decimal("0.03")),
        ("draws", "draw_avg_utilization", Decimal("0.50")),
        ("draws", "s_curve_avg_utilization", Decimal("0.50")),
        ("rental", "ltv", Decimal("0.75")),
        ("flags", "underwrite_flag_floor", Decimal("1")),
    ],
)
def test_removed_keys_are_rejected(
    data: dict[str, Any], section: str, key: str, value: Any
) -> None:
    data[section][key] = value
    with pytest.raises(ConfigError, match=key):
        Config.from_dict(data)


@pytest.mark.parametrize("section", ["returns", "takeout", "downside"])
def test_a_whole_retired_section_is_rejected(data: dict[str, Any], section: str) -> None:
    """A stale yaml still carrying the rate grid, the DSCR takeout or the REO downside does
    not start the service (SPEC §10)."""
    assert section not in data
    data[section] = {"anything": 1}
    with pytest.raises(ConfigError, match=section):
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


def test_the_caps_grid_carries_two_caps_and_refuses_the_retired_pair(
    data: dict[str, Any],
) -> None:
    """LTARV is gone and the as-is LTV went with it (SPEC §7.4); a stale yaml is refused."""
    cell = data["leverage_caps"]["NO_DRAW"]["T1"]["E0"]
    assert set(cell) == {"ltc", "ltv"}
    for key in ("ltarv", "ltv_as_is"):
        stale = copy.deepcopy(data)
        stale["leverage_caps"]["NO_DRAW"]["T1"]["E0"][key] = Decimal("0.70")
        with pytest.raises(ConfigError, match=key):
            Config.from_dict(stale)


def test_one_holding_cost_replaced_the_three_itemized_opex_lines(data: dict[str, Any]) -> None:
    """Taxes, insurance and utilities are one ``holding_costs_total_usd`` input now, so the
    percentages that stood in for them are gone from config entirely (SPEC §8.1)."""
    cfg = Config.from_dict(data)
    assert cfg.fees.holding_costs_default_pct_of_cost == Decimal("0.02")
    for key in (
        "taxes_pct_of_as_is_value",
        "insurance_pct_of_as_is_value",
        "utilities_pct_of_as_is_value",
        "vacancy_pct_of_rent",
    ):
        stale = copy.deepcopy(data)
        stale["fees"][key] = Decimal("0.01")
        with pytest.raises(ConfigError, match=key):
            Config.from_dict(stale)


def test_underwrite_severities_cover_the_graded_codes_only(data: dict[str, Any]) -> None:
    graded = {UnderwriteFlag.DSCR_BELOW_FLOOR, UnderwriteFlag.TAKE_BACK_DSCR_BELOW_FLOOR}
    assert set(Config.from_dict(data).flags.underwrite_severities) == graded


def test_config_cannot_grade_an_informational_underwrite_flag(data: dict[str, Any]) -> None:
    """NO_REHAB_PERIOD and MONTHLY_RENT_MISSING are fixed Info in code (SPEC §8.8)."""
    for code in ("NO_REHAB_PERIOD", "MONTHLY_RENT_MISSING", "SALE_PRICE_MISSING"):
        stale = copy.deepcopy(data)
        stale["flags"]["underwrite_severities"][code] = "HARD"
        with pytest.raises(ConfigError, match=code):
            Config.from_dict(stale)


def test_missing_graded_severity_still_fails(data: dict[str, Any]) -> None:
    del data["flags"]["underwrite_severities"]["TAKE_BACK_DSCR_BELOW_FLOOR"]
    with pytest.raises(ConfigError, match="TAKE_BACK_DSCR_BELOW_FLOOR"):
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
