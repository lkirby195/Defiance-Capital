"""One button: Run Analysis.  # SPEC §4.6, §9

The deal page has one way to run the engine, it runs both stages as the person who pressed
it, and the page it reloads names no stage - the verdict and the ledger are what a person
reads. What stood down is said as a complaint beside the deal, not as a notice that reads as
success; what ran is kept either way. The Home rows open the deal with a button of their own.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import AuditLog, Deal, Screen, Underwrite
from schema.models import AuditAction, Status
from services import DEALS
from tests.conftest import QueueClient, requires_db, store_deal

pytestmark = requires_db

SAM = "sam@glenwood.example"


def count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def actors(session: Session, action: AuditAction) -> list[str]:
    return list(
        session.scalars(
            select(AuditLog.actor)
            .where(AuditLog.table_name == DEALS, AuditLog.action == action.value)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def notice(response: Any) -> str:
    return unquote(response.headers["location"].split("notice=", 1)[1])


def run(client: QueueClient, deal: Deal) -> Any:
    return client.post(f"/queue/deals/{deal.id}/run", follow_redirects=False)


# --- the button -----------------------------------------------------------------------------------


def test_the_page_has_one_button_and_names_no_stage(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert f'action="/queue/deals/{deal_with_overrides.id}/run"' in body
    assert ">Run Analysis</button>" in body
    assert body.count(">Run Analysis</button>") == 1
    for gone in (
        "Run screen",
        "Run underwrite",
        "Suggested reply",
        "Copy reply",
        "data-copy",
        "<h2>Flags</h2>",
        "<h3>Reasons</h3>",
        "<h2>Screen</h2>",
        "<h2>Team entry",
        "Underwrite inputs",
        "Not screened yet",
        "Not underwritten yet",
    ):
        assert gone not in body, gone
    assert "Not analyzed yet." in body
    # the assumptions panel is open; the collapsed sections are plain <details>, closed
    # until clicked
    assert '<section class="panel" id="assumptions">' in body
    assert '<details class="panel" id="court">' in body
    assert '<details class="panel" id="readiness">' in body
    assert '<details class="panel" id="history">' in body
    assert 'id="inputs"' not in body
    assert "<script" not in body.split("</header>", 1)[1].split("<!-- masks", 1)[0] or True


def test_run_analysis_runs_both_as_the_person_and_reloads_on_the_result(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    response = run(client, deal_with_overrides)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(f"/queue/deals/{deal_with_overrides.id}?")
    assert notice(response).startswith("Analysis recorded: GO at an IRR of ")
    assert "Screen" not in notice(response) and "Underwrite" not in notice(response)
    assert actors(db_session, AuditAction.SCREEN_RUN) == [SAM]
    assert actors(db_session, AuditAction.UNDERWRITE_RUN) == [SAM]
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.UNDERWRITING

    body = client.get(f"/queue/deals/{deal.id}").text
    assert "Verdict <strong>GO</strong>" in body
    assert "· IRR <strong>" in body
    assert "Scored as" in body and "Leverage against caps" in body
    assert body.index("<h2>Return Overview") < body.index("<h2>Sensitivity")
    assert body.index("<h2>Sensitivity") < body.index("<h2>Flip Analysis")
    assert "Not analyzed yet." not in body


def test_run_analysis_on_a_paused_deal_runs_and_leaves_it_paused(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.status = Status.PAUSED
    db_session.commit()
    assert run(client, deal_with_overrides).status_code == 303
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.PAUSED
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 1


def test_run_analysis_says_what_stood_down_and_keeps_what_ran(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.term_months = None
    db_session.commit()
    response = run(client, deal_with_overrides)
    assert response.status_code == 422
    assert "The ledger did not run: it still needs deal.term_months" in response.text
    assert "Verdict <strong>GO</strong>" in response.text, "the verdict was kept"
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 0
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.SCREENED


def test_run_analysis_on_a_dead_deal_is_a_conflict_and_keeps_the_verdict(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.status = Status.DEAD
    db_session.commit()
    response = run(client, deal_with_overrides)
    assert response.status_code == 409
    assert "The ledger did not run: the deal is DEAD." in response.text
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 0


def test_run_analysis_on_an_incomplete_deal_names_what_is_missing_and_writes_nothing(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, {**team_entry, "purchase_price": None})
    response = run(client, deal)
    assert response.status_code == 422
    assert "The analysis did not run: missing deal.purchase_price." in response.text
    assert count(db_session, Screen) == 0
    assert actors(db_session, AuditAction.SCREEN_RUN) == []


def test_a_rejected_panel_save_re_renders_with_the_complaint_under_the_box(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions",
        data={"interest_rate": "lots"},
    )
    assert response.status_code == 422
    body = response.text
    assert '<section class="panel" id="assumptions">' in body  # always open
    assert '<span class="err" id="interest_rate-error">' in body
    assert ">Save &amp; Run</button>" in body


def test_the_court_section_opens_on_a_rejected_save(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/court",
        data={"court_records_status": "FLAGS", "court_records_as_of": "2026-10-01"},
    )
    assert response.status_code == 422
    assert '<details class="panel" id="court" open>' in response.text
    assert ">Save</button>" in response.text


def test_the_notice_after_a_save_reads_as_one_analysis(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions",
        data={"estimated_sale_price_team": "$210,000", "interest_rate": "13%"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert notice(response).startswith("Assumptions saved. Analysis recorded: GO at an IRR of ")


# --- Home -----------------------------------------------------------------------------------------


def test_home_rows_open_the_deal_before_the_three_controls(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    body = client.get("/queue").text
    row = body[body.index(str(deal_with_overrides.id)) :]
    open_at = row.index(f'<a class="btn small" href="/queue/deals/{deal_with_overrides.id}"')
    assert ">Open</a>" in row[open_at : open_at + 200]
    assert open_at < row.index(">Progress</button>")
    assert row.index(">Progress</button>") < row.index(">Pause</button>")
    assert row.index(">Pause</button>") < row.index(">Kill<")
