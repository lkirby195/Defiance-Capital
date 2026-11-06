"""The /defaults page: every key in config/glenwood.yaml, grouped, read-only.  # SPEC §10

The check that carries the weight reads the yaml itself - not ``Config`` - flattens it to
dotted keys and asserts each one is on the page as a ``data-key``. A key added to the yaml
and not placed in ``api/defaults_page.GROUPS`` either lands in Other (which fails here too)
or is missing (which fails the first test).
"""

from __future__ import annotations

import copy
import html
import re

from fastapi.testclient import TestClient

from api.defaults_page import (
    GROUPS,
    INPUT_COLUMNS,
    OTHER,
    caps_tables,
    display,
    flatten,
    group_of,
    groups,
    label_for,
    placeholder_line,
)
from config.config import DEFAULT_PATH, PLACEHOLDERS, Config, load_yaml
from engine.screen import tier_from_bucket
from schema.labels import INPUT_LABELS, TIER_BUCKETS, tier_label
from schema.models import ExperienceBucket, ExperienceTier
from tests.conftest import QueueClient, requires_db

CONFIG = Config.load()


def yaml_keys() -> set[str]:
    return set(flatten(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))))


@requires_db
def test_every_key_in_the_yaml_is_on_the_page(client: QueueClient) -> None:
    response = client.get("/defaults")
    assert response.status_code == 200
    shown = set(re.findall(r'data-key="([^"]+)"', response.text))
    missing = sorted(yaml_keys() - shown)
    assert not missing, f"keys in glenwood.yaml the page does not show: {missing}"
    assert not sorted(shown - yaml_keys())  # and nothing the yaml does not have


@requires_db
def test_the_page_shows_the_groups_in_order_and_some_values(client: QueueClient) -> None:
    body = client.get("/defaults").text
    headings = [heading for heading, _ in GROUPS]
    at = [body.index(f"<h2>{html.escape(heading)}</h2>") for heading in headings]
    assert at == sorted(at)
    assert f"<h2>{OTHER}</h2>" not in body
    assert 'data-key="interest.default_annual_rate">12.50%<' in body
    assert 'data-key="fees.closing_costs_default_usd">$1,000.00<' in body
    assert 'data-key="pricing.target_lender_irr">20.00%<' in body
    assert (
        'data-key="pricing.loan_amount_pct_steps">0.00%, -2.50%, -5.00%, -7.50%, -10.00%<' in body
    )
    assert 'data-key="pricing.loan_amount_rounding">$1,000.00<' in body
    assert 'data-key="pricing.borrower_rate_cushion">0.50%<' in body
    assert 'data-key="pricing.borrower_coc_target">20.00%<' in body
    assert 'data-key="pricing.borrower_rate_rounding">0.25%<' in body
    assert 'data-key="web_intake.thanks_token_hours">24<' in body
    assert 'data-key="sign_in.lockout_failures">10<' in body
    assert 'data-key="sign_in.lockout_minutes">15<' in body
    assert 'data-key="rental.target_dscr">1.20x<' in body
    assert 'data-key="rental.takeout_ltv">75.00%<' in body
    assert 'data-key="take_back.foreclosure_months.OK">6<' in body
    assert 'data-key="take_back.foreclosure_months.OTHER">8<' in body
    assert 'data-key="take_back.hold_months">12, 24, 36<' in body
    assert 'data-key="states.served">OK, CO<' in body
    cell = "leverage_caps.ACQUISITION_REHAB.T3.E2.ltv"
    assert f'data-key="{cell}" title="{cell}">75.00%<' in body
    assert 'data-key="flags.severities.OPEN_TAX_LIEN">Hard<' in body
    # v0.25: the column is "Variable", and no label carries a unit word
    assert "<th>Variable</th>" in body and "<th>Setting</th>" not in body
    for clutter in ("default pct", "Default annual rate", "Broker selling", " usd<"):
        assert clutter not in body, clutter
    # v0.27: no Key column; the key is the row's hover title, and still its value's data-key
    assert "<th>Key</th>" not in body
    assert "<code>fees.origination_default_pct</code>" not in body
    assert '<tr title="fees.origination_default_pct">' in body
    assert '<tr title="take_back.foreclosure_months.OTHER">' in body
    # the one input word this page qualifies (the deal page keeps "Holding Costs")
    assert "<td>Holding costs (of purchase + rehab)</td>" in body


@requires_db
def test_it_is_read_only_and_linked_from_the_top_bar(client: QueueClient) -> None:
    body = client.get("/defaults").text
    assert '<form method="post" action="/defaults"' not in body
    assert '<a class="btn" href="/defaults">Defaults</a>' in client.get("/queue").text
    assert client.post("/defaults").status_code == 405


@requires_db
def test_a_stranger_is_sent_to_sign_in(anon_client: TestClient) -> None:
    response = anon_client.get("/defaults", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_every_yaml_key_has_a_group() -> None:
    # the placeholders list is not a value: the page counts it under the title (v0.27)
    stray = sorted(key for key in yaml_keys() - {PLACEHOLDERS} if group_of(key) == OTHER)
    assert not stray, f"place these in api/defaults_page.GROUPS: {stray}"
    assert all(group.heading != OTHER for group in groups(CONFIG))


def test_the_groups_the_brief_names() -> None:
    """v0.27: the groups, in order, and what each holds in the order the brief lists it."""
    assert [heading for heading, _ in GROUPS] == [
        "Loan Terms",
        "Exit & Sales",
        "Rental Takeout",
        "Take-Back",
        "Pricing & Quotes",
        "Sensitivity",
        "Credit & Experience",
        "Court & Lien Flags",
        "Leverage Caps",
        "System",
    ]
    held = {group.heading: [leaf.key for leaf in group.leaves] for group in groups(CONFIG)}
    assert held["Loan Terms"] == [
        "fees.origination_default_pct",
        "fees.contingency_default_pct",
        "fees.closing_costs_default_usd",
        "fees.holding_costs_default_pct_of_cost",
        "interest.default_annual_rate",
        "states.served",
    ]
    assert held["Exit & Sales"] == ["fees.broker_selling_pct", "draws.listing_months"]
    assert held["Leverage Caps"] == ["screen.tolerance_band"]  # then the caps tables
    assert held["System"] == [
        "web_intake.submissions_per_hour_per_ip",
        "web_intake.thanks_token_hours",
        "rentcast.cache_days",
        "rentcast.monthly_request_budget",
        "sign_in.lockout_failures",
        "sign_in.lockout_minutes",
        "interest.day_count_basis",
        "closing.default_lead_days",
    ]
    assert held["Court & Lien Flags"][-2:] == ["states.court_adapter.OK", "states.court_adapter.CO"]
    assert all(key.startswith("flags.") for key in held["Court & Lien Flags"][:-2])
    assert all(key.startswith(("credit.", "experience.")) for key in held["Credit & Experience"])
    for heading, prefix in (
        ("Rental Takeout", "rental."),
        ("Take-Back", "take_back."),
        ("Pricing & Quotes", "pricing."),
        ("Sensitivity", "sensitivity."),
    ):
        assert held[heading] and all(key.startswith(prefix) for key in held[heading]), heading
    assert group_of("leverage_caps.ACQUISITION.T1.E0.ltc") == "Leverage Caps"
    # System alone is collapsed, and the caps tables hang off Leverage Caps alone
    assert [group.heading for group in groups(CONFIG) if group.collapsed] == ["System"]
    assert [group.heading for group in groups(CONFIG) if group.caps] == ["Leverage Caps"]


def test_display() -> None:
    from decimal import Decimal as D

    assert display("fees.origination_default_pct", D("0.02")) == "2.00%"
    assert display("sensitivity.rate_min", D("0.125")) == "12.50%"
    assert display("pricing.borrower_rate_rounding", D("0.0025")) == "0.25%"
    assert display("take_back.legal_costs_usd", D("5000.00")) == "$5,000.00"
    assert display("credit.tranche_cutoffs.T1", 740) == "740"


def test_every_row_is_labelled_in_the_team_s_words() -> None:
    """v0.25: no "default pct" and no bare state code; the unit is the value's job."""
    assert label_for("fees.origination_default_pct") == "Origination"
    assert label_for("fees.broker_selling_pct") == "Sales cost"
    assert label_for("interest.default_annual_rate") == "Interest rate"
    # a default for a deal's own input reads as the deal page captions it (v0.27)...
    assert set(INPUT_COLUMNS.values()) == set(INPUT_LABELS)
    for key, column in INPUT_COLUMNS.items():
        assert label_for(key).startswith(INPUT_LABELS[column]), key
    assert label_for("fees.contingency_default_pct") == "Contingency"
    # ...and the holding costs say what they are a share of, on this page alone
    assert label_for("fees.holding_costs_default_pct_of_cost") == (
        "Holding costs (of purchase + rehab)"
    )
    assert INPUT_LABELS["holding_costs_pct_of_cost"] == "Holding costs"
    assert [key for key in INPUT_COLUMNS if label_for(key) != INPUT_LABELS[INPUT_COLUMNS[key]]] == [
        "fees.holding_costs_default_pct_of_cost"
    ]
    assert label_for("take_back.foreclosure_months.OK") == "OK foreclosure months"
    assert label_for("take_back.foreclosure_months.CO") == "CO foreclosure months"
    assert label_for("take_back.foreclosure_months.OTHER") == "Other states foreclosure months"
    assert label_for("states.court_adapter.OK") == "OK court records source"
    assert label_for("credit.tranche_cutoffs.T1") == "T1 minimum FICO"
    assert label_for("flags.severities.OPEN_TAX_LIEN") == "Open tax lien"
    # a key nobody named still loses its unit words
    assert label_for("fees.some_new_default_usd") == "Some new"
    leaves = [leaf for group in groups(CONFIG) for leaf in group.leaves]
    for leaf in leaves:
        for word in ("pct", "usd", "default"):
            assert word not in leaf.label.lower().split(), (leaf.key, leaf.label)
    assert len({leaf.label for leaf in leaves}) == len(leaves), "two rows read the same"


# --- v0.27: placeholders, the caps tables, the columns -------------------------------------------

TAG = '<span class="dflt">placeholder</span>'


def _tagged_rows(body: str) -> set[str]:
    return set(re.findall(rf'data-key="([^"]+)">[^<]*</span> {re.escape(TAG)}', body))


@requires_db
def test_each_placeholder_is_tagged_and_the_list_is_counted(client: QueueClient) -> None:
    body = client.get("/defaults").text
    assert '<p class="small" data-key="placeholders">13 values are placeholders until ' in body
    rows = [leaf.key for group in groups(CONFIG) for leaf in group.leaves]
    # every row the list names wears the tag, and no other row does
    assert _tagged_rows(body) == {key for key in rows if CONFIG.is_placeholder(key)}
    assert {
        "interest.default_annual_rate",
        "pricing.max_quoted_rate",
        "rental.takeout_ltv",
        "take_back.foreclosure_months.OK",  # a prefix entry tags every row under it
        "take_back.foreclosure_months.OTHER",
        "rentcast.monthly_request_budget",
    } <= _tagged_rows(body)
    assert "pricing.loan_amount_pct_steps" not in _tagged_rows(body)
    # each caps table as a whole, in its title bar, and then not cell by cell
    assert f"<h4>100% at close {TAG}</h4>" in body and f"<h4>Holdback {TAG}</h4>" in body
    caps = body[body.index("<h2>Leverage Caps</h2>") : body.index("<h2>System</h2>")]
    assert caps.count(TAG) == 2
    # the closed System card says it holds some, so the count above can be found
    assert f"<summary><h2>System</h2> {TAG}</summary>" in body


def test_the_count_line_reads_for_any_length() -> None:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    assert placeholder_line(CONFIG) == "13 values are placeholders until Defiance confirms them."
    data["placeholders"] = ["rental.takeout_ltv"]
    assert placeholder_line(Config.from_dict(data)) == (
        "1 value is a placeholder until Defiance confirms it."
    )
    data["placeholders"] = []
    assert placeholder_line(Config.from_dict(data)) == "No values are placeholders."


def test_an_entry_for_part_of_a_caps_table_tags_its_cells_not_the_table() -> None:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    data["placeholders"] = ["leverage_caps.ACQUISITION.T1"]
    at_close, holdback = caps_tables(Config.from_dict(data))
    assert not at_close.placeholder and not holdback.placeholder
    first, *rest = at_close.rows
    assert all(ltc.placeholder and ltv.placeholder for ltc, ltv in first[1])
    assert not any(ltc.placeholder or ltv.placeholder for _, cells in rest for ltc, ltv in cells)
    assert not any(ltc.placeholder for _, cells in holdback.rows for ltc, _ in cells)


def test_the_caps_read_as_fico_ranges_and_deal_counts() -> None:
    tables = caps_tables(CONFIG)
    assert [table.structure for table in tables] == ["100% at close", "Holdback"]
    for table in tables:
        assert [fico for fico, _ in table.rows] == [
            "740+",
            "700–739",
            "660–699",
            "620–659",
            "Under 620",
        ]
        assert table.tiers == ["0", "1–2", "3–5", "6+"]
        assert table.placeholder
    # a tier's words are the bucket the screen maps onto it, both ways round
    for bucket in ExperienceBucket:
        assert TIER_BUCKETS[tier_from_bucket(bucket)] is bucket
    assert tier_label(ExperienceTier.E2) == "3–5"


@requires_db
def test_the_caps_tables_print_no_codes(client: QueueClient) -> None:
    body = client.get("/defaults").text
    caps = body[body.index("<h2>Leverage Caps</h2>") : body.index("<h2>System</h2>")]
    text = re.sub(r"<[^>]+>", " ", caps)  # what a person reads, without the hover titles
    for code in ("T1", "T3", "T5", "E0", "E2", "ACQUISITION", "Tranche"):
        assert not re.search(rf"\b{code}\b", text), code
    assert "Credit Range" in text and "Deals in last 36 months" in text and "Under 620" in text
    # the keys are the cells' hover titles
    assert 'title="leverage_caps.ACQUISITION.T1.E0.ltc"' in caps


@requires_db
def test_every_table_is_on_the_same_two_columns(client: QueueClient) -> None:
    body = client.get("/defaults").text
    pairs = body.count('<table class="pairs">')
    assert pairs == len(GROUPS)  # one per card
    colgroup = '<colgroup><col class="variable"><col class="value"></colgroup>'
    assert body.count(colgroup) == pairs
    # the only other tables are the two caps tables, on fixed columns of their own
    assert body.count("<table") == pairs + 2 == pairs + body.count('<table class="caps">')
    assert ".defaults table.pairs col.variable { width: 45%; }" in body
    assert "<th>Variable</th><th>Value</th>" in body


@requires_db
def test_system_is_collapsed_at_the_bottom(client: QueueClient) -> None:
    body = client.get("/defaults").text
    # a closed <details>: no "open" on it
    assert re.search(r'<details class="panel chart">\s*<summary><h2>System</h2>', body)
    system = body.index("<summary><h2>System</h2>")
    assert body.index("<h2>Leverage Caps</h2>") < system
    assert "<section" not in body[system:]  # nothing after it
