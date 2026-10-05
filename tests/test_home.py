"""The Home page: its four sections, their order, and the three controls on every row.

# SPEC §4.6, §9.1, §12

The membership rules and the ordering are tested against ``home_view`` directly, because
they are decisions and not markup. The controls are tested through the routes, as a browser
posts them, and each one is held to the ``audit_log`` row it writes - an action that moved a
deal and recorded nothing is the failure that table exists to prevent. The pages are tested
by rendering them: every fixture deal is put through Home, screened, underwritten where it
can be, and the deal page asked for - so a template that reaches for a field the engine
renamed fails here rather than in front of someone.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.ratelimit import LIMITER
from config.config import Config
from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite
from schema.models import AuditAction, Channel, Status
from services import (
    DEALS,
    ActionNotAllowed,
    Section,
    TeamOverrides,
    add_note,
    advance_to_review,
    auto_run,
    home_view,
    last_activity,
    latest_screen,
    latest_underwrite,
    mark_dead,
    pause,
    paused_from,
    progress,
    resume,
    run_screen,
    run_underwrite,
    save_overrides,
    section_of,
    underwrite_result,
)
from services.home import RunSummary
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, DECLINING_SALE_PRICE, QueueClient, requires_db, store_deal
from tests.test_apply import VALID

pytestmark = requires_db

CONFIG = Config.load()
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
TEAM_ENTRY = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def a_deal(session: Session, **overrides: Any) -> Deal:
    """One complete deal, with whatever the caller wants different about it."""
    payload = json.loads(TEAM_ENTRY.read_text(encoding="utf-8"))
    payload.update(overrides)
    return store_deal(session, payload)


def a_web_deal(anon_client: QueueClient, session: Session, **changes: str) -> Deal:
    """A deal the borrower typed in on the public form (channel WEB)."""
    LIMITER.reset()
    before = {deal.id for deal in session.scalars(select(Deal))}
    body = {**VALID, **changes}
    assert anon_client.post("/apply", data=body, follow_redirects=False).status_code == 303
    session.expire_all()
    [deal] = [deal for deal in session.scalars(select(Deal)) if deal.id not in before]
    return deal


def deal_that_screens_go(session: Session) -> Deal:
    """A deal with the team's valuation and court search on it, so every run succeeds."""
    fixture = json.loads((FIXTURE_DIR / "go_team_overrides_tulsa.json").read_text(encoding="utf-8"))
    payload = dict(fixture["team_entry"])
    payload["address"] = "7 Cedar St, Tulsa, OK 74104"
    payload["borrower_phone"] = "(918) 555-0199"
    return store_deal(session, payload)


def at(session: Session, deal: Deal, status: Status) -> Deal:
    deal.status = status
    session.flush()
    session.commit()
    return deal


def ids(view: Any, key: Section) -> list[Any]:
    return [entry.deal.id for entry in view.section(key).entries]


def rows(session: Session, deal: Deal, action: AuditAction) -> list[AuditLog]:
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


def post(client: TestClient, deal: Deal, action: str, **data: Any) -> Any:
    return client.post(f"/queue/deals/{deal.id}/{action}", data=data, follow_redirects=False)


# --- the four sections and who is in them (SPEC §9.1) --------------------------------------------


def test_the_four_sections_are_always_there_in_order(db_session: Session) -> None:
    view = home_view(db_session)
    assert [section.key for section in view.sections] == [
        Section.BORROWER,
        Section.DCF,
        Section.PAUSED,
        Section.DEAD,
    ]
    assert [section.title for section in view.sections] == [
        "Borrower Submissions",
        "DCF New Deals",
        "On Pause",
        "Dead",
    ]
    assert all(section.entries == [] for section in view.sections)
    assert view.total == 0


def test_a_borrower_submission_and_a_team_entry_sit_in_the_first_two(
    anon_client: QueueClient, db_session: Session
) -> None:
    web = a_web_deal(anon_client, db_session)
    team = a_deal(db_session)
    view = home_view(db_session)
    assert ids(view, Section.BORROWER) == [web.id]
    assert ids(view, Section.DCF) == [team.id]
    assert ids(view, Section.PAUSED) == [] and ids(view, Section.DEAD) == []
    assert view.total == 2


@pytest.mark.parametrize(
    ("status", "key"),
    [
        (Status.NEW, Section.DCF),
        (Status.NEEDS_INFO, Section.DCF),
        (Status.SCREENED, Section.DCF),
        (Status.IN_REVIEW, Section.DCF),
        (Status.UNDERWRITING, Section.DCF),
        (Status.LOI_SENT, Section.DCF),
        (Status.HANDED_OFF, Section.DCF),
        (Status.PAUSED, Section.PAUSED),
        (Status.DECLINED, Section.DEAD),
        (Status.DEAD, Section.DEAD),
    ],
)
def test_every_status_lands_in_exactly_one_section(
    db_session: Session, status: Status, key: Section
) -> None:
    """The status decides first; the channel only splits the live ones."""
    deal = at(db_session, a_deal(db_session), status)
    assert section_of(deal) is key
    view = home_view(db_session)
    assert [k for k in Section if deal.id in ids(view, k)] == [key]


@pytest.mark.parametrize("status", [Status.PAUSED, Status.DECLINED, Status.DEAD])
def test_a_paused_or_dead_web_deal_leaves_the_borrower_section(
    anon_client: QueueClient, db_session: Session, status: Status
) -> None:
    """The first two sections exclude the paused and the dead, whichever channel."""
    web = at(db_session, a_web_deal(anon_client, db_session), status)
    assert web.channel is Channel.WEB
    view = home_view(db_session)
    assert ids(view, Section.BORROWER) == []
    expected = Section.PAUSED if status is Status.PAUSED else Section.DEAD
    assert ids(view, expected) == [web.id]


def test_a_channel_that_is_not_the_public_form_is_a_dcf_deal(db_session: Session) -> None:
    """Only WEB is the borrower's own; everything else the team entered (SPEC §13)."""
    deal = a_deal(db_session)
    deal.channel = Channel.SMS
    db_session.commit()
    assert section_of(deal) is Section.DCF


# --- ordering: most recently touched first, within each section ----------------------------------


def quiet_since(session: Session, deal: Deal, when: datetime) -> Deal:
    """Backdate everything that has ever happened to the deal to one moment."""
    deal.created_at = when
    session.execute(
        update(IntakeSubmission).where(IntakeSubmission.deal_id == deal.id).values(received_at=when)
    )
    session.execute(update(Screen).where(Screen.deal_id == deal.id).values(created_at=when))
    session.execute(update(Underwrite).where(Underwrite.deal_id == deal.id).values(created_at=when))
    session.execute(
        update(AuditLog)
        .where(AuditLog.table_name == DEALS, AuditLog.row_id == str(deal.id))
        .values(created_at=when)
    )
    session.commit()
    return deal


def test_a_section_is_ordered_by_last_activity_not_by_arrival(db_session: Session) -> None:
    now = datetime.now(UTC)
    oldest, middle, newest = (
        quiet_since(db_session, a_deal(db_session, address=f"{n} Elm St, Tulsa OK"), now - gap)
        for n, gap in enumerate((timedelta(days=3), timedelta(days=2), timedelta(days=1)))
    )
    assert ids(home_view(db_session), Section.DCF) == [newest.id, middle.id, oldest.id]

    add_note(db_session, oldest.id, actor=ACTOR, note="Borrower called back")
    db_session.commit()
    assert ids(home_view(db_session), Section.DCF) == [oldest.id, newest.id, middle.id]


@pytest.mark.parametrize("event", ["note", "action", "screen", "underwrite", "submission"])
def test_every_kind_of_activity_moves_a_deal_up(db_session: Session, event: str) -> None:
    now = datetime.now(UTC)
    stale = quiet_since(db_session, deal_that_screens_go(db_session), now - timedelta(days=5))
    fresh = quiet_since(
        db_session, a_deal(db_session, address="9 Ash St, Tulsa OK"), now - timedelta(hours=1)
    )
    assert ids(home_view(db_session), Section.DCF) == [fresh.id, stale.id]

    if event == "note":
        add_note(db_session, stale.id, actor=ACTOR, note="left a voicemail")
    elif event == "action":
        at(db_session, stale, Status.SCREENED)
        advance_to_review(db_session, stale.id, actor=ACTOR)
    elif event == "screen":
        run_screen(db_session, stale.id, CONFIG, actor=ACTOR)
    elif event == "underwrite":
        run_underwrite(db_session, stale.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    else:
        db_session.add(
            IntakeSubmission(deal_id=stale.id, channel=Channel.TEAM, raw_payload={"note": "reply"})
        )
    db_session.commit()
    entries = home_view(db_session).section(Section.DCF).entries
    assert entries[0].deal.id == stale.id
    assert entries[0].last_activity > now - timedelta(hours=1)


def test_last_activity_is_the_latest_of_its_sources(db_session: Session) -> None:
    now = datetime.now(UTC)
    deal = quiet_since(db_session, a_deal(db_session), now - timedelta(days=9))
    screen = RunSummary(deal.id, now - timedelta(days=4))
    underwrite = RunSummary(deal.id, now - timedelta(days=6))
    assert last_activity(deal, None, None, None, None) == deal.created_at
    assert last_activity(deal, screen, underwrite, None, None) == screen.created_at
    assert last_activity(
        deal, screen, underwrite, now - timedelta(days=1), now - timedelta(hours=2)
    ) == now - timedelta(hours=2)


def test_a_tie_is_broken_by_id_so_the_order_does_not_wobble(db_session: Session) -> None:
    now = datetime.now(UTC)
    deals = [
        quiet_since(db_session, a_deal(db_session, address=f"{n} Yew St, Tulsa OK"), now)
        for n in range(3)
    ]
    expected = sorted((deal.id for deal in deals), reverse=True)
    assert ids(home_view(db_session), Section.DCF) == expected
    assert ids(home_view(db_session), Section.DCF) == expected


def test_each_row_carries_the_latest_verdict_and_irr(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()
    first = latest_screen(db_session, deal_with_overrides.id)
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    db_session.commit()

    [entry] = home_view(db_session).section(Section.DCF).entries
    assert entry.screen is not None and first is not None and entry.screen.id != first.id
    assert entry.verdict is not None and entry.verdict.value == "GO"
    assert entry.irr is not None and entry.irr > 0


# --- the three controls (SPEC §4.6) ---------------------------------------------------------------


def test_pause_sets_the_deal_aside_and_records_where_it_was(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.IN_REVIEW)
    pause(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.PAUSED
    [row] = rows(db_session, deal_with_overrides, AuditAction.PAUSED)
    assert row.actor == ACTOR
    assert row.before == {"status": "IN_REVIEW"} and row.after == {"status": "PAUSED"}
    assert paused_from(db_session, deal_with_overrides) is Status.IN_REVIEW


def test_progress_on_a_paused_deal_returns_it_to_its_prior_status(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.UNDERWRITING)
    pause(db_session, deal_with_overrides.id, actor=ACTOR)
    progress(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.UNDERWRITING
    [row] = rows(db_session, deal_with_overrides, AuditAction.RESUMED)
    assert row.before == {"status": "PAUSED"} and row.after == {"status": "UNDERWRITING"}
    # ...and the pause before it is still on the record
    assert len(rows(db_session, deal_with_overrides, AuditAction.PAUSED)) == 1


def test_progress_on_a_screened_deal_is_the_advance_it_always_was(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    progress(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.IN_REVIEW
    [row] = rows(db_session, deal_with_overrides, AuditAction.ADVANCED_TO_REVIEW)
    assert row.before == {"status": "SCREENED"} and row.after == {"status": "IN_REVIEW"}


@pytest.mark.parametrize(
    "current", [Status.NEW, Status.IN_REVIEW, Status.UNDERWRITING, Status.DECLINED, Status.DEAD]
)
def test_progress_from_anywhere_else_is_refused_and_names_both_statuses_it_runs_from(
    db_session: Session, deal_with_overrides: Deal, current: Status
) -> None:
    at(db_session, deal_with_overrides, current)
    with pytest.raises(ActionNotAllowed) as caught:
        progress(db_session, deal_with_overrides.id, actor=ACTOR)
    assert "PAUSED" in str(caught.value) and "SCREENED" in str(caught.value)
    assert deal_with_overrides.status is current


@pytest.mark.parametrize("closed", [Status.PAUSED, Status.DECLINED, Status.DEAD])
def test_a_paused_or_closed_deal_is_not_paused_again(
    db_session: Session, deal_with_overrides: Deal, closed: Status
) -> None:
    at(db_session, deal_with_overrides, closed)
    with pytest.raises(ActionNotAllowed):
        pause(db_session, deal_with_overrides.id, actor=ACTOR)
    assert rows(db_session, deal_with_overrides, AuditAction.PAUSED) == []


def test_only_a_paused_deal_is_resumed(db_session: Session, deal_with_overrides: Deal) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    with pytest.raises(ActionNotAllowed):
        resume(db_session, deal_with_overrides.id, actor=ACTOR)


def test_a_paused_deal_with_no_pause_row_lands_where_a_reopened_one_does(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A row set PAUSED by a script has nothing to say where it came from."""
    at(db_session, deal_with_overrides, Status.PAUSED)
    assert paused_from(db_session, deal_with_overrides) is Status.NEW
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    at(db_session, deal_with_overrides, Status.PAUSED)
    assert paused_from(db_session, deal_with_overrides) is Status.SCREENED


def test_a_paused_deal_can_still_be_declined_or_killed(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.PAUSED)
    assert section_of(deal_with_overrides) is Section.PAUSED
    mark_dead(db_session, deal_with_overrides.id, actor=ACTOR, reason="went quiet on pause")
    db_session.commit()
    assert deal_with_overrides.status is Status.DEAD
    assert section_of(deal_with_overrides) is Section.DEAD


# --- PAUSED and the automatic runs (SPEC §4.6) ----------------------------------------------------


def test_a_paused_deal_is_left_out_of_the_automatic_runs(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.PAUSED)
    assert auto_run(db_session, deal_with_overrides).ran is False
    assert latest_screen(db_session, deal_with_overrides.id) is None


def test_an_edit_on_a_paused_deal_is_stored_and_waits(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.PAUSED)
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/overrides",
        data={"estimated_sale_price_team": "$210,000", "term_months": "6"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].endswith("notice=Inputs%20saved.")
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None
    assert deal.estimated_sale_price_team == 210000 and deal.status is Status.PAUSED
    assert latest_screen(db_session, deal.id) is None


def test_a_manual_run_on_a_paused_deal_leaves_it_paused(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The buttons still work - a person pressing one has not left the deal alone."""
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    at(db_session, deal_with_overrides, Status.PAUSED)
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()
    assert deal_with_overrides.status is Status.PAUSED


def test_resuming_puts_the_deal_back_in_its_live_section_and_runs_again_on_the_next_edit(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    pause(db_session, deal_with_overrides.id, actor=ACTOR)
    db_session.commit()
    assert ids(home_view(db_session), Section.PAUSED) == [deal_with_overrides.id]
    assert post(client, deal_with_overrides, "advance", return_to="home").status_code == 303
    db_session.expire_all()
    assert ids(home_view(db_session), Section.DCF) == [deal_with_overrides.id]
    save_overrides(db_session, deal_with_overrides.id, TeamOverrides(term_months=6), actor=ACTOR)
    assert auto_run(db_session, deal_with_overrides).ran is True


# --- the controls through the routes, as a Home row posts them ------------------------------------


def test_each_control_posts_from_a_home_row_and_lands_back_on_home(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    paused = post(client, deal_with_overrides, "pause", return_to="home")
    assert paused.status_code == 303
    assert paused.headers["location"].startswith("/queue?notice=Paused")
    resumed = post(client, deal_with_overrides, "advance", return_to="home")
    assert resumed.status_code == 303
    assert resumed.headers["location"].startswith("/queue?notice=Progressed")
    killed = post(client, deal_with_overrides, "kill", confirm="yes", return_to="home")
    assert killed.status_code == 303
    assert killed.headers["location"].startswith("/queue?notice=Killed")

    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.DEAD
    actions = [
        row.action
        for row in db_session.scalars(
            select(AuditLog)
            .where(AuditLog.table_name == DEALS, AuditLog.row_id == str(deal.id))
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    ]
    assert actions[-3:] == [
        AuditAction.PAUSED.value,
        AuditAction.RESUMED.value,
        AuditAction.MARKED_DEAD.value,
    ]
    assert {
        row.actor
        for row in db_session.scalars(
            select(AuditLog).where(
                AuditLog.row_id == str(deal.id), AuditLog.action != "INTAKE_CREATED"
            )
        )
    } == {"sam@glenwood.example"}


def test_the_same_controls_from_the_deal_page_land_back_on_the_deal(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    response = post(client, deal_with_overrides, "pause")
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/queue/deals/{deal_with_overrides.id}?")


def test_kill_asks_first_and_a_post_without_the_confirmation_kills_nothing(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    page = client.get(f"/queue/deals/{deal_with_overrides.id}/kill?return_to=home")
    assert page.status_code == 200
    assert "Kill this deal?" in page.text
    assert 'name="confirm" value="yes"' in page.text
    assert 'name="return_to" value="home"' in page.text
    assert 'href="/queue"' in page.text  # the way out

    unconfirmed = post(client, deal_with_overrides, "kill", reason="nope")
    assert unconfirmed.status_code == 422
    assert "Kill this deal?" in unconfirmed.text
    db_session.expire_all()
    assert db_session.get(Deal, deal_with_overrides.id).status is Status.NEW  # type: ignore[union-attr]
    assert rows(db_session, deal_with_overrides, AuditAction.MARKED_DEAD) == []


def test_kill_records_the_reason_when_one_is_given(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    killed = post(client, deal_with_overrides, "kill", confirm="yes", reason="sold elsewhere")
    assert killed.status_code == 303
    db_session.expire_all()
    [row] = rows(db_session, deal_with_overrides, AuditAction.MARKED_DEAD)
    assert row.after == {"status": "DEAD", "reason": "sold elsewhere"}


def test_a_control_the_status_rules_out_comes_back_as_the_deal_page_saying_why(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.DEAD)
    response = post(client, deal_with_overrides, "pause", return_to="home")
    assert response.status_code == 409
    assert "cannot be paused" in response.text


# --- the pages ------------------------------------------------------------------------------------


def test_the_home_page_lists_every_section_and_the_row_columns(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()
    response = client.get("/queue")
    assert response.status_code == 200
    body = response.text
    assert "<h1>Home</h1>" in body
    for title in ("Borrower Submissions", "DCF New Deals", "On Pause", "Dead"):
        assert title in body
    assert body.index("Borrower Submissions") < body.index("DCF New Deals")
    assert body.index("DCF New Deals") < body.index("On Pause") < body.index("Dead")
    for column in ("Borrower", "Address", "Loan type", "Verdict", "Source", "Last activity"):
        assert f"<th>{column}</th>" in body, column
    assert '<th class="num">Loan amount</th>' in body and '<th class="num">IRR</th>' in body
    assert str(deal_with_overrides.id) in body
    assert ">GO<" in body and "Wholetail" in body and "Team" in body
    for control in ("Progress", "Pause", "Kill"):
        assert f">{control}</button>" in body or f">{control}</a>" in body, control
    assert "Review queue" not in body and "pinned" not in body.lower()


def test_the_top_bar_has_home_and_new_deal_on_every_page(
    client: TestClient, deal_with_overrides: Deal
) -> None:
    for path in ("/queue", "/queue/new", f"/queue/deals/{deal_with_overrides.id}"):
        body = client.get(path).text
        assert '<a class="btn" href="/queue">Home</a>' in body
        assert '<a class="btn" href="/queue/new">New Deal</a>' in body
        assert ">Queue<" not in body


def test_a_web_deal_shows_its_source_and_a_paused_deal_its_section(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    web = a_web_deal(anon_client, db_session)
    at(db_session, a_deal(db_session), Status.PAUSED)
    body = client.get("/queue").text
    assert "Web" in body and str(web.id) in body
    assert "PAUSED" in body


def test_an_empty_home_page_points_at_the_entry_form(client: TestClient) -> None:
    response = client.get("/queue")
    assert response.status_code == 200
    assert "No deals yet" in response.text


def test_the_new_deal_page_renders_the_team_entry_form(client: TestClient) -> None:
    response = client.get("/queue/new")
    assert response.status_code == 200
    assert 'action="/intake/team"' in response.text
    assert 'name="borrower_phone"' in response.text
    assert "<h1>New Deal</h1>" in response.text


def test_an_unknown_deal_page_goes_back_home(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/queue/deals/{missing}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/queue")


def test_the_deal_page_shows_intake_missing_fields_and_the_overrides(
    client: TestClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    payload = dict(team_entry)
    payload.pop("credit_range")
    deal = store_deal(db_session, payload)

    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200
    assert "NEEDS_INFO" in response.text
    assert "borrower.credit_range" in response.text
    assert "Not analyzed yet." in response.text
    assert "No analysis yet." in response.text
    assert 'name="estimated_sale_price_team"' in response.text
    assert 'name="asset_type"' not in response.text and 'name="stated_exit"' not in response.text
    assert "Asset Type" not in response.text and "<dt>Exit</dt>" not in response.text


def test_the_deal_page_renders_a_screen_and_an_underwrite(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()

    response = client.get(f"/queue/deals/{deal_with_overrides.id}")
    assert response.status_code == 200
    body = response.text
    assert "Scored as" in body
    assert "Leverage against caps" in body
    assert ">LTV<" in body and ">LTC<" in body
    assert body.index("Return Overview") < body.index("<h2>Sensitivity")
    assert body.index("<h2>Sensitivity") < body.index("Flip Analysis")
    assert body.index("Flip Analysis") < body.index("Rental Analysis")
    assert body.index("Rental Analysis") < body.index("Take-Back Analysis")
    assert "Future Draws" in body
    assert "Yield (Profit / Costs)" in body
    assert "DSCR at loan cost" in body
    assert AuditAction.UNDERWRITE_RUN.value in body
    # the payoff date is the month end, derived and read-only (SPEC §8.1)
    payoff = re.search(r"<dt>Payoff Date.*?</dt>\s*<dd>([^<]*)</dd>", body, re.S)
    assert payoff is not None and payoff.group(1).strip() == "2027-08-31"


def test_the_run_analysis_button_screens_and_underwrites_the_deal(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    ran = client.post(f"/queue/deals/{deal_with_overrides.id}/run", follow_redirects=False)
    assert ran.status_code == 303
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.UNDERWRITING


def test_the_analysis_runs_without_a_rent(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """The rent is optional: without one, both DSCR analyses stand down (SPEC §8.5, §8.6)."""
    save_overrides(
        db_session,
        deal_with_overrides.id,
        TeamOverrides(
            estimated_sale_price_team=deal_with_overrides.estimated_sale_price_team,
            closing_date=deal_with_overrides.closing_date,
            term_months=deal_with_overrides.term_months,
            interest_rate=deal_with_overrides.interest_rate,
            court_records_status=deal_with_overrides.court_records_status,
            court_records_as_of=deal_with_overrides.court_records_as_of,
        ),
        actor=ACTOR,
    )
    db_session.commit()
    response = client.post(f"/queue/deals/{deal_with_overrides.id}/run", follow_redirects=False)
    assert response.status_code == 303, response.text
    page = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert "NOT_EVALUATED" in page
    # the flag is on the stored result, not the page (SPEC §9)
    row = latest_underwrite(db_session, deal_with_overrides.id)
    assert row is not None
    assert "MONTHLY_RENT_MISSING" in {flag.code.value for flag in underwrite_result(row).flags}
    assert "MONTHLY_RENT_MISSING" not in page


def test_the_analysis_names_what_the_deal_still_needs(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.term_months = None
    db_session.commit()
    response = client.post(f"/queue/deals/{deal_with_overrides.id}/run")
    assert response.status_code == 422
    assert "deal.term_months" in response.text
    assert "The ledger is off until these are entered" in response.text


def test_the_analysis_refuses_a_closed_deal_and_keeps_the_page(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.DEAD)
    response = client.post(f"/queue/deals/{deal_with_overrides.id}/run")
    assert response.status_code == 409
    assert "The ledger did not run: the deal is DEAD." in response.text


def test_a_declining_deal_lands_in_the_dead_section(
    db_session: Session, declining_deal: Deal
) -> None:
    run_screen(db_session, declining_deal.id, CONFIG, actor=ACTOR)
    db_session.commit()
    assert declining_deal.status is Status.DECLINED
    assert ids(home_view(db_session), Section.DEAD) == [declining_deal.id]
    assert DECLINING_SALE_PRICE  # the price that declines it, pinned in conftest


# --- every fixture, end to end --------------------------------------------------------------------

TEAM_ENTRY_FIXTURES = sorted(
    path
    for path in FIXTURE_DIR.glob("*.json")
    if "team_entry" in json.loads(path.read_text(encoding="utf-8"))
)


@pytest.mark.parametrize("path", TEAM_ENTRY_FIXTURES, ids=[p.stem for p in TEAM_ENTRY_FIXTURES])
def test_every_team_entry_fixture_renders_its_screen_and_underwrite(
    client: TestClient, db_session: Session, path: Path
) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    deal = store_deal(db_session, fixture["team_entry"])
    request = UnderwriteRequest(**fixture["underwrite"]["request"])

    run_screen(db_session, deal.id, CONFIG, actor=ACTOR)
    run_underwrite(db_session, deal.id, request, CONFIG, actor=ACTOR)
    db_session.commit()

    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200, response.text
    assert "Scored as" in response.text
    assert "Return Overview" in response.text and "<h2>Sensitivity" in response.text
    assert "Take-Back Analysis" in response.text
    assert client.get("/queue").status_code == 200
