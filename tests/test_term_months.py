"""The term the deal is priced on: months in, a month-end payoff date out.  # SPEC §8.1

One term, one box. The team form takes a term in months and requires it; the payoff date is
derived - the last day of the month that is the closing month plus the term - and is shown
read-only on the deal page. No form takes a payoff date, so no form produces a stub.

`term_bucket` is the borrower's own answer to "how long do you need the loan?", asked by the
borrower channels (SPEC §4.1). It seeds `term_months` at intake — `12_PLUS` seeds 12, the
floor of the ask — and it does not fix it: a deal repriced to 7 months on a 6-month ask is a
real thing rather than a row to reject. **The team form does not ask it at all**: a person
with the whole deal in front of them knows the term, and a bucket beside it would be a second
number to keep in step with the first.

`deal.term` on the missing-fields list is one row for both columns: a deal that has a bucket or
a term has answered the question, and one with neither has not.
"""

from __future__ import annotations

import json
import re
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
from schema.dates import Term
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


def test_twelve_plus_seeds_twelve_and_keeps_what_the_team_typed() -> None:
    """The floor of the ask, on every channel (SPEC §4.1); the team's own number wins."""
    assert months_for_bucket(TermBucket.M12_PLUS) == 12
    assert infer_term_months(TermBucket.M12_PLUS, 18) == 18
    assert infer_term_months(TermBucket.M12_PLUS, None) == 12
    assert infer_term_months(None, None) is None


def test_the_team_form_does_not_ask_the_bucket_at_all() -> None:
    """SPEC §8.1: the borrower channels ask it; a person with the deal in front of them
    knows the term, and a second number to keep in step is a second number to get wrong."""
    with pytest.raises(ValidationError, match="term_bucket"):
        TeamEntryForm(**payload(term_bucket="9"))


def test_the_bucket_still_seeds_the_term_on_a_borrower_channel_intake() -> None:
    seeded = normalize(parsed(DealInfo(term_bucket=TermBucket.M6)), Channel.SMS, raw_payload={})
    assert seeded.deal.term_months == 6
    over_a_year = normalize(
        parsed(DealInfo(term_bucket=TermBucket.M12_PLUS)), Channel.SMS, raw_payload={}
    )
    assert over_a_year.deal.term_months == 12  # the seed; the bucket stays beside it
    assert over_a_year.deal.term_bucket is TermBucket.M12_PLUS
    assert "deal.term" not in seeded.missing_fields

    own = normalize(
        parsed(DealInfo(term_bucket=TermBucket.M9, term_months=11)), Channel.SMS, raw_payload={}
    )
    assert own.deal.term_months == 11  # the team's own wins; the ask stays on the record
    assert own.deal.term_bucket is TermBucket.M9


# --- one term, one box (SPEC §8.1) ---------------------------------------------------------------


def test_the_normalizer_takes_the_term_off_the_form() -> None:
    record = normalize(parse_team_form(TeamEntryForm(**payload())), Channel.TEAM, raw_payload={})
    assert record.deal.term_bucket is None
    assert record.deal.term_months == 9
    assert record.deal.term_stub_days is None


def test_a_deal_entered_through_the_form_carries_it(
    client: TestClient, db_session: Session
) -> None:
    assert form_post(client, payload()).status_code == 303
    deal = db_session.query(Deal).one()
    assert deal.term_months == 9


def test_the_form_model_takes_no_payoff_date() -> None:
    """No form path produces a stub (SPEC §8.1): the payoff date is derived, not entered."""
    with pytest.raises(ValidationError, match="payoff_date"):
        TeamEntryForm(**payload(payoff_date="2027-08-01"))
    assert TeamEntryForm(**payload()).effective_term == Term(9, 0)


def test_the_payoff_date_is_the_end_of_the_month_the_term_lands_on() -> None:
    """A one-month term closing 31 January pays off 28 February; so does one closing the 1st."""
    for day in ("2027-01-01", "2027-01-15", "2027-01-31"):
        record = normalize(
            parse_team_form(TeamEntryForm(**payload(closing_date=day, term_months=1))),
            Channel.TEAM,
            raw_payload={},
        )
        assert record.deal.payoff_date == date(2027, 2, 28)


# --- what the form insists on --------------------------------------------------------------------


def test_the_form_marks_the_term_required_and_has_no_payoff_box(client: TestClient) -> None:
    body = client.get("/queue/new").text
    assert 'name="term_months"' in body
    assert 'name="payoff_date"' not in body
    box = body[body.index('id="term_months"') :][:200]
    assert re.search(r"\srequired[\s>]", box)


def test_the_form_refuses_a_deal_with_no_term(client: TestClient) -> None:
    response = form_post(client, payload(term_months=None))
    assert response.status_code == 422
    assert "Term (months) is required." in response.text


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
    """The one place in the queue: Run Analysis posts no form of its own."""
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
    assert again.term_stub_days is None  # a term in months stores no stub


def test_the_override_block_takes_no_payoff_date() -> None:
    """The payoff date is read-only on the deal page; the model refuses the box by name."""
    with pytest.raises(ValidationError, match="payoff_date"):
        TeamOverrides(closing_date=date(2027, 1, 1), payoff_date=date(2027, 11, 1))  # type: ignore[call-arg]


def test_a_term_typed_in_months_clears_a_stub_the_deal_carried(
    client: TestClient, db_session: Session
) -> None:
    """A stub could only have come from a script or an older row; a term in months has none."""
    deal = stored(client, db_session, payload())
    deal.term_stub_days = 11
    db_session.commit()
    save_overrides(db_session, deal.id, TeamOverrides(term_months=9), actor=ACTOR)
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and (again.term_months, again.term_stub_days) == (9, None)


def test_the_deal_page_shows_the_term_and_the_derived_payoff_date(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload(closing_date="2026-10-15", term_months=9))
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "<dt>Term" in body
    assert "9 month(s)" in body
    payoff = re.search(r"<dt>Payoff Date.*?</dt>\s*<dd>([^<]*)</dd>", body, re.S)
    assert payoff is not None and payoff.group(1).strip() == "2027-07-31"
    # the term is the intake form's box, not the deal page's (SPEC §8.1)
    assert 'name="term_months"' not in body
    form = client.get(f"/queue/deals/{deal.id}/intake").text
    assert re.search(r'id="term_months"[^>]*value="9"', form)


def test_the_page_says_the_button_is_off_on_a_deal_with_no_term(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload(term_months=None))
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "The ledger is off until these are entered" in body
    assert "Term" in body


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
