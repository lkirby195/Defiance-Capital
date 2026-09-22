"""The team-entry form: what it shows a person, and what it refuses.  # SPEC §4.1, §4.2

Three things are under test and they are deliberately separate. The *labels* are what the
page says where the model stores a code - a FICO range rather than ``T3``, a count of deals
rather than ``3_5`` - and those are asserted on the rendered HTML, because the whole point is
what a reader sees. The *markers* say Required or Optional on every box, and the browser's
own ``required`` attribute has to agree with them or the page is lying. The *refusal* is the
server saying the same thing again, because the attribute is a courtesy and not a control:
anything can post a form.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.intake_form import REQUIRED_FIELDS, REQUIRED_NAMES, TEAM_ENTRY_FIELDS, intake_form_values
from db.models import Deal
from schema.models import ExperienceBucket, Tranche
from tests.conftest import QueueClient, requires_db

pytestmark = requires_db

CONTROL = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.S)
LABEL = re.compile(r'<label for="([^"]+)">(.*?)</label>', re.S)


def controls(body: str) -> dict[str, str]:
    """Every named control on the page, by name, first one wins."""
    found: dict[str, str] = {}
    for tag in CONTROL.findall(body):
        name = re.search(r'name="([^"]+)"', tag)
        if name is not None:
            found.setdefault(name.group(1), tag)
    return found


def labels(body: str) -> dict[str, str]:
    return {match.group(1): " ".join(match.group(2).split()) for match in LABEL.finditer(body)}


def form_page(client: QueueClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.status_code
    return response.text


# --- the labels a person reads --------------------------------------------------------------------


def test_the_credit_select_offers_fico_ranges_and_stores_tranches(client: QueueClient) -> None:
    """``T3`` is a caps-grid coordinate; nobody picked it off a form.  # SPEC §7.1"""
    body = form_page(client, "/queue/new")
    options = re.search(r'name="credit_range"[^>]*>(.*?)</select>', body, re.S)
    assert options is not None
    block = options.group(1)
    for value, label in (
        ("T1", "740+"),
        ("T2", "700–739"),
        ("T3", "660–699"),
        ("T4", "620–659"),
        ("T5", "Under 620"),
    ):
        assert re.search(rf'value="{value}"[^>]*>{re.escape(label)}<', block), value
    # every tranche is still offered, by its stored value
    for member in Tranche:
        assert f'value="{member.value}"' in block
    assert ">T1<" not in block and ">T5<" not in block


def test_the_experience_select_offers_deal_counts_and_stores_buckets(client: QueueClient) -> None:
    body = form_page(client, "/queue/new")
    block = re.search(r'name="experience_bucket"[^>]*>(.*?)</select>', body, re.S)
    assert block is not None
    for value, label in (("0", "0"), ("1_2", "1–2"), ("3_5", "3–5"), ("6_PLUS", "6+")):
        assert re.search(rf'value="{value}"[^>]*>{re.escape(label)}<', block.group(1)), value
    for member in ExperienceBucket:
        assert f'value="{member.value}"' in block.group(1)
    assert ">6_PLUS<" not in block.group(1) and ">1_2<" not in block.group(1)


def test_the_repeat_borrower_question_does_not_say_glenwood(client: QueueClient) -> None:
    """Every borrower on this form is a GLENWOOD borrower; the word said nothing."""
    body = form_page(client, "/queue/new")
    assert "Repeat GLENWOOD borrower" not in body
    assert labels(body)["repeat_borrower"].startswith("Repeat borrower")


def test_the_deal_page_reads_the_same_way(client: QueueClient, stored_deal: Deal) -> None:
    """A tranche and a bucket are labelled the same wherever they are shown."""
    body = form_page(client, f"/queue/deals/{stored_deal.id}")
    assert "700–739" in body  # the fixture's T2
    assert "3–5 deals in 36 months" in body  # its 3_5
    assert "Repeat GLENWOOD borrower" not in body
    assert ">T2<" not in body


def test_a_screened_deal_names_the_range_in_its_flags_and_caps_cell(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The screen summary, the flag list and the sizing table all drop the T-code."""
    team_entry["credit_range"] = "T5"  # below the floor: a Hard flag that names both tranches
    created = client.post("/intake/team", json=team_entry)
    assert created.status_code == 201, created.text
    deal_id = created.json()["id"]
    assert client.post(f"/queue/deals/{deal_id}/screen", follow_redirects=False).status_code == 303

    body = form_page(client, f"/queue/deals/{deal_id}")
    assert "Under 620 is below the floor of 620–659" in body
    assert "Caps cell" in body
    assert not re.search(r">\s*T[1-5]\s*<", body), "a raw tranche code is still on the page"
    del db_session


# --- Required or Optional, on every box -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/queue/new", "deal"])
def test_every_box_on_the_form_says_which_it_is(
    client: QueueClient, stored_deal: Deal, path: str
) -> None:
    """A person guessing which boxes matter is a person posting twice."""
    target = f"/queue/deals/{stored_deal.id}/intake" if path == "deal" else path
    body = form_page(client, target)
    shown = labels(body)
    for name in TEAM_ENTRY_FIELDS:
        assert name in shown, f"{name} has no label"
        marker = "Required" if name in REQUIRED_NAMES else "Optional"
        assert marker in shown[name], f"{name} is not marked {marker}"
    # the repeated court-matter columns are marked once each, in the header
    assert body.count('<span class="opt">Optional</span>') >= len(TEAM_ENTRY_FIELDS) - len(
        REQUIRED_NAMES
    )


def test_the_browser_is_asked_to_hold_the_same_line(client: QueueClient) -> None:
    """The marker and the attribute come from one argument; neither can drift alone."""
    found = controls(form_page(client, "/queue/new"))
    for name in TEAM_ENTRY_FIELDS:
        assert name in found, f"{name} is not on the page"
        required = re.search(r"\srequired[\s>]", found[name]) is not None
        assert required is (name in REQUIRED_NAMES), name


def test_the_required_list_is_the_minimum_viable_intake() -> None:
    """SPEC §4.1 and nothing else: the form does not invent a requirement of its own."""
    assert REQUIRED_NAMES == {
        "borrower_name",
        "borrower_phone",
        "credit_range",
        "experience_bucket",
        "repeat_borrower",
        "address",
        "purchase_price",
        "rehab_budget",
        "loan_requested",
        "term_bucket",
    }
    assert [name for name, _ in REQUIRED_FIELDS] == [
        name for name in TEAM_ENTRY_FIELDS if name in REQUIRED_NAMES
    ]


def test_the_deal_page_actions_say_which_reasons_are_required(
    client: QueueClient, stored_deal: Deal
) -> None:
    """The same treatment off the intake form: a decline records a reason, a dead deal may."""
    shown = labels(form_page(client, f"/queue/deals/{stored_deal.id}"))
    assert "Required" in shown["decline_reason"]
    assert "Optional" in shown["dead_reason"]
    assert "Required" in shown["reopen_reason"]
    assert "Required" in shown["note"]


# --- and the server says it again -----------------------------------------------------------------


def test_a_form_post_missing_a_required_box_is_refused_by_name(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """Named, because "the form could not be read" is not something a person can act on."""
    posted = {key: str(value) for key, value in team_entry.items()}
    posted["repeat_borrower"] = "false"
    del posted["purchase_price"]
    posted["borrower_phone"] = ""

    response = client.post("/intake/team", data=posted, follow_redirects=False)

    assert response.status_code == 422
    assert "Phone is required." in response.text
    assert "Purchase price is required." in response.text
    assert "Name is required." not in response.text
    # and nothing was stored
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 0
    # the rest of what was typed is still in the boxes
    assert 'value="Dana Whitfield"' in response.text


def test_a_complete_form_post_goes_through(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    posted = {key: str(value) for key, value in team_entry.items()}
    posted["repeat_borrower"] = "false"
    response = client.post("/intake/team", data=posted, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 1


def test_a_json_post_may_still_be_partial(client: QueueClient, db_session: Session) -> None:
    """The required list is the form's rule. A channel with half an intake still has one."""
    response = client.post("/intake/team", json={"address": "12 Elm St, Denver, CO 80202"})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "NEEDS_INFO"
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 1


def test_a_missing_box_and_a_bad_number_are_both_reported(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    """One post, both problems: fixing them one at a time means posting twice."""
    posted = {key: str(value) for key, value in team_entry.items()}
    posted["repeat_borrower"] = "false"
    posted["loan_requested"] = ""
    posted["purchase_price"] = "100.123"

    response = client.post("/intake/team", data=posted, follow_redirects=False)

    assert response.status_code == 422
    assert "Loan requested is required." in response.text
    assert "purchase_price" in response.text


# --- the form a deal fills in ---------------------------------------------------------------------


def test_the_deal_fills_in_every_box_the_form_renders(stored_deal: Deal) -> None:
    """A name in one and not the other renders as a missing box or a dropped value."""
    assert set(intake_form_values(stored_deal)) == set(TEAM_ENTRY_FIELDS)


def test_the_filled_form_carries_what_was_entered(stored_deal: Deal) -> None:
    values = intake_form_values(stored_deal)
    assert values["borrower_name"] == "Dana Whitfield"
    assert values["borrower_phone"] == "+19185550142"  # normalized, as stored
    assert values["entity_name"] == "Whitfield Holdings LLC"
    assert values["credit_range"] == "T2"
    assert values["experience_bucket"] == "3_5"
    assert values["repeat_borrower"] == "false"
    assert values["purchase_price"] == "185000.00"
    assert values["address"] == "1412 S Cheyenne Ave, Tulsa, OK 74119"


def test_an_inferred_state_and_product_come_back_blank(stored_deal: Deal) -> None:
    """Both carry a source column; rendering the inference would record it as a choice."""
    values = intake_form_values(stored_deal)
    assert stored_deal.property is not None and stored_deal.property.state.value == "OK"
    assert values["state"] == ""
    assert stored_deal.product is not None
    assert values["product"] == ""
