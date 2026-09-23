"""The term in months, and the bucket that seeds it.  # SPEC §8.1

`term_bucket` is the borrower's own answer to "how long do you need the loan?" It seeds
`term_months` at intake — every bucket but `12_PLUS` names a number — and it does not fix it.
The deal is priced on the team's term, typed in months or implied by a payoff date, and a
deal repriced to 7 months on a 6-month ask is a real thing rather than a row to reject.

That is the v0.3 change. Before it the two were pinned together by a check constraint, the
box was read-only, and the server re-derived the bucket's number over whatever came back.

`12_PLUS` names no number at all, so a deal on that bucket carries no term until somebody
sets one, and the readiness checklist and the underwrite both say so by name.
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
from intake.normalize import infer_term_months, normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import Channel, Status, TermBucket, months_for_bucket
from services import TeamOverrides, save_overrides
from tests.conftest import ACTOR, requires_db

pytestmark = requires_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def payload(**overrides: Any) -> dict[str, Any]:
    """The complete team entry (a 9-month bucket), with changes."""
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


# --- derived from the bucket ---------------------------------------------------------------------


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


def test_the_normalizer_fills_it_at_intake() -> None:
    record = normalize(parse_team_form(TeamEntryForm(**payload())), Channel.TEAM, raw_payload={})
    assert record.deal.term_bucket is TermBucket.M9
    assert record.deal.term_months == 9


def test_a_deal_entered_through_the_form_carries_it(
    client: TestClient, db_session: Session
) -> None:
    assert form_post(client, payload()).status_code == 303
    deal = db_session.query(Deal).one()
    assert deal.term_months == 9


def test_the_bucket_seeds_the_term_when_nobody_has_set_one() -> None:
    record = normalize(
        parse_team_form(TeamEntryForm(**payload(term_bucket="6", term_months=None))),
        Channel.TEAM,
        raw_payload={},
    )
    assert record.deal.term_months == 6


def test_a_term_the_team_typed_wins_over_the_bucket_it_came_from() -> None:
    """The bucket was the ask; the term is what the deal is priced on (SPEC §8.1)."""
    record = normalize(
        parse_team_form(TeamEntryForm(**payload(term_bucket="9", term_months=11))),
        Channel.TEAM,
        raw_payload={},
    )
    assert record.deal.term_months == 11


def test_a_payoff_date_sets_the_term_instead() -> None:
    form = TeamEntryForm(
        **payload(term_months=None, closing_date="2027-01-01", payoff_date="2027-08-01")
    )
    record = normalize(parse_team_form(form), Channel.TEAM, raw_payload={})
    assert record.deal.term_months == 7


def test_a_payoff_date_that_is_not_a_whole_number_of_months_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a whole number of months"):
        TeamEntryForm(
            **payload(term_months=None, closing_date="2027-01-01", payoff_date="2027-08-15")
        )


def test_a_term_with_no_bucket_behind_it_is_allowed_now() -> None:
    """A JSON intake may carry a term and no bucket; neither implies the other any more."""
    form = TeamEntryForm(**payload(term_bucket=None, term_months=9))
    assert form.effective_term_months == 9


def test_the_form_shows_the_derived_number_read_only(client: TestClient) -> None:
    """Shown rather than hidden - a reader wants the number - but not up for argument."""
    body = client.get("/queue/new").text
    assert 'name="term_months"' in body
    assert "Required for a 12+ term" in body


# --- entered on a 12+ bucket ---------------------------------------------------------------------


def test_the_form_asks_for_a_term_on_a_twelve_plus_bucket(client: TestClient) -> None:
    response = form_post(client, payload(term_bucket="12_PLUS"))
    assert response.status_code == 422
    assert "Term in months is required on a 12+ term" in response.text


def test_the_form_takes_one(client: TestClient, db_session: Session) -> None:
    posted = form_post(client, payload(term_bucket="12_PLUS", term_months="18"))
    assert posted.status_code == 303, posted.text
    deal = db_session.query(Deal).one()
    assert deal.term_bucket is TermBucket.M12_PLUS and deal.term_months == 18


def test_a_json_intake_may_still_arrive_without_one(
    client: TestClient, db_session: Session
) -> None:
    """The borrower channel says "a year or more" and nobody has negotiated a number yet."""
    deal = stored(client, db_session, payload(term_bucket="12_PLUS"))
    assert deal.term_months is None
    assert deal.status is Status.NEW, "the term is not part of the minimum viable intake"


def test_the_edit_page_renders_the_term_editable_on_a_twelve_plus_deal(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload(term_bucket="12_PLUS"))
    body = client.get(f"/queue/deals/{deal.id}/intake").text
    box = body[body.index('id="term_months"') :][:200]
    assert "readonly" not in box


def test_the_edit_page_renders_the_term_editable_whatever_the_bucket_says(
    client: TestClient, db_session: Session
) -> None:
    """Editable everywhere now: the bucket seeds it, the team owns it (SPEC §8.1)."""
    deal = stored(client, db_session, payload())
    body = client.get(f"/queue/deals/{deal.id}/intake").text
    box = body[body.index('id="term_months"') :][:200]
    assert "readonly" not in box
    assert 'value="9"' in box


# --- the deal page's override block ---------------------------------------------------------------


def twelve_plus(client: TestClient, session: Session) -> Deal:
    return stored(client, session, payload(term_bucket="12_PLUS"))


def test_the_override_block_is_where_a_twelve_plus_term_gets_set(
    client: TestClient, db_session: Session
) -> None:
    """The one place in the queue: Run underwrite posts no form of its own."""
    deal = twelve_plus(client, db_session)
    save_overrides(db_session, deal.id, TeamOverrides(term_months=18), actor=ACTOR)
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.term_months == 18


def test_the_override_block_reprices_a_deal_off_its_bucket(
    client: TestClient, db_session: Session
) -> None:
    """A 9-month ask repriced to 12 is a real thing, and the column follows the team."""
    deal = stored(client, db_session, payload())  # a 9-month bucket
    save_overrides(db_session, deal.id, TeamOverrides(term_months=12), actor=ACTOR)
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.term_months == 12
    assert again.term_bucket is TermBucket.M9  # the ask is still on the record


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


def test_the_deal_page_shows_the_term_beside_its_bucket(
    client: TestClient, db_session: Session
) -> None:
    deal = stored(client, db_session, payload())
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "the borrower asked for" in body
    assert "<dt>Term (months)</dt>" in body
    assert 'name="term_months"' in body  # and the override block carries a box


def test_the_page_says_the_button_is_off_on_a_twelve_plus_deal_with_no_term(
    client: TestClient, db_session: Session
) -> None:
    deal = twelve_plus(client, db_session)
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
