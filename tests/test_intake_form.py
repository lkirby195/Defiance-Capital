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
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.intake_form import (
    REQUIRED_FIELDS,
    REQUIRED_NAMES,
    TEAM_ENTRY_FIELDS,
    VERSION_NAME,
    intake_form_values,
)
from db.models import Deal
from db.repository import FORM_COLUMNS
from schema.models import ExperienceBucket, Product, ProductSource, Tranche
from services import latest_screen, screen_result
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
    assert "740+" in body  # the fixture's T1
    assert "<dd>6+</dd>" in body  # its 6_PLUS
    assert "Repeat GLENWOOD borrower" not in body
    assert ">T1<" not in body


def test_the_two_read_enums_are_words_not_codes(client: QueueClient, stored_deal: Deal) -> None:
    """SPEC §3: title case with spaces wherever a person reads one."""
    body = form_page(client, f"/queue/deals/{stored_deal.id}")
    assert re.search(r"<dd[^>]*>Holdback", body)  # the inferred structure
    # the code stays the stored value on the radio, and is never the words on the page
    assert not re.search(r">\s*ACQUISITION_REHAB\s*<", body)
    assert "<dd>Purchase</dd>" in body  # the loan purpose
    # ...and not the §3 definition of the product: the (?) that carried it is gone (v0.15)
    assert "the rehab holdback drawn over the rehab period" not in body


def test_the_loan_structure_is_a_radio_of_two_and_the_type_select_is_gone(
    client: QueueClient,
) -> None:
    """The two radios, "100% at close" / "Holdback", after the Loan Amount (SPEC §3, §8.1)."""
    body = form_page(client, "/queue/new")
    radios = re.findall(r'<input[^>]*type="radio" name="product" value="([A-Z_]+)"', body)
    assert radios == ["ACQUISITION", "ACQUISITION_REHAB"]
    assert ">\n 100% at close</label>" in body or "> 100% at close</label>" in body
    assert "> Holdback</label>" in body
    assert 'role="radiogroup" aria-label="Loan Structure"' in body
    # nothing is picked on a new deal: blank is inferred from the rehab costs
    assert not re.search(r'name="product" value="[A-Z_]+"\s+checked', body)
    # the old select and its four products are gone
    assert not re.search(r'<select[^>]*name="product"', body)
    assert "Loan Type" not in body
    for gone in ("No Draw", "Split Draw", "Split Principal", "Wholetail"):
        assert gone not in body, gone


def test_holdback_reveals_the_rehab_portion_box_without_script(client: QueueClient) -> None:
    """A stylesheet rule on the checked radio, no JS (CLAUDE.md): the box is in the page and
    hidden until Holdback is picked, where :has() is supported."""
    body = form_page(client, "/queue/new")
    box = re.search(r'<div class="holdback-only">(.*?)</div>\s*</div>', body, re.S)
    assert box is not None
    assert 'name="loan_rehab_portion"' in box.group(1) and "Rehab portion" in box.group(1)
    assert 'data-mask="money"' in box.group(1)
    assert '<div class="grid2 structure">' in body
    assert "@supports selector(:has(*))" in body
    assert 'input[name="product"][value="ACQUISITION_REHAB"]:checked' in body


def test_there_are_no_analysis_toggles_on_the_form_or_the_deal_page(
    client: QueueClient, stored_deal: Deal
) -> None:
    """SPEC §8.1: all three analyses run on every deal (engine 2.0.0)."""
    for target in ("/queue/new", f"/queue/deals/{stored_deal.id}"):
        body = form_page(client, target)
        assert 'name="flip_analysis"' not in body and 'name="rental_analysis"' not in body
        assert 'name="take_back_analysis"' not in body


# --- the form is exactly the listed boxes, and nothing else (SPEC §8.1) ---------------------------

DEAL_ECONOMICS_IN_ORDER = (
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "product",
    "loan_rehab_portion",
    "loan_purpose",
    "closing_date",
    "term_months",
)
NOT_ON_THE_FORM = (
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
    "loan_purchase_portion",
    "payoff_date",
    "estimated_sale_price_team",
    "monthly_rent",
    "flip_analysis",
    "rental_analysis",
    "court_records_status",
    "court_records_as_of",
    "matter_code",
    "asset_type",
    "stated_exit",
)


def form_control_names(body: str) -> list[str]:
    """Every named control inside the form, in document order, the CSRF field aside."""
    inner = re.search(r'<form class="stack"[^>]*>(.*?)</form>', body, re.S)
    assert inner is not None
    names = [re.search(r'name="([^"]+)"', tag) for tag in CONTROL.findall(inner.group(1))]
    found = [m.group(1) for m in names if m is not None and m.group(1) != "_csrf"]
    # a radio group is one control: its inputs share the name, one after another
    return [name for at, name in enumerate(found) if at == 0 or found[at - 1] != name]


@pytest.mark.parametrize("path", ["/queue/new", "deal"])
def test_the_form_renders_exactly_the_listed_boxes_in_order(
    client: QueueClient, stored_deal: Deal, path: str
) -> None:
    target = f"/queue/deals/{stored_deal.id}/intake" if path == "deal" else path
    names = form_control_names(form_page(client, target))
    # New Deal adds one box after the three groups, not the intake: the arrival version's name
    if path == "/queue/new":
        assert names.pop() == VERSION_NAME
    assert names == list(TEAM_ENTRY_FIELDS)
    assert tuple(names[-8:]) == DEAL_ECONOMICS_IN_ORDER
    assert names[:7] == [
        "entity_name",
        "borrower_name",
        "borrower_phone",
        "borrower_email",
        "credit_range",
        "experience_bucket",
        "repeat_borrower",
    ]
    for gone in NOT_ON_THE_FORM:
        assert gone not in names, gone


def test_the_form_s_boxes_are_the_columns_an_edit_writes() -> None:
    """One list for the form and one for Edit Intake, held together here (SPEC §8.1)."""
    assert set(FORM_COLUMNS) == {
        "credit_range_self_reported",
        "experience_bucket_self_reported",
        "repeat_borrower_self_reported",
        "purchase_price",
        "rehab_costs",
        "loan_requested",
        "loan_purpose",
        "product",
        "product_source",
        "loan_purchase_portion",
        "loan_rehab_portion",
        "closing_date",
        "term_bucket",
        "term_months",
        "term_stub_days",
    }


@pytest.mark.parametrize("path", ["/queue/new", "deal"])
def test_the_form_prints_labels_only(client: QueueClient, stored_deal: Deal, path: str) -> None:
    """No helper text, no hints, no inline comments, no (?) marks (SPEC §8.1)."""
    target = f"/queue/deals/{stored_deal.id}/intake" if path == "deal" else path
    body = form_page(client, target)
    inner = re.search(r'<form class="stack".*?</form>', body, re.S)
    assert inner is not None
    form = inner.group(0)
    assert "<p" not in form, "a paragraph of prose inside the form"
    assert "<dl" not in form and 'class="help"' not in form and "SPEC" not in form
    for text in labels(body).values():
        assert "—" not in text and " - " not in text, text
    # the headings are the three groups and nothing more
    headings = re.findall(r"<h2>(.*?)</h2>", form)
    assert headings == ["Overview", "Property Overview", "Deal Economics"]
    assert "<h3" not in form


def test_the_marks_are_plain_text_in_the_label_s_own_type(client: QueueClient) -> None:
    body = form_page(client, "/queue/new")
    assert '<span class="mark">Required</span>' in body
    assert '<span class="mark">Optional</span>' in body
    for old in ('class="req"', 'class="opt"', 'class="cond"', ".req {", ".opt {", ".cond {"):
        assert old not in body, old
    assert re.search(r"\.mark \{[^}]*font: inherit;[^}]*color: inherit;", body)


def test_a_screened_deal_names_the_range_in_its_flags_and_caps_cell(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The stored flag and the page's sizing table both drop the T-code."""
    team_entry["credit_range"] = "T5"  # below the floor: a Hard flag that names both tranches
    created = client.post("/intake/team", json=team_entry)
    assert created.status_code == 201, created.text
    deal_id = created.json()["id"]
    # the deal declines on arrival, so the ledger stands down: a conflict, with the verdict kept
    assert client.post(f"/queue/deals/{deal_id}/run").status_code == 409

    row = latest_screen(db_session, UUID(deal_id))
    assert row is not None
    messages = [flag.message for flag in screen_result(row).flags]
    assert any("Under 620 is below the floor of 620–659" in message for message in messages)
    body = form_page(client, f"/queue/deals/{deal_id}")
    assert "Caps cell" in body
    assert not re.search(r">\s*T[1-5]\s*<", body), "a raw tranche code is still on the page"


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
    extra = 1 if path == "/queue/new" else 0  # the Version name box, Optional
    if extra:
        assert "Optional" in shown[VERSION_NAME]
    assert (
        body.count('<span class="mark">Optional</span>')
        == len(TEAM_ENTRY_FIELDS) - len(REQUIRED_NAMES) + extra
    )
    assert body.count('<span class="mark">Required</span>') == len(REQUIRED_NAMES)


def test_the_browser_is_asked_to_hold_the_same_line(client: QueueClient) -> None:
    """The marker and the attribute come from one argument; neither can drift alone."""
    found = controls(form_page(client, "/queue/new"))
    for name in TEAM_ENTRY_FIELDS:
        assert name in found, f"{name} is not on the page"
        required = re.search(r"\srequired[\s>]", found[name]) is not None
        assert required is (name in REQUIRED_NAMES), name


def test_the_required_list_is_the_minimum_viable_intake() -> None:
    """What an engine run cannot proceed without, and nothing else (SPEC §4.1, §8.1).

    The credit range is not on the list: a person on the phone often does not have it yet,
    and the screen names it by hand rather than the form refusing to submit. The one that
    joined it is the one the ledger has no stand-in for: the term in months. The rate and the
    closing date have defaults (SPEC §8.1); the rate is not on the form at all and the closing
    date is optional on it.
    """
    assert REQUIRED_NAMES == {
        "borrower_name",
        "experience_bucket",
        "repeat_borrower",
        "address",
        "purchase_price",
        "rehab_costs",
        "loan_requested",
        "term_months",
    }
    assert "interest_rate" not in REQUIRED_NAMES
    assert "closing_date" not in REQUIRED_NAMES
    # the credit range, because a person on the phone often does not have it yet; the phone,
    # because a deal that arrived by email has a name and no number (SPEC §4.1)
    assert "credit_range" not in REQUIRED_NAMES
    assert "borrower_phone" not in REQUIRED_NAMES
    assert [name for name, _ in REQUIRED_FIELDS] == [
        name for name in TEAM_ENTRY_FIELDS if name in REQUIRED_NAMES
    ]


def test_the_deal_page_actions_say_which_reasons_are_required(
    client: QueueClient, stored_deal: Deal
) -> None:
    """The same treatment off the intake form: a decline records a reason, a dead deal may."""
    shown = labels(form_page(client, f"/queue/deals/{stored_deal.id}"))
    assert "Required" in shown["decline_reason"]
    assert "Required" in shown["reopen_reason"]
    assert "Required" in shown["note"]
    # Kill asks on a page of its own, and its reason is optional
    killing = labels(form_page(client, f"/queue/deals/{stored_deal.id}/kill"))
    assert "Optional" in killing["kill_reason"]


# --- and the server says it again -----------------------------------------------------------------


def test_a_form_post_missing_a_required_box_is_refused_by_name(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """Named, because "the form could not be read" is not something a person can act on."""
    posted = {key: str(value) for key, value in team_entry.items()}
    posted["repeat_borrower"] = "false"
    del posted["purchase_price"]
    posted["borrower_name"] = ""

    response = client.post("/intake/team", data=posted, follow_redirects=False)

    assert response.status_code == 422
    assert "Guarantor Name is required." in response.text
    assert "Purchase Price is required." in response.text
    assert "Address is required." not in response.text
    # and nothing was stored
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 0
    # the rest of what was typed is still in the boxes, masked as the box had it
    assert 'value="720-555-0192"' in response.text
    assert 'value="$195,000"' in response.text


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
    assert "Loan Amount is required." in response.text
    assert "purchase_price" in response.text


# --- the form a deal fills in ---------------------------------------------------------------------


def test_the_deal_fills_in_every_box_the_form_renders(stored_deal: Deal) -> None:
    """A name in one and not the other renders as a missing box or a dropped value."""
    assert set(intake_form_values(stored_deal)) == set(TEAM_ENTRY_FIELDS)


def test_the_filled_form_carries_what_was_entered(stored_deal: Deal) -> None:
    values = intake_form_values(stored_deal)
    assert values["borrower_name"] == "Rafael Ortiz"
    assert values["entity_name"] == "Ortiz Builds LLC"
    assert values["credit_range"] == "T1"
    assert values["experience_bucket"] == "6_PLUS"
    assert values["repeat_borrower"] == "false"
    assert values["address"] == "3320 Meade St, Denver, CO 80211"
    assert values["closing_date"] == "2026-10-01"


def test_the_filled_form_is_masked_the_way_the_boxes_take_it(stored_deal: Deal) -> None:
    """SPEC §8.1: a phone is ###-###-####, money is $#,###, a rate is a percent."""
    values = intake_form_values(stored_deal)
    assert values["borrower_phone"] == "720-555-0192"  # stored as digits
    assert values["purchase_price"] == "$200,000"
    assert values["loan_requested"] == "$195,000"
    assert values["term_months"] == "9"


def test_the_defaulted_economics_are_on_the_deal_not_on_the_form(stored_deal: Deal) -> None:
    """The five §8.1 economics with a default populate when the deal is stored and are
    edited on the deal page; the form has no box for them (SPEC §8.1)."""
    assert stored_deal.contingency_pct == Decimal("0")
    assert stored_deal.origination_fee_pct == Decimal("0.02")
    assert {"contingency_pct", "origination_fee_pct"} <= set(stored_deal.defaulted_fields)
    values = intake_form_values(stored_deal)
    for name in ("interest_rate", "contingency_pct", "closing_costs_usd", "payoff_date"):
        assert name not in values, name


def test_an_inferred_state_and_product_come_back_blank(stored_deal: Deal) -> None:
    """Both carry a source column; rendering the inference would record it as a choice."""
    values = intake_form_values(stored_deal)
    assert stored_deal.property is not None and stored_deal.property.state.value == "CO"
    assert values["state"] == ""
    assert stored_deal.product is not None
    assert values["product"] == ""


# --- the loan structure and the rehab portion (SPEC §3, §8.1, v0.17) ------------------------------


def posted_form(team_entry: dict[str, Any], **changes: str) -> dict[str, str]:
    """The fixture as the browser posts it: no advance box, the rehab portion as typed."""
    posted = {key: str(value) for key, value in team_entry.items() if value is not None}
    posted["repeat_borrower"] = "false"
    posted.pop("loan_purchase_portion", None)
    posted.pop("loan_rehab_portion", None)
    posted.update(changes)
    return posted


def only_deal(db_session: Session) -> Deal:
    deal = db_session.scalar(select(Deal))
    assert deal is not None
    return deal


def test_holdback_with_a_typed_rehab_portion_advances_the_rest(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """$195,000 with $40,000 held back advances $155,000 at closing, the team's own split."""
    posted = posted_form(team_entry, product="ACQUISITION_REHAB", loan_rehab_portion="$40,000")
    response = client.post("/intake/team", data=posted, follow_redirects=False)
    assert response.status_code == 303, response.text
    deal = only_deal(db_session)
    assert deal.product is Product.ACQUISITION_REHAB
    assert deal.product_source is ProductSource.ENTERED
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (
        Decimal("155000.00"),
        Decimal("40000.00"),
    )
    assert "loan_rehab_portion" not in deal.defaulted_fields


def test_holdback_with_a_blank_rehab_portion_takes_the_default(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The rehab budget times one plus the contingency (0%), capped at the loan amount."""
    posted = posted_form(team_entry, product="ACQUISITION_REHAB")
    assert client.post("/intake/team", data=posted, follow_redirects=False).status_code == 303
    deal = only_deal(db_session)
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (
        Decimal("147000.00"),
        Decimal("48000.00"),
    )
    assert {"loan_purchase_portion", "loan_rehab_portion"} <= set(deal.defaulted_fields)


def test_a_rehab_portion_above_the_loan_is_refused_under_its_box(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    posted = posted_form(team_entry, product="ACQUISITION_REHAB", loan_rehab_portion="$200,000")
    response = client.post("/intake/team", data=posted, follow_redirects=False)
    assert response.status_code == 422
    assert re.search(
        r'name="loan_rehab_portion".*?<span class="err" id="loan_rehab_portion-error">'
        r"Rehab portion cannot be more than the Loan Amount\.</span>",
        response.text,
        re.S,
    )
    # the box keeps what was typed, and is shown though the stylesheet would hide it
    assert '<div class="holdback-only shown">' in response.text
    assert db_session.scalar(select(func.count()).select_from(Deal)) == 0


def test_100_pct_at_close_ignores_the_hidden_rehab_portion(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The box is hidden at 100% at close; whatever it still holds is not a split."""
    posted = posted_form(team_entry, product="ACQUISITION", loan_rehab_portion="$40,000")
    assert client.post("/intake/team", data=posted, follow_redirects=False).status_code == 303
    deal = only_deal(db_session)
    assert deal.product is Product.ACQUISITION
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (None, None)


def test_an_unpicked_structure_is_inferred_and_a_typed_portion_still_counts(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """No radio picked and a rehab budget: a holdback, inferred, with the portion typed."""
    posted = posted_form(team_entry, loan_rehab_portion="$30,000")
    assert client.post("/intake/team", data=posted, follow_redirects=False).status_code == 303
    deal = only_deal(db_session)
    assert deal.product is Product.ACQUISITION_REHAB
    assert deal.product_source is ProductSource.INFERRED
    assert deal.loan_rehab_portion == Decimal("30000.00")


def test_edit_intake_shows_a_typed_rehab_portion_and_not_a_default(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """A default portion is left blank, so an edit that moves the rehab works it out again."""
    posted = posted_form(team_entry, product="ACQUISITION_REHAB", loan_rehab_portion="$40,000")
    assert client.post("/intake/team", data=posted, follow_redirects=False).status_code == 303
    deal = only_deal(db_session)
    values = intake_form_values(deal)
    assert values["product"] == "ACQUISITION_REHAB" and values["loan_rehab_portion"] == "$40,000"
    page = form_page(client, f"/queue/deals/{deal.id}/intake")
    assert re.search(r'name="product" value="ACQUISITION_REHAB"\s+checked', page)
    assert re.search(r'id="loan_rehab_portion"[^>]*value="\$40,000"', page)

    # back to the default: blank on the edit form, and a new rehab budget moves it
    edit = posted_form(team_entry, product="ACQUISITION_REHAB", rehab_costs="$50,000")
    response = client.post(f"/queue/deals/{deal.id}/intake", data=edit, follow_redirects=False)
    assert response.status_code == 303, response.text
    db_session.expire_all()
    deal = only_deal(db_session)
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (
        Decimal("145000.00"),
        Decimal("50000.00"),
    )
    assert intake_form_values(deal)["loan_rehab_portion"] == ""
