"""A rejected form comes back as a page, with each complaint beside the box it is about.

# SPEC §8.1, §12

Two things are asserted here and they are separate promises.

**Nothing a person can type produces a 500.** Every ``ValueError`` and every
``ValidationError`` raised while handling the team form, the override block, Edit Intake or an
underwrite request is answered with the same form, re-rendered, carrying what was typed. That
includes the ones nobody reaches through a browser box - the engine refusing to price a deal
whose commitment is zero, a deal id that is not a deal - because a 500 tells the team nothing
and loses their typing.

**And the complaint sits under the box.** ``api/problems.py`` files each message by field and
``_fields.html`` prints it there, with ``aria-describedby`` pointing at it, so a person fixing
three boxes reads three lines in three places rather than a list at the top they have to match
up themselves. What stays at the top is what is genuinely about more than one box: the term
against the payoff date against the closing date is the example, because there is no single
box a message about three of them belongs under.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy.orm import Session

from db.models import Deal
from tests.conftest import QueueClient, form_body, requires_db, store_deal

pytestmark = requires_db

# Every state-changing POST the queue serves, and a body that satisfies each one's own form.
QUEUE_POSTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("screen", {}),
    ("underwrite", {}),
    ("overrides", {}),
    ("intake", {}),
    ("advance", {}),
    ("decline", {"reason": "not for us"}),
    ("dead", {"reason": "gone quiet"}),
    ("reopen", {"reason": "back on"}),
    ("note", {"note": "a note"}),
)

MISSING_DEAL = "00000000-0000-0000-0000-000000000000"


def field_error(body: str, name: str) -> str | None:
    """The message printed under one box, or None when that box carries none."""
    found = re.search(rf'<span class="err" id="{name}-error">(.*?)</span>', body, re.S)
    return None if found is None else " ".join(found.group(1).split())


def flagged(body: str, name: str) -> bool:
    """Whether the box itself is marked invalid and points at its own message."""
    return bool(
        re.search(rf'id="{name}"[^>]*aria-invalid="true" aria-describedby="{name}-error"', body)
    )


def top_problems(body: str) -> list[str]:
    """The cross-field complaints the page prints above everything."""
    return [
        " ".join(text.split()) for text in re.findall(r'<p class="problem">(.*?)</p>', body, re.S)
    ]


# --- the payoff date: the case that used to be refused, and the one still refused ---------------


def test_a_payoff_date_that_contradicts_the_term_re_renders_the_deal_page(
    client: QueueClient, db_session: Session, stored_deal: Deal
) -> None:
    """The payoff-date case. A 500 here would lose the block somebody had just filled in."""
    response = client.post(
        f"/queue/deals/{stored_deal.id}/overrides",
        data={
            "closing_date": "2027-01-01",
            "term_months": "6",
            "payoff_date": "2027-10-01",
            "interest_rate": "12%",
        },
        follow_redirects=False,
    )

    assert response.status_code == 422
    body = response.text
    # It is about three boxes at once, so it goes to the top rather than under any one.
    assert len(top_problems(body)) == 1
    complaint = top_problems(body)[0]
    assert "disagree" in complaint
    assert "6 month(s)" in complaint and "2027-10-01" in complaint
    assert "Clear whichever one you did not mean" in complaint
    # ...and what was typed is still in the boxes.
    assert 'id="term_months"' in body and 'value="6"' in body
    db_session.expire_all()
    assert db_session.get(Deal, stored_deal.id).term_months == 9  # type: ignore[union-attr]


def test_a_mid_month_payoff_date_is_now_accepted_and_stored_as_a_stub(
    client: QueueClient, db_session: Session, stored_deal: Deal
) -> None:
    """The other half of the same change: SPEC §8.1 takes any date after closing."""
    response = client.post(
        f"/queue/deals/{stored_deal.id}/overrides",
        data={"closing_date": "2027-03-15", "payoff_date": "2027-12-26", "interest_rate": "12%"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    db_session.expire_all()
    deal = db_session.get(Deal, stored_deal.id)
    assert deal is not None
    assert (deal.term_months, deal.term_stub_days) == (9, 11)


def test_a_payoff_date_before_the_closing_date_is_still_refused_by_name(
    client: QueueClient, stored_deal: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{stored_deal.id}/overrides",
        data={"closing_date": "2027-03-15", "payoff_date": "2027-01-01"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert any("on or before the closing date" in line for line in top_problems(response.text))


# --- and one other: a box whose own value is wrong -----------------------------------------------


def test_a_price_of_nothing_is_answered_under_the_price_box(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    """The other case. One box is wrong, so one box carries the message."""
    response = client.post(
        "/intake/team", data=form_body(team_entry, purchase_price="$0"), follow_redirects=False
    )

    assert response.status_code == 422
    body = response.text
    assert field_error(body, "purchase_price") == "Input should be greater than 0"
    assert flagged(body, "purchase_price")
    # ...and nothing lands at the top, because nothing here is about more than one box.
    assert top_problems(body) == []
    # ...and no other box is marked.
    assert field_error(body, "loan_requested") is None


def test_a_required_box_left_empty_says_so_where_the_box_is(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    body = form_body(team_entry)
    del body["purchase_price"]
    del body["borrower_name"]

    response = client.post("/intake/team", data=body, follow_redirects=False)

    assert response.status_code == 422
    assert field_error(response.text, "purchase_price") == "Purchase Price is required."
    assert field_error(response.text, "borrower_name") == "Guarantor Name is required."
    assert top_problems(response.text) == []


def test_several_wrong_boxes_are_all_answered_at_once(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    """A person fixing one box at a time is a person posting four times."""
    response = client.post(
        "/intake/team",
        data=form_body(team_entry, purchase_price="$0", year_built="12", baths="1.234"),
        follow_redirects=False,
    )
    assert response.status_code == 422
    for name in ("purchase_price", "year_built", "baths"):
        assert field_error(response.text, name), name


def test_a_court_matter_names_the_row_it_is_in(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    """A repeated row has no box of its own to sit under, so it says which row it is."""
    body = form_body(team_entry)
    body["matter_code"] = "BANKRUPTCY_IN_LOOKBACK"  # a lookback code with no date on it

    response = client.post("/intake/team", data=body, follow_redirects=False)

    assert response.status_code == 422
    assert any("Matters found, row 1" in line for line in top_problems(response.text))


# --- Edit Intake answers the same way ------------------------------------------------------------


def test_edit_intake_re_renders_with_the_complaint_under_the_box(
    client: QueueClient, db_session: Session, stored_deal: Deal, team_entry: dict[str, Any]
) -> None:
    response = client.post(
        f"/queue/deals/{stored_deal.id}/intake",
        data=form_body(team_entry, loan_requested="$0"),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert field_error(response.text, "loan_requested") == "Input should be greater than 0"
    db_session.expire_all()
    assert db_session.get(Deal, stored_deal.id).loan_requested is not None  # type: ignore[union-attr]


def test_edit_intake_takes_a_mid_month_payoff_date(
    client: QueueClient, db_session: Session, stored_deal: Deal, team_entry: dict[str, Any]
) -> None:
    body = form_body(team_entry, closing_date="2027-03-15", payoff_date="2027-12-26")
    del body["term_months"]

    response = client.post(
        f"/queue/deals/{stored_deal.id}/intake", data=body, follow_redirects=False
    )

    assert response.status_code == 303, response.text
    db_session.expire_all()
    deal = db_session.get(Deal, stored_deal.id)
    assert deal is not None
    assert (deal.term_months, deal.term_stub_days) == (9, 11)


# --- the underwrite request: a deal the engine will not price ------------------------------------


@pytest.fixture
def unpriceable_deal(db_session: Session, team_entry: dict[str, Any]) -> Deal:
    """A SPLIT_PRINCIPAL whose whole loan is a rehab tranche against no rehab budget.

    The split adds up and every column is filled in, so nothing refuses it before the engine:
    the rehab portion is capped at a contingency-adjusted rehab cost of zero (SPEC §8.2), so
    the commitment is zero and every §8 figure divides by it.
    """
    return store_deal(
        db_session,
        {
            **team_entry,
            "product": "SPLIT_PRINCIPAL",
            "rehab_costs": "0.00",
            "loan_requested": "100000.00",
            "loan_purchase_portion": "0.00",
            "loan_rehab_portion": "100000.00",
        },
    )


def test_a_deal_the_engine_cannot_price_says_so_on_the_page(
    client: QueueClient, unpriceable_deal: Deal
) -> None:
    """Not a 500: every value is present, and what they add up to is the problem."""
    response = client.post(f"/queue/deals/{unpriceable_deal.id}/underwrite", follow_redirects=False)

    assert response.status_code == 422
    assert any("no commitment cannot be priced" in line for line in top_problems(response.text))


def test_the_json_underwrite_route_answers_422_rather_than_500(
    client: QueueClient, unpriceable_deal: Deal
) -> None:
    response = client.post(f"/deals/{unpriceable_deal.id}/underwrite", json={})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("no commitment cannot be priced" in reason for reason in detail["reasons"])


# --- and a deal that is not there at all ---------------------------------------------------------


@pytest.mark.parametrize(("action", "body"), QUEUE_POSTS, ids=[action for action, _ in QUEUE_POSTS])
def test_no_queue_action_on_a_missing_deal_is_a_server_error(
    client: QueueClient, db_session: Session, action: str, body: dict[str, str]
) -> None:
    """A stale link is a wrong link, not a fault; it goes back to the queue and says so."""
    response = client.post(
        f"/queue/deals/{MISSING_DEAL}/{action}", data=body, follow_redirects=False
    )
    assert response.status_code in (303, 422), (action, response.status_code)
    if response.status_code == 303:
        assert response.headers["location"].startswith("/queue?notice=")
    db_session.rollback()
