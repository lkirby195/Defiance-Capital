"""The entity name and the guarantor name survive every path a team member takes a deal down.

A partner reported both names clearing out of a deal and refusing to be typed back in. These
tests drive the pages the way a browser does: read the form the page actually renders, fill
every box it has (no box is left to a default the test did not choose), post it, follow the
redirect, open the deal page and look for both names; then open Edit Intake, post back what
it shows, and look again; then Save & Run the Underwriting Assumptions panel, and look again.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Deal
from tests.conftest import QueueClient, requires_db

pytestmark = requires_db

ENTITY = "Ridgeline Holdings LLC"
GUARANTOR = "Avery Quinlan"


class FormReader(HTMLParser):
    """Every named control in the first ``<form method="post">`` with ``action`` == target.

    ``values`` is what a browser would post untouched: a text box's ``value``, a select's
    selected option (or its first), a checked radio. ``options`` lists what a select or a
    radio group can hold, so a test can pick a non-empty one.
    """

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.inside = False
        self.done = False
        self.values: dict[str, str] = {}
        self.options: dict[str, list[str]] = {}
        self.kinds: dict[str, str] = {}
        self._select: str | None = None
        self._selected: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form" and not self.done and a.get("action") == self.action:
            self.inside = True
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text")
            self.kinds[name] = kind
            if kind in ("radio", "checkbox"):
                self.options.setdefault(name, []).append(a.get("value", "on"))
                if "checked" in a:
                    self.values[name] = a.get("value", "on")
                else:
                    self.values.setdefault(name, "") if kind == "radio" else None
            else:
                self.values[name] = a.get("value", "")
        elif tag == "select" and name:
            self._select, self._selected = name, None
            self.kinds[name] = "select"
            self.options[name] = []
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            self.options[self._select].append(value)
            if "selected" in a:
                self._selected = value
        elif tag == "textarea" and name:
            self.kinds[name] = "textarea"
            self.values[name] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.inside:
            self.inside, self.done = False, True
        elif tag == "select" and self._select is not None:
            choices = self.options[self._select]
            self.values[self._select] = (
                self._selected if self._selected is not None else (choices[0] if choices else "")
            )
            self._select = None


def read_form(html: str, action: str) -> FormReader:
    reader = FormReader(action)
    reader.feed(html)
    assert reader.done, f"no form posting to {action}"
    return reader


# What a person types into each box on the New Deal form. Every box the page renders must be
# here, or the test fails: a box the test skipped is a box whose clearing it would not see.
TYPED: dict[str, str] = {
    "entity_name": ENTITY,
    "borrower_name": GUARANTOR,
    "borrower_phone": "405-555-0142",
    "borrower_email": "avery@ridgeline.example",
    "address": "1412 S Boston Ave",
    "listing_url": "",
    "city": "Tulsa",
    "state": "OK",
    "units": "1",
    "structures": "1",
    "sf": "1450",
    "year_built": "1962",
    "year_renovated": "2004",
    "beds": "3",
    "baths": "2",
    "garage_spaces": "1",
    "purchase_price": "$150,000",
    "rehab_costs": "$40,000",
    "loan_requested": "$140,000",
    "closing_date": "2026-11-12",
    "term_months": "9",
    # not the intake: what to call the version the deal arrives with (New Deal only, SPEC §5)
    "version_name": "First look",
}


def fill(reader: FormReader) -> dict[str, str]:
    """Every control the form rendered, typed or picked; nothing left to the page."""
    body = dict(reader.values)
    for name, kind in reader.kinds.items():
        if name == "_csrf":
            continue
        if name in TYPED:
            body[name] = TYPED[name]
        elif kind in ("select", "radio"):
            choices = [c for c in reader.options.get(name, []) if c]
            assert choices, f"{name} offers nothing to pick"
            body[name] = choices[0]
        else:
            pytest.fail(f"the New Deal form has a box the test does not fill: {name}")
    return body


def names_on(page: str) -> tuple[bool, bool]:
    return ENTITY in page, GUARANTOR in page


def overview_value(page: str, label: str) -> str:
    """The ``<dd>`` after a ``<dt>`` in the deal page's Overview, whitespace collapsed."""
    match = re.search(rf"<dt>{re.escape(label)}</dt>\s*<dd>(.*?)</dd>", page, re.S)
    assert match, f"no {label} on the deal page"
    return " ".join(match.group(1).split())


def create(client: QueueClient, db_session: Session, **changes: str) -> tuple[Deal, str]:
    new = client.get("/queue/new")
    assert new.status_code == 200
    body = {**fill(read_form(new.text, "/intake/team")), **changes}
    posted = client.post("/intake/team", data=body, follow_redirects=False)
    assert posted.status_code == 303, posted.text[:3000]
    (deal,) = list(db_session.scalars(select(Deal)))
    page = client.get(posted.headers["location"])
    assert page.status_code == 200
    return deal, page.text


def assert_both(page: str) -> None:
    assert overview_value(page, "Entity Name") == ENTITY
    assert overview_value(page, "Guarantor Name") == GUARANTOR


def test_new_deal_with_every_box_filled_shows_both_names(
    client: QueueClient, db_session: Session
) -> None:
    _, page = create(client, db_session)
    assert_both(page)


@pytest.mark.parametrize("phone", ["405-555-0142", ""], ids=["with_phone", "no_phone"])
def test_both_names_survive_edit_intake_posted_back_unchanged(
    client: QueueClient, db_session: Session, phone: str
) -> None:
    deal, page = create(client, db_session, borrower_phone=phone)
    assert_both(page)

    url = f"/queue/deals/{deal.id}/intake"
    edit = client.get(url)
    assert edit.status_code == 200
    reader = read_form(edit.text, url)
    assert reader.values["entity_name"] == ENTITY
    assert reader.values["borrower_name"] == GUARANTOR
    posted = client.post(url, data=reader.values, follow_redirects=False)
    assert posted.status_code == 303, posted.text[:3000]
    assert_both(client.get(f"/queue/deals/{deal.id}").text)


@pytest.mark.parametrize("phone", ["405-555-0142", ""], ids=["with_phone", "no_phone"])
def test_both_names_can_be_typed_back_in_through_edit_intake(
    client: QueueClient, db_session: Session, phone: str
) -> None:
    """A deal stored with neither name takes both on Edit Intake."""
    deal, page = create(client, db_session, borrower_phone=phone, entity_name="")
    assert overview_value(page, "Entity Name") == "—"

    url = f"/queue/deals/{deal.id}/intake"
    reader = read_form(client.get(url).text, url)
    body: dict[str, Any] = {**reader.values, "entity_name": ENTITY, "borrower_name": GUARANTOR}
    posted = client.post(url, data=body, follow_redirects=False)
    assert posted.status_code == 303, posted.text[:3000]
    assert_both(client.get(f"/queue/deals/{deal.id}").text)


@pytest.mark.parametrize("phone", ["405-555-0142", ""], ids=["with_phone", "no_phone"])
def test_both_names_survive_save_and_run(
    client: QueueClient, db_session: Session, phone: str
) -> None:
    deal, page = create(client, db_session, borrower_phone=phone)
    url = f"/queue/deals/{deal.id}/assumptions"
    reader = read_form(page, url)
    posted = client.post(url, data=reader.values, follow_redirects=False)
    assert posted.status_code == 303, posted.text[:3000]
    assert_both(client.get(f"/queue/deals/{deal.id}").text)
