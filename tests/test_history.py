"""The History section: every version and run, who did it, and Restore.  # SPEC §5, §9.1, §11

Three append-only tables and the audit rows beside them, read back as one list
(``services/history.py``); and the one write the section offers, which saves an earlier intake
version as a new submission and runs the analysis again (``services/intake.restore_intake``).
Nothing is rewound: the version restored is still there afterwards, and so is the one it
replaced.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.intake_form import intake_form_values
from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite
from db.repository import create_deal_from_intake
from engine.version import ENGINE_VERSION
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import AuditAction, Channel, Status, Verdict
from services import DEALS, HistoryKind, deal_history, populate, record_audit
from tests.conftest import QueueClient, form_body, requires_db, store_deal
from tests.test_home import a_web_deal

pytestmark = requires_db

D = Decimal
SAM = "sam@glenwood.example"


def one_deal(session: Session) -> Deal:
    (deal,) = list(session.scalars(select(Deal)))
    return deal


def notice(response: Any) -> str:
    return unquote(response.headers["location"].split("notice=", 1)[1])


def audit(session: Session, deal: Deal, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.table_name == DEALS,
                AuditLog.row_id == str(deal.id),
                AuditLog.action == action.value,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def submissions(session: Session, deal: Deal) -> list[IntakeSubmission]:
    return list(
        session.scalars(
            select(IntakeSubmission)
            .where(IntakeSubmission.deal_id == deal.id)
            .order_by(IntakeSubmission.received_at, IntakeSubmission.id)
        )
    )


# --- the list -------------------------------------------------------------------------------------


def test_history_lists_every_version_and_run_with_who_and_which_engine(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303
    deal = one_deal(db_session)
    pressed = client.post(f"/queue/deals/{deal.id}/run", follow_redirects=False)
    assert pressed.status_code == 303, pressed.text

    history = deal_history(db_session, deal.id)
    assert [entry.kind for entry in history] == [
        HistoryKind.UNDERWRITE,  # the button, newest first
        HistoryKind.SCREEN,
        HistoryKind.UNDERWRITE,  # the automatic run on arrival
        HistoryKind.SCREEN,
        HistoryKind.INTAKE,
    ]
    assert [entry.actor for entry in history] == [SAM, SAM, "system", "system", SAM]
    assert [entry.engine_version for entry in history][:4] == [ENGINE_VERSION] * 4
    intake = history[-1]
    assert intake.engine_version is None and intake.note == "team created"
    assert intake.restorable
    assert history[1].verdict is Verdict.CONDITIONAL and history[1].irr is None
    assert history[0].irr is not None and history[0].verdict is None
    assert all(earlier.at <= later.at for later, earlier in zip(history, history[1:], strict=False))


def test_an_intake_row_written_before_submission_ids_still_finds_its_actor(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The audit row and the submission share a transaction's ``now()``; that is the join."""
    record = normalize(
        parse_team_form(TeamEntryForm(**team_entry)), Channel.TEAM, raw_payload=team_entry
    )
    deal = create_deal_from_intake(db_session, record)
    populate(deal)
    record_audit(
        db_session,
        actor="an older queue",
        action=AuditAction.INTAKE_CREATED,
        table_name=DEALS,
        row_id=deal.id,
        after={"channel": "TEAM"},  # no submission_id, as every row before 7a
    )
    db_session.commit()
    (entry,) = deal_history(db_session, deal.id)
    assert entry.kind is HistoryKind.INTAKE
    assert entry.actor == "an older queue" and entry.note == "team created"


def test_a_version_nobody_recorded_says_nobody(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)  # no audit row at all
    (entry,) = deal_history(db_session, deal.id)
    assert entry.actor is None and entry.note == "team"


# --- restore --------------------------------------------------------------------------------------


def test_restore_saves_the_old_version_as_a_new_submission_and_re_runs(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303
    deal = one_deal(db_session)
    edited = client.post(
        f"/queue/deals/{deal.id}/intake",
        data=form_body(team_entry, purchase_price="192500.00"),
        follow_redirects=False,
    )
    assert edited.status_code == 303
    db_session.expire_all()
    first, second = submissions(db_session, deal)
    assert db_session.get(Deal, deal.id).purchase_price == D("192500")  # type: ignore[union-attr]
    screens_before = db_session.scalar(select(func.count()).select_from(Screen))

    restored = client.post(
        f"/queue/deals/{deal.id}/intake/{first.id}/restore", follow_redirects=False
    )
    assert restored.status_code == 303, restored.text
    assert notice(restored).startswith("Intake restored. Analysis recorded: CONDITIONAL")

    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None and again.purchase_price == D("200000.00")
    rows = submissions(db_session, again)
    assert [row.id for row in rows[:2]] == [first.id, second.id], "nothing was rewound"
    assert len(rows) == 3
    assert rows[2].raw_payload == first.raw_payload and rows[2].channel is Channel.TEAM
    (row,) = audit(db_session, again, AuditAction.INTAKE_RESTORED)
    assert row.actor == SAM
    assert row.after is not None
    assert row.after["restored_from"] == str(first.id)
    assert row.after["submission_id"] == str(rows[2].id)
    assert row.before is not None
    assert D(row.after["purchase_price"]) == D(200000) and D(row.before["purchase_price"]) == D(
        192500
    )
    assert set(row.before) == {"purchase_price"}
    # ...and the analysis ran again on what was restored
    assert db_session.scalar(select(func.count()).select_from(Screen)) == screens_before + 1
    latest = db_session.scalars(select(Screen).order_by(Screen.created_at.desc())).first()
    assert latest is not None and D(latest.inputs["deal"]["purchase_price"]) == D("200000")
    history = deal_history(db_session, again.id)
    notes = [entry.note for entry in history if entry.kind is HistoryKind.INTAKE]
    assert notes == ["team restored", "team edited", "team created"]


def test_a_web_version_restores_through_the_web_parser(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    web = a_web_deal(anon_client, db_session)
    assert web.purchase_price == D("185000")
    (original,) = submissions(db_session, web)
    assert original.channel is Channel.WEB
    # the team corrects the price on the intake form, then thinks better of it
    body = {**intake_form_values(web), "purchase_price": "$190,000"}
    body = {name: value for name, value in body.items() if value}
    edited = client.post(f"/queue/deals/{web.id}/intake", data=body, follow_redirects=False)
    assert edited.status_code == 303, edited.text
    db_session.expire_all()
    assert db_session.get(Deal, web.id).purchase_price == D("190000")  # type: ignore[union-attr]

    restored = client.post(
        f"/queue/deals/{web.id}/intake/{original.id}/restore", follow_redirects=False
    )
    assert restored.status_code == 303, restored.text
    db_session.expire_all()
    again = db_session.get(Deal, web.id)
    assert again is not None
    assert again.purchase_price == D("185000")
    assert again.channel is Channel.WEB
    # the borrower's own figures were never the form's to touch, and are still there
    assert again.estimated_sale_price_borrower == D("290000")
    assert again.monthly_rent_borrower == D("1950")
    rows = submissions(db_session, again)
    assert rows[-1].channel is Channel.WEB and rows[-1].raw_payload == original.raw_payload
    assert rows[-1].raw_payload["language"] == "en"


def test_restore_is_refused_on_a_closed_deal_and_on_another_deals_version(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)
    (version,) = submissions(db_session, deal)
    foreign = client.post(f"/queue/deals/{deal.id}/intake/{uuid.uuid4()}/restore")
    assert foreign.status_code == 422
    assert "does not belong to this deal" in foreign.text

    deal.status = Status.DEAD
    db_session.commit()
    closed = client.post(f"/queue/deals/{deal.id}/intake/{version.id}/restore")
    assert closed.status_code == 409
    assert "DEAD" in closed.text
    db_session.expire_all()
    assert len(submissions(db_session, deal)) == 1
    assert audit(db_session, deal, AuditAction.INTAKE_RESTORED) == []
    assert db_session.scalar(select(func.count()).select_from(Underwrite)) == 0


def test_restore_on_a_deal_that_does_not_exist_goes_home(client: QueueClient) -> None:
    response = client.post(
        f"/queue/deals/{uuid.uuid4()}/intake/{uuid.uuid4()}/restore", follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/queue?")


# --- the section ----------------------------------------------------------------------------------


def test_the_history_section_is_collapsed_and_offers_restore_on_each_version(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)
    (version,) = submissions(db_session, deal)
    body = client.get(f"/queue/deals/{deal.id}").text
    assert '<details class="panel" id="history">' in body
    assert '<details class="panel" id="history" open>' not in body
    section = body[body.index('id="history"') :]
    assert "<h2>History" in section
    assert f'action="/queue/deals/{deal.id}/intake/{version.id}/restore"' in section
    assert ">Restore</button>" in section
    assert "<th>When</th><th>What</th><th>Who</th><th>Engine</th>" in section
    assert "<h3>Actions and notes</h3>" in section


def test_restore_is_off_on_the_page_where_an_edit_is_off(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)
    deal.status = Status.LOI_SENT
    db_session.commit()
    body = client.get(f"/queue/deals/{deal.id}").text
    section = body[body.index('id="history"') :]
    assert "disabled>Restore</button>" in section
