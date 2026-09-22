"""Editing the intake on a deal that is already in the queue.  # SPEC §4.1, §4.5, §5

The team takes a call with half a deal on it, and the rest arrives later. Until now the only
way to record that was a note, which nothing reads: the deal sat in NEEDS_INFO with a
``missing_fields`` list that could never empty, and could not be priced.

What an edit is and is not, which is most of what is tested here:

* it is a **new submission**, not a correction of the old one - ``intake_submissions`` is
  immutable (SPEC §5) and every version posted is still on the deal afterwards;
* it re-runs the normalizer, so ``missing_fields`` is recomputed and a deal that is finally
  complete leaves NEEDS_INFO;
* it does **not** re-screen. The deal page says the screen is stale and a person decides
  (SPEC §7); a deal that quietly re-priced itself on an edit would be worse than a stale one
  that says so;
* it is refused on a deal that is closed or past LOI, where the numbers on file have to keep
  agreeing with a document that went out.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import AuditLog, Borrower, Deal, IntakeSubmission, Screen
from schema.models import AuditAction, Status, TermBucket, Tranche
from tests.conftest import QueueClient, requires_db

pytestmark = requires_db

PARTIAL = {"address": "12 Elm St, Denver, CO 80202", "borrower_name": "Ray Okafor"}


def form_body(payload: dict[str, Any], **changes: str) -> dict[str, str]:
    """A team-entry payload as the form posts it: every value a string."""
    body = {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in payload.items()
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value != ""}


def edit(client: QueueClient, deal_id: Any, body: dict[str, str]) -> Any:
    return client.post(f"/queue/deals/{deal_id}/intake", data=body, follow_redirects=False)


def trail(session: Session, action: AuditAction) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.action == action.value)))


def needs_info_deal(client: QueueClient) -> str:
    """A deal entered through a channel that only had part of an intake."""
    created = client.post("/intake/team", json=PARTIAL)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "NEEDS_INFO"
    deal_id: str = created.json()["id"]
    return deal_id


# --- the page -------------------------------------------------------------------------------------


def test_the_deal_page_offers_the_edit(client: QueueClient, stored_deal: Deal) -> None:
    body = client.get(f"/queue/deals/{stored_deal.id}").text
    assert f'href="/queue/deals/{stored_deal.id}/intake"' in body
    assert "Edit intake" in body


def test_the_edit_page_is_the_form_filled_in(client: QueueClient, stored_deal: Deal) -> None:
    response = client.get(f"/queue/deals/{stored_deal.id}/intake")
    assert response.status_code == 200
    body = response.text
    assert f'action="/queue/deals/{stored_deal.id}/intake"' in body
    assert 'value="Dana Whitfield"' in body
    assert 'value="185000.00"' in body
    assert 'value="T2" selected' in body
    assert "Save intake" in body


# --- what a save does -----------------------------------------------------------------------------


def test_an_edit_keeps_the_old_submission_and_adds_a_new_one(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """``intake_submissions`` is immutable: an edit appends, it does not rewrite."""
    deal_id = stored_deal.id
    response = edit(client, deal_id, form_body(team_entry, purchase_price="192500.00"))
    assert response.status_code == 303, response.text

    db_session.expire_all()
    rows = list(
        db_session.scalars(
            select(IntakeSubmission)
            .where(IntakeSubmission.deal_id == deal_id)
            .order_by(IntakeSubmission.received_at)
        )
    )
    assert len(rows) == 2
    assert rows[0].raw_payload["purchase_price"] == "185000.00"
    assert rows[1].raw_payload["purchase_price"] == "192500.00"
    deal = db_session.get(Deal, deal_id)
    assert deal is not None and str(deal.purchase_price) == "192500.00"
    assert deal.id == deal_id, "an edit is not a new deal"


def test_a_completed_intake_leaves_needs_info(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """NEEDS_INFO -> NEW, and ``missing_fields`` empties.  # SPEC §4.1"""
    deal_id = needs_info_deal(client)
    db_session.expire_all()
    before = db_session.get(Deal, deal_id)
    assert before is not None and before.missing_fields, "the fixture should start incomplete"

    assert edit(client, deal_id, form_body(team_entry)).status_code == 303

    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None
    assert deal.status is Status.NEW
    assert deal.missing_fields == []
    assert deal.credit_range_self_reported is Tranche.T2
    assert deal.term_bucket is TermBucket.M9


def test_an_edit_does_not_drag_a_screened_deal_backwards(
    client: QueueClient,
    db_session: Session,
    team_entry_with_overrides: dict[str, Any],
    deal_with_overrides: Deal,
) -> None:
    """Only NEEDS_INFO moves. A SCREENED deal stays SCREENED until somebody re-screens it."""
    deal_id = deal_with_overrides.id
    assert client.post(f"/queue/deals/{deal_id}/screen", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(Deal, deal_id).status is not Status.NEEDS_INFO  # type: ignore[union-attr]
    before = db_session.get(Deal, deal_id)
    assert before is not None
    was = before.status

    assert (
        edit(
            client, deal_id, form_body(team_entry_with_overrides, loan_requested="151000.00")
        ).status_code
        == 303
    )

    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None and deal.status is was
    assert db_session.scalar(select(func.count()).select_from(Screen)) == 1, "it re-screened"


def test_an_edit_records_who_did_it_and_what_moved(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """The audit row is the answer to "who changed this number"; it names only what changed."""
    edit(
        client,
        stored_deal.id,
        form_body(team_entry, loan_requested="175000.00", borrower_email="dana.w@example.com"),
    )

    db_session.expire_all()
    [row] = trail(db_session, AuditAction.INTAKE_EDITED)
    assert row.actor == "sam@glenwood.example"
    assert row.table_name == "deals" and row.row_id == str(stored_deal.id)
    assert row.after == {"loan_requested": "175000.00", "borrower.email": "dana.w@example.com"}
    assert row.before == {"loan_requested": "190000.00", "borrower.email": "dana@example.com"}


def test_completing_an_intake_records_the_status_move(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal_id = needs_info_deal(client)
    edit(client, deal_id, form_body(team_entry))

    db_session.expire_all()
    [row] = trail(db_session, AuditAction.INTAKE_EDITED)
    assert row.before is not None and row.before["status"] == "NEEDS_INFO"
    assert row.after is not None and row.after["status"] == "NEW"
    assert row.after["missing_fields"] == []


def test_correcting_the_name_reaches_the_page(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """The borrower row is the person behind the phone number, and a typed name set it."""
    edit(client, stored_deal.id, form_body(team_entry, borrower_name="Dana Whitfield-Cole"))

    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Borrower)) == 1
    body = client.get(f"/queue/deals/{stored_deal.id}").text
    assert "Dana Whitfield-Cole" in body


def test_a_new_phone_is_a_different_borrower(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """The phone is the match key (SPEC §5), so changing it re-points the deal."""
    was = stored_deal.borrower_id
    edit(client, stored_deal.id, form_body(team_entry, borrower_phone="(918) 555-0199"))

    db_session.expire_all()
    deal = db_session.get(Deal, stored_deal.id)
    assert deal is not None and deal.borrower is not None
    assert deal.borrower_id != was
    assert deal.borrower.phone == "+19185550199"
    assert db_session.scalar(select(func.count()).select_from(Borrower)) == 2


# --- the stale note -------------------------------------------------------------------------------


def test_the_screen_is_called_stale_only_after_the_intake_moves(
    client: QueueClient,
    db_session: Session,
    team_entry_with_overrides: dict[str, Any],
    deal_with_overrides: Deal,
) -> None:
    deal_id = deal_with_overrides.id
    assert "changed after this screen ran" not in client.get(f"/queue/deals/{deal_id}").text

    client.post(f"/queue/deals/{deal_id}/screen", follow_redirects=False)
    fresh = client.get(f"/queue/deals/{deal_id}").text
    assert "changed after this screen ran" not in fresh, "a screen just run is not stale"

    edit(client, deal_id, form_body(team_entry_with_overrides, rehab_budget="51000.00"))

    stale = client.get(f"/queue/deals/{deal_id}").text
    assert "changed after this screen ran" in stale
    assert "not automatic" in stale

    client.post(f"/queue/deals/{deal_id}/screen", follow_redirects=False)
    assert "changed after this screen ran" not in client.get(f"/queue/deals/{deal_id}").text
    del db_session


def test_an_unscreened_deal_is_never_stale(
    client: QueueClient, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """There is nothing to be out of date with."""
    edit(client, stored_deal.id, form_body(team_entry, county="Creek"))
    assert "changed after this screen ran" not in client.get(f"/queue/deals/{stored_deal.id}").text


# --- what an edit is refused on -------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [Status.DECLINED, Status.DEAD, Status.LOI_SENT, Status.HANDED_OFF]
)
def test_a_closed_or_committed_deal_is_not_edited(
    client: QueueClient,
    db_session: Session,
    team_entry: dict[str, Any],
    stored_deal: Deal,
    status: Status,
) -> None:
    stored_deal.status = status
    db_session.commit()

    page = client.get(f"/queue/deals/{stored_deal.id}/intake", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith(f"/queue/deals/{stored_deal.id}")

    posted = edit(client, stored_deal.id, form_body(team_entry, loan_requested="1000.00"))
    assert posted.status_code == 409
    assert status.value in posted.text

    db_session.expire_all()
    deal = db_session.get(Deal, stored_deal.id)
    assert deal is not None
    assert str(deal.loan_requested) == "190000.00", "the refused edit wrote a column anyway"
    assert len(list(db_session.scalars(select(IntakeSubmission)))) == 1
    assert trail(db_session, AuditAction.INTAKE_EDITED) == []
    assert client.get(f"/queue/deals/{stored_deal.id}").text.count("Edit intake") == 0


def test_the_edit_form_asks_for_the_same_required_boxes(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    """Server-side, by name, and nothing is written."""
    body = form_body(team_entry)
    del body["term_bucket"]

    response = edit(client, stored_deal.id, body)

    assert response.status_code == 422
    assert "Term is required." in response.text
    db_session.expire_all()
    assert len(list(db_session.scalars(select(IntakeSubmission)))) == 1
    assert trail(db_session, AuditAction.INTAKE_EDITED) == []


def test_an_edit_on_a_deal_that_does_not_exist_goes_back_to_the_queue(
    client: QueueClient, team_entry: dict[str, Any]
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    page = client.get(f"/queue/deals/{missing}/intake", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/queue?")
    posted = edit(client, missing, form_body(team_entry))
    assert posted.status_code == 303 and posted.headers["location"].startswith("/queue?")
