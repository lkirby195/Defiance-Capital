"""The term the deal is priced on, and the two ways of saying it.  # SPEC §8.1

One term, two boxes. The team form takes a term in months or a payoff date, entering either
solves the other, and entering both means they have to agree — a term and a date that disagree
are a person having edited one box and left the other behind, and the form says so by name
rather than picking one.

`term_bucket` is the borrower's own answer to "how long do you need the loan?", asked by the
borrower channels (SPEC §4.1). It seeds `term_months` at intake — every bucket but `12_PLUS`
names a number — and it does not fix it: a deal repriced to 7 months on a 6-month ask is a
real thing rather than a row to reject. **The team form does not ask it at all** (v0.5): a
person with the whole deal in front of them knows the term, and a bucket beside it would be a
second number to keep in step with the first.

`deal.term` on the missing-fields list is one row for both columns: a deal that has a bucket or
a term has answered the question, and one with neither has not.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from db.models import Deal
from intake.normalize import ParsedIntake, infer_term_months, normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import Channel, DealInfo, Status, TermBucket, months_for_bucket
from services import TeamOverrides, save_overrides
from tests.conftest import ACTOR, requires_db

pytestmark = requires_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def parsed(deal: DealInfo) -> ParsedIntake:
    """A borrower-channel intake carrying nothing but the deal block under test."""
    return ParsedIntake(deal=deal)


def payload(**overrides: Any) -> dict[str, Any]:
    """The complete team entry (a 9-month term), with changes."""
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def form_post(client: TestClient, data: dict[str, Any]) -> Any:
    body = {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in data.items()
    }
    return client.post("/intake/team", data=body, follow_redirects=False)


def stored(client: TestClient, session: Session, data: dict[str, Any]) -> Deal:
    response = client.post("/intake/team", json=data)
    assert response.status_code == 201, response.text
    deal = session.get(Deal, response.json()["id"])
    assert deal is not None
    return deal


# --- the bucket, where a borrower channel still asks it ------------------------------------------


@pytest.mark.parametrize(
    "bucket,months",
    [(TermBucket.M3, 3), (TermBucket.M6, 6), (TermBucket.M9, 9), (TermBucket.M12, 12)],
)
def test_a_bucket_that_names_months_names_them(bucket: TermBucket, months: int) -> None:
    assert months_for_bucket(bucket) == months
    assert infer_term_months(bucket, None) == months


def test_twelve_plus_names_none_and_keeps_what_the_team_typed() -> None:
    assert months_for_bucket(TermBucket.M12_PLUS) is None
    assert infer_term_months(TermBucket.M12_PLUS, 18) == 18
    assert infer_term_months(TermBucket.M12_PLUS, None) is None
    assert infer_term_months(None, None) is None


def test_the_team_form_does_not_ask_the_bucket_at_all() -> None:
    """SPEC §8.1: the borrower channels ask it; a person with the deal in front of them
    knows the term, and a second number to keep in step is a second number to get wrong."""
    with pytest.raises(ValidationError, match="term_bucket"):
        TeamEntryForm(**payload(term_bucket="9"))


def test_the_bucket_still_seeds_the_term_on_a_borrower_channel_intake() -> None:
    seeded = normalize(parsed(DealInfo(term_bucket=TermBucket.M6)), Channel.SMS, raw_payload={})
    assert seeded.deal.term_months == 6
    assert "deal.term" not in seeded.missing_fields

    own = normalize(
        parsed(DealInfo(term_bucket=TermBucket.M9, term_months=11)), Channel.SMS, raw_payload={}
    )
    assert own.deal.term_months == 11  # the team's own wins; the ask stays on the record
    assert own.deal.term_bucket is TermBucket.M9


# --- one term, two boxes (SPEC §8.1) -------------------------------------------------------------


def test_the_normalizer_takes_the_term_off_the_form() -> None:
    record = normalize(parse_team_form(TeamEntryForm(**payload())), Channel.TEAM, raw_payload={})
    assert record.deal.term_bucket is None
    assert record.deal.term_months == 9


def test_a_deal_entered_through_the_form_carries_it(
    client: TestClient, db_session: Session
) -> None:
    assert form_post(client, payload()).status_code == 303
    deal = db_session.query(Deal).one()
    assert deal.term_months == 9


def test_a_payoff_date_solves_the_term() -> None:
    form = TeamEntryForm(
        **payload(term_months=None, closing_date="2027-01-01", payoff_date="2027-08-01")
    )
    assert form.effective_term_months == 7


def test_a_term_solves_the_payoff_date() -> None:
    """The other direction is the same fact: the payoff date is closing plus the term."""
    record = normalize(
        parse_team_form(TeamEntryForm(**payload(closing_date="2027-01-31", term_months=1))),
        Channel.TEAM,
        raw_payload={},
    )
    assert record.deal.payoff_date == date(2027, 2, 28)  # clamped to a short month


def test_a_term_and_a_payoff_date_that_disagree_are_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="disagree"):
        TeamEntryForm(**payload(closing_date="2027-01-01", term_months=9, payoff_date="2027-08-01"))


def test_a_term_and_a_payoff_date_that_agree_are_fine() -> None:
    form = TeamEntryForm(
        **payload(closing_date="2027-01-01", term_months=7, payoff_date="2027-08-01")
    )
    assert form.effective_term_months == 7


def test_a_payoff_date_that_is_not_a_whole_number_of_months_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a whole number of months"):
        TeamEntryForm(
            **payload(term_months=None, closing_date="2027-01-01", payoff_date="2027-08-15")
        )


# --- what the form insists on --------------------------------------------------------------------


def test_the_form_marks_both_boxes_as_the_alternatives_they_are(client: TestClient) -> None:
    body = client.get("/queue/new").text
    assert 'name="term_months"' in body and 'name="payoff_date"' in body
    assert "Required, or a payoff date" in body
    assert "Required, or a term" in body


def test_the_form_refuses_a_deal_with_neither(client: TestClient) -> None:
    response = form_post(client, payload(term_months=None))
    assert response.status_code == 422
    assert "Term (months) is required" in response.text
    assert "Entering either solves the other" in response.text


def test_the_form_takes_the_payoff_date_instead(client: TestClient, db_session: Session) -> None:
    posted = form_post(
        client, payload(term_months=None, closing_date="2027-01-01", payoff_date="2027-11-01")
    )
    assert posted.status_code == 303, posted.text
    assert db_session.query(Deal).one().term_months == 10


def test_a_json_intake_may_still_arrive_without_one(
    client: TestClient, db_session: Session
) -> None:
    """The JSON body takes a partial intake; the form is what insists (SPEC §4.1)."""
    deal = stored(client, db_session, payload(term_months=None))
    assert deal.term_months is None
    assert deal.status is Status.NEEDS_INFO
    assert "deal.term" in deal.missing_fields


def test_the_edit_page_renders_the_term_editable(client: TestClient, db_session: Session) -> None:
    deal = stored(client, db_session, payload())
    body = client.get(f"/queue/deals/{deal.id}/intake").text
    box = body[body.index('id="term_months"') :][:200]
    assert "readonly" not in box
    assert 'value="9"' in box


# --- the deal page's override block ---------------------------------------------------------------


def test_the_override_block_sets_a_term_on_a_deal_that_has_none(
    client: TestClient, db_session: Session
) -> None:
    """The one place in the queue: Run underwrite posts no form of its own."""
    deal = stored(client, db_session, payload(term_months=None))
    save_overrides(db_session, deal.id, TeamOverrides(term_months=18), actor=ACTOR)
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.term_months == 18


def test_the_override_block_reprices_a_deal(client: TestClient, db_session: Session) -> None:
    deal = stored(client, db_session, payload())  # a 9-month term
    save_overrides(db_session, deal.id, TeamOverrides(term_months=12), actor=ACTOR)
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.term_months == 12


def test_the_override_block_takes_a_payoff_date_instead_of_a_term(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload())
    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(closing_date=date(2027, 1, 1), payoff_date=date(2027, 11, 1)),
        actor=ACTOR,
    )
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.term_months == 10


def test_the_override_block_refuses_a_term_and_a_date_that_disagree() -> None:
    with pytest.raises(ValidationError, match="disagree"):
        TeamOverrides(closing_date=date(2027, 1, 1), term_months=9, payoff_date=date(2027, 11, 1))


def test_the_deal_page_shows_the_term_in_deal_economics(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload())
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "<dt>Term (months)</dt>" in body
    assert "<dt>Payoff Date</dt>" in body
    assert 'name="term_months"' in body  # and the override block carries a box


def test_the_page_says_the_button_is_off_on_a_deal_with_no_term(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload(term_months=None))
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "Run underwrite is off until these are entered" in body
    assert "Term (months)" in body


# --- and what the database insists on ------------------------------------------------------------


def test_the_database_no_longer_pins_the_term_to_the_bucket(db_session: Session) -> None:
    """The constraint is gone with v0.3 (migration 0010): a repriced deal is a real row."""
    db_session.add(
        Deal(
            channel=Channel.TEAM,
            status=Status.NEW,
            missing_fields=[],
            term_bucket=TermBucket.M9,
            term_months=11,
        )
    )
    db_session.flush()
    assert db_session.query(Deal).one().term_months == 11


def test_a_term_with_no_bucket_is_accepted_by_the_database(db_session: Session) -> None:
    db_session.add(
        Deal(channel=Channel.TEAM, status=Status.NEEDS_INFO, missing_fields=[], term_months=9)
    )
    db_session.flush()
    assert db_session.query(Deal).one().term_bucket is None


def test_a_twelve_plus_bucket_takes_any_term(db_session: Session) -> None:
    """It names no number, so there is nothing for the constraint to compare against."""
    db_session.add(
        Deal(
            channel=Channel.TEAM,
            status=Status.NEW,
            missing_fields=[],
            term_bucket=TermBucket.M12_PLUS,
            term_months=18,
        )
    )
    db_session.flush()
    db_session.rollback()
