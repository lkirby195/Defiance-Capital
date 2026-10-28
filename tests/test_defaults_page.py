"""The /defaults page: every key in config/glenwood.yaml, grouped, read-only.  # SPEC §10

The check that carries the weight reads the yaml itself - not ``Config`` - flattens it to
dotted keys and asserts each one is on the page as a ``data-key``. A key added to the yaml
and not placed in ``api/defaults_page.GROUPS`` either lands in Other (which fails here too)
or is missing (which fails the first test).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from api.defaults_page import GROUPS, OTHER, display, flatten, group_of, groups
from config.config import DEFAULT_PATH, Config, load_yaml
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
    at = [body.index(f"<h2>{heading}</h2>") for heading in headings]
    assert at == sorted(at)
    assert f"<h2>{OTHER}</h2>" not in body
    assert 'data-key="interest.default_annual_rate">12.5%<' in body
    assert 'data-key="fees.closing_costs_default_usd">$1,000.00<' in body
    assert 'data-key="pricing.target_lender_irr">20%<' in body
    assert 'data-key="pricing.loan_amount_pct_steps">0%, -5%, -10%<' in body
    assert 'data-key="pricing.loan_amount_rounding">$1,000.00<' in body
    assert 'data-key="web_intake.thanks_token_hours">24<' in body
    assert 'data-key="rental.target_dscr">1.20x<' in body
    assert 'data-key="rental.takeout_ltv">75%<' in body
    assert 'data-key="take_back.foreclosure_months.OK">6<' in body
    assert 'data-key="take_back.hold_months">12, 24, 36<' in body
    assert 'data-key="states.served">OK, CO<' in body
    assert 'data-key="leverage_caps.ACQUISITION_REHAB.T3.E2.ltv">75%<' in body


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
    stray = sorted(key for key in yaml_keys() if group_of(key) == OTHER)
    assert not stray, f"place these in api/defaults_page.GROUPS: {stray}"
    assert all(group.heading != OTHER for group in groups(CONFIG))


def test_the_groups_the_brief_names() -> None:
    assert [heading for heading, _ in GROUPS] == [
        "Economics",
        "Flip",
        "Rental",
        "Take-Back",
        "Pricing",
        "Sensitivity",
        "Screen caps and thresholds",
        "Dates",
    ]
    assert group_of("fees.origination_default_pct") == "Economics"
    assert group_of("fees.broker_selling_pct") == "Flip"
    assert group_of("interest.day_count_basis") == "Dates"
    assert group_of("closing.default_lead_days") == "Dates"
    assert group_of("leverage_caps.ACQUISITION.T1.E0.ltc") == "Screen caps and thresholds"


def test_display() -> None:
    from decimal import Decimal as D

    assert display("fees.origination_default_pct", D("0.02")) == "2%"
    assert display("sensitivity.rate_min", D("0.125")) == "12.5%"
    assert display("take_back.legal_costs_usd", D("5000.00")) == "$5,000.00"
    assert display("credit.tranche_cutoffs.T1", 740) == "740"
