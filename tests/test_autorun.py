"""The screen and the underwrite run on their own: at intake, and after every team edit.

# SPEC §4.6, §7, §8, §11

Three triggers and one actor. A complete deal is run on the way in on every channel - the
public form and the team form here - and run again when the override block or Edit Intake
is saved; every row that writes is recorded against ``system``, because nobody pressed the
button. The underwrite stands down by name when the deal is short of a closing date or a
term, and the screen's own Decline stops it, with the deal left DECLINED as SPEC §4.6 says.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.ratelimit import LIMITER
from db.models import AuditLog, Deal, Screen, Underwrite
from schema.models import AuditAction, Status
from services import DEALS, auto_run, latest_screen, run_screen, screen_result
from tests.conftest import ACTOR, QueueClient, form_body, requires_db, store_deal
from tests.test_apply import VALID

pytestmark = requires_db


def count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def actors(session: Session, action: AuditAction) -> list[str]:
    return list(
        session.scalars(
            select(AuditLog.actor)
            .where(AuditLog.table_name == DEALS, AuditLog.action == action.value)
            .order_by(AuditLog.created_at)
        )
    )


def one_deal(session: Session) -> Deal:
    (deal,) = list(session.scalars(select(Deal)))
    return deal


def notice(response: Any) -> str:
    """The redirect's notice, as the page will print it."""
    return unquote(response.headers["location"].split("notice=", 1)[1])


# --- on creation, every channel ------------------------------------------------------------------


def test_a_web_submission_is_screened_and_priced_on_arrival(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    LIMITER.reset()
    assert anon_client.post("/apply", data=VALID, follow_redirects=False).status_code == 303
    deal = one_deal(db_session)
    assert deal.status is Status.UNDERWRITING
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 1
    assert actors(db_session, AuditAction.SCREEN_RUN) == ["system"]
    assert actors(db_session, AuditAction.UNDERWRITE_RUN) == ["system"]
    # ...and the page opens on the results, with the flags that say what they rest on
    body = client.get(f"/queue/deals/{deal.id}").text
    assert "Not analyzed yet." not in body and "No analysis yet." not in body
    assert "Verdict <strong>" in body
    assert "<strong>SCREEN_RUN</strong>" in body and ">system<" in body
    # ...with the flags that say what they rest on, on the stored row (SPEC §9)
    row = latest_screen(db_session, deal.id)
    assert row is not None
    codes = {flag.code.value for flag in screen_result(row).flags}
    assert {"BORROWER_SOURCED_VALUES", "COURT_RECORDS_NOT_CHECKED"} <= codes


def test_a_team_entry_is_screened_and_priced_on_arrival(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    response = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert response.status_code == 303
    assert notice(response).startswith("Deal created. Analysis recorded: CONDITIONAL at an IRR of")
    deal = one_deal(db_session)
    assert deal.status is Status.UNDERWRITING
    assert actors(db_session, AuditAction.SCREEN_RUN) == ["system"]
    assert actors(db_session, AuditAction.UNDERWRITE_RUN) == ["system"]
    # the intake itself is still the person's
    assert actors(db_session, AuditAction.INTAKE_CREATED) == ["sam@glenwood.example"]


def test_an_incomplete_intake_is_not_run(client: QueueClient, db_session: Session) -> None:
    partial = {"address": "12 Elm St, Denver, CO 80202", "borrower_name": "Ray Okafor"}
    response = client.post("/intake/team", json=partial)
    assert response.status_code == 201 and response.json()["status"] == "NEEDS_INFO"
    assert count(db_session, Screen) == 0
    assert actors(db_session, AuditAction.SCREEN_RUN) == []


def test_a_screen_that_declines_stops_the_underwrite_and_closes_the_deal(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    body = form_body(team_entry, estimated_sale_price_team="220000.00")  # 88.6% LTV
    response = client.post("/intake/team", data=body, follow_redirects=False)
    assert response.status_code == 303
    assert notice(response) == (
        "Deal created. Analysis recorded: DECLINE. The ledger did not run: the deal is DECLINED."
    )
    deal = one_deal(db_session)
    assert deal.status is Status.DECLINED
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 0


# --- on a team edit -------------------------------------------------------------------------------


def test_saving_the_override_block_re_runs_both(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    run_screen(db_session, deal_id, actor=ACTOR)
    db_session.commit()
    assert count(db_session, Screen) == 1

    response = client.post(
        f"/queue/deals/{deal_id}/overrides",
        data={
            "estimated_sale_price_team": "$210,000",
            "closing_date": "2027-02-01",
            "term_months": "6",
            "interest_rate": "13%",
            "court_records_status": "CLEAN",
            "court_records_as_of": "2026-10-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert notice(response).startswith("Inputs saved. Analysis recorded: GO at an IRR of")
    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None and deal.status is Status.UNDERWRITING
    assert count(db_session, Screen) == 2 and count(db_session, Underwrite) == 1
    assert actors(db_session, AuditAction.SCREEN_RUN) == [ACTOR, "system"]
    assert actors(db_session, AuditAction.UNDERWRITE_RUN) == ["system"]
    latest = db_session.scalars(select(Underwrite).order_by(Underwrite.created_at.desc())).first()
    assert latest is not None
    assert Decimal(latest.inputs["deal"]["estimated_sale_price"]) == Decimal("210000"), (
        "it ran on the save"
    )


def test_an_override_save_that_leaves_the_deal_unpriceable_says_so_and_keeps_the_save(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """The edit is applied; what could not run is in the notice (``services/autorun.py``)."""
    deal_id = deal_with_overrides.id
    response = client.post(
        f"/queue/deals/{deal_id}/overrides",
        data={"estimated_sale_price_team": "$210,000"},  # no term; the closing date defaults
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    said = notice(response)
    assert said.startswith("Inputs saved. Analysis recorded: GO. The ledger did not run")
    assert "deal.term_months" in said and "closing_date" not in said
    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None
    assert deal.term_months is None and deal.status is Status.SCREENED
    assert deal.closing_date is not None and "closing_date" in deal.defaulted_fields
    assert count(db_session, Underwrite) == 0


def test_an_override_save_on_a_declined_deal_runs_nothing(
    client: QueueClient, db_session: Session, declining_deal: Deal
) -> None:
    deal_id = declining_deal.id
    run_screen(db_session, deal_id, actor=ACTOR)
    db_session.commit()
    assert declining_deal.status is Status.DECLINED
    response = client.post(
        f"/queue/deals/{deal_id}/overrides",
        data={"estimated_sale_price_team": "$400,000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert notice(response) == "Inputs saved."
    assert count(db_session, Screen) == 1, "a closed deal is not re-run until it is re-opened"


def test_auto_run_on_a_needs_info_deal_runs_nothing(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, {**team_entry, "purchase_price": None})
    assert deal.status is Status.NEEDS_INFO
    assert auto_run(db_session, deal).ran is False
    assert count(db_session, Screen) == 0
