"""The review-queue list and the pages it renders.  # SPEC §9.1, §12

The ordering rule and the pin rule are tested against ``queue_view`` directly, because they
are decisions and not markup. The pages are tested by rendering them: every fixture deal is
put through the queue, screened, underwritten where it can be, and the deal page asked for -
so a template that reaches for a field the engine renamed fails here rather than in front of
someone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from config.config import Config
from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite
from schema.models import AuditAction, Channel, Severity, Status
from services import (
    DEALS,
    TeamOverrides,
    add_note,
    advance_to_review,
    last_activity,
    latest_screen,
    queue_view,
    record_audit,
    run_screen,
    run_underwrite,
    save_overrides,
    screen_result,
)
from services.queue import RunSummary
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, DECLINING_SALE_PRICE, requires_db, store_deal

pytestmark = requires_db

CONFIG = Config.load()
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
TEAM_ENTRY = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def a_deal(session: Session, **overrides: Any) -> Deal:
    """One complete deal, with whatever the caller wants different about it."""
    payload = json.loads(TEAM_ENTRY.read_text(encoding="utf-8"))
    payload.update(overrides)
    return store_deal(session, payload)


def a_deal_that_declines(session: Session, **overrides: Any) -> Deal:
    """The same deal at a sale price its commitment is 88.6% of: a Hard LTV flag, so a
    Decline (SPEC §7.4, §7.5). The pin tests need a real Hard flag from the real engine."""
    return a_deal(session, estimated_sale_price_team=DECLINING_SALE_PRICE, **overrides)


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


def statuses(view: Any) -> list[str]:
    return [group.status.value for group in view.groups]


# --- ordering -------------------------------------------------------------------------------------


def test_deals_are_grouped_by_status_in_lifecycle_order(db_session: Session) -> None:
    """The enum is the lifecycle, so the page reads the way the deal moves."""
    wanted = [Status.NEW, Status.SCREENED, Status.IN_REVIEW, Status.DEAD]
    for index, status in enumerate(wanted):
        at(db_session, a_deal(db_session, address=f"{index} Main St, Tulsa OK"), status)
    view = queue_view(db_session)
    assert statuses(view) == [status.value for status in wanted]
    # only the statuses that have a deal in them; an empty heading is noise
    assert Status.LOI_SENT.value not in statuses(view)


def quiet_since(session: Session, deal: Deal, when: datetime) -> Deal:
    """Backdate everything that has ever happened to the deal to one moment.

    Every activity source at once - the deal row, its submissions, its runs and its audit
    rows - so ``last_activity`` is exactly ``when`` and a test can then apply one later event
    and know that event is the only thing the order can be reading.
    """
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


def order_of(session: Session) -> list[Any]:
    view = queue_view(session)
    assert len(view.groups) == 1, statuses(view)
    return [entry.deal.id for entry in view.groups[0].entries]


def test_a_group_is_ordered_by_last_activity_not_by_arrival(db_session: Session) -> None:
    """The deal somebody is in the middle of is the deal worth seeing first."""
    now = datetime.now(UTC)
    oldest, middle, newest = (
        quiet_since(db_session, a_deal(db_session, address=f"{n} Elm St, Tulsa OK"), now - gap)
        for n, gap in enumerate((timedelta(days=3), timedelta(days=2), timedelta(days=1)))
    )
    assert order_of(db_session) == [newest.id, middle.id, oldest.id]

    # a note on the deal nobody had touched in three days puts it at the top
    add_note(db_session, oldest.id, actor=ACTOR, note="Borrower called back")
    db_session.commit()
    assert order_of(db_session) == [oldest.id, newest.id, middle.id]


@pytest.mark.parametrize("event", ["note", "action", "screen", "underwrite", "submission"])
def test_every_kind_of_activity_moves_a_deal_up(db_session: Session, event: str) -> None:
    """Intake, a screen, an underwrite, a team action and a note all count as a touch."""
    now = datetime.now(UTC)
    stale = quiet_since(db_session, deal_that_screens_go(db_session), now - timedelta(days=5))
    fresh = quiet_since(
        db_session, a_deal(db_session, address="9 Ash St, Tulsa OK"), now - timedelta(hours=1)
    )
    assert order_of(db_session) == [fresh.id, stale.id]

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

    view = queue_view(db_session)
    touched = [
        entry for group in view.groups for entry in group.entries if entry.deal.id == stale.id
    ]
    assert len(touched) == 1
    assert touched[0].last_activity > now - timedelta(hours=1)
    # and it now leads its group, whichever group the event left it in
    group = next(g for g in view.groups if any(e.deal.id == stale.id for e in g.entries))
    assert group.entries[0].deal.id == stale.id


def test_last_activity_is_the_latest_of_its_sources(db_session: Session) -> None:
    """The rule itself, with nothing else in the way."""
    now = datetime.now(UTC)
    deal = quiet_since(db_session, a_deal(db_session), now - timedelta(days=9))
    screen = RunSummary(uuid4(), now - timedelta(days=4), [])
    underwrite = RunSummary(uuid4(), now - timedelta(days=6), [])

    assert last_activity(deal, None, None, None, None) == deal.created_at
    assert last_activity(deal, screen, underwrite, None, None) == screen.created_at
    assert last_activity(
        deal, screen, underwrite, now - timedelta(days=1), None
    ) == now - timedelta(days=1)
    assert last_activity(
        deal, screen, underwrite, now - timedelta(days=1), now - timedelta(hours=2)
    ) == now - timedelta(hours=2)
    # nothing later than the deal itself still sorts, rather than raising
    fresh = quiet_since(db_session, a_deal(db_session, address="3 Fir St, Tulsa OK"), now)
    assert last_activity(fresh, None, None, None, None) == now


def test_a_tie_is_broken_by_id_so_the_order_does_not_wobble(db_session: Session) -> None:
    """Two deals touched in the same transaction must not swap between page loads."""
    now = datetime.now(UTC)
    deals = [
        quiet_since(db_session, a_deal(db_session, address=f"{n} Yew St, Tulsa OK"), now)
        for n in range(3)
    ]
    expected = sorted((deal.id for deal in deals), reverse=True)
    assert order_of(db_session) == expected
    assert order_of(db_session) == expected


def test_the_view_counts_everything_it_holds(db_session: Session) -> None:
    for n in range(4):
        a_deal(db_session, address=f"{n} Oak St, Tulsa OK")
    view = queue_view(db_session)
    assert view.total == 4
    assert queue_view(db_session).pinned == []


def test_an_empty_queue_is_empty_rather_than_an_error(db_session: Session) -> None:
    view = queue_view(db_session)
    assert view.groups == [] and view.pinned == [] and view.total == 0


def test_each_entry_carries_the_latest_run_of_each_stage(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    db_session.commit()
    first = latest_screen(db_session, deal_with_overrides.id)
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    db_session.commit()

    entry = queue_view(db_session).groups[0].entries[0]
    assert entry.screen is not None
    assert first is not None and entry.screen.id != first.id
    assert entry.underwrite is None


# --- pinning (SPEC §12) ---------------------------------------------------------------------------


def hard_flagged(session: Session, deal: Deal) -> None:
    """Screen the deal into a state where its latest screen carries a Hard flag.

    The deal's sale price puts its commitment past the LTV cap by more than the tolerance
    band: a real Hard flag from the real engine rather than a row written by hand.
    """
    run_screen(session, deal.id, CONFIG, actor=ACTOR)
    session.commit()
    row = latest_screen(session, deal.id)
    assert row is not None
    assert any(flag.severity is Severity.HARD for flag in screen_result(row).flags)


@pytest.mark.parametrize("status", [Status.LOI_SENT, Status.HANDED_OFF])
def test_a_hard_flag_raised_after_the_loi_pins_the_deal(
    db_session: Session, declining_deal: Deal, status: Status
) -> None:
    record_audit(
        db_session,
        actor=ACTOR,
        action=AuditAction.LOI_SENT if status is Status.LOI_SENT else AuditAction.HANDED_OFF,
        table_name=DEALS,
        row_id=declining_deal.id,
        after={"status": status.value},
    )
    at(db_session, declining_deal, status)
    hard_flagged(db_session, declining_deal)

    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [declining_deal.id]
    entry = view.pinned[0]
    assert entry.pinned is True
    assert entry.pin_reason is not None
    assert "Hard flag" in entry.pin_reason
    assert status.value in entry.pin_reason
    assert entry.hard_flags
    # pinned once, at the top, and not again under its own status
    assert statuses(view) == []


def test_a_hard_flag_raised_before_the_loi_does_not_pin(
    db_session: Session, declining_deal: Deal
) -> None:
    """The flag was already on the file when the LOI went out; it is not news."""
    hard_flagged(db_session, declining_deal)
    at(db_session, declining_deal, Status.LOI_SENT)
    record_audit(
        db_session,
        actor=ACTOR,
        action=AuditAction.LOI_SENT,
        table_name=DEALS,
        row_id=declining_deal.id,
        after={"status": Status.LOI_SENT.value},
    )
    db_session.commit()

    view = queue_view(db_session)
    assert view.pinned == []
    assert statuses(view) == [Status.LOI_SENT.value]


def test_a_deal_short_of_the_loi_is_never_pinned(db_session: Session, declining_deal: Deal) -> None:
    """Below LOI_SENT a Decline still closes the deal (SPEC §4.6); nothing needs pinning."""
    hard_flagged(db_session, declining_deal)
    assert declining_deal.status is Status.DECLINED
    assert queue_view(db_session).pinned == []


def test_a_deal_at_the_loi_with_no_hard_flag_is_not_pinned(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_screen(db_session, deal_with_overrides.id, CONFIG, actor=ACTOR)
    at(db_session, deal_with_overrides, Status.LOI_SENT)
    view = queue_view(db_session)
    assert view.pinned == []
    assert statuses(view) == [Status.LOI_SENT.value]


def test_a_deal_that_reached_the_loi_without_an_audit_row_still_pins(
    db_session: Session, declining_deal: Deal
) -> None:
    """No row to compare against, and a missed Hard flag at LOI is the worse mistake."""
    at(db_session, declining_deal, Status.LOI_SENT)
    hard_flagged(db_session, declining_deal)
    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [declining_deal.id]
    assert "and the deal is LOI_SENT" in (view.pinned[0].pin_reason or "")


def test_an_underwrite_hard_flag_pins_as_readily_as_a_screen_one(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """The pin rule reads the latest run of each stage, not only Stage 1."""
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()
    record_audit(
        db_session,
        actor=ACTOR,
        action=AuditAction.HANDED_OFF,
        table_name=DEALS,
        row_id=deal_with_overrides.id,
        after={"status": Status.HANDED_OFF.value},
    )
    at(db_session, deal_with_overrides, Status.HANDED_OFF)
    # a credit floor breach is a Hard flag the underwrite raises on its own inputs
    run_underwrite(
        db_session,
        deal_with_overrides.id,
        UnderwriteRequest(verified_credit_score=560),
        CONFIG,
        actor=ACTOR,
    )
    db_session.commit()

    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [deal_with_overrides.id]
    assert "underwrite" in (view.pinned[0].pin_reason or "")


def test_the_pinned_list_is_ordered_by_last_activity_too(db_session: Session) -> None:
    """One sort, applied before the split, so the top of the page reads the same way."""
    now = datetime.now(UTC)
    pinned_deals = []
    for n in range(2):
        deal = a_deal_that_declines(db_session, address=f"{n} Birch St, Tulsa OK")
        at(db_session, deal, Status.LOI_SENT)
        hard_flagged(db_session, deal)
        pinned_deals.append(deal)
    stale, fresh = pinned_deals
    quiet_since(db_session, stale, now - timedelta(days=4))
    quiet_since(db_session, fresh, now - timedelta(hours=3))

    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [fresh.id, stale.id]

    add_note(db_session, stale.id, actor=ACTOR, note="lender asked about the lien")
    db_session.commit()
    assert [entry.deal.id for entry in queue_view(db_session).pinned] == [stale.id, fresh.id]


# --- the pages ------------------------------------------------------------------------------------


def test_the_queue_page_lists_a_deal_and_its_status(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.SCREENED)
    response = client.get("/queue")
    assert response.status_code == 200
    assert str(deal_with_overrides.id) in response.text
    assert "SCREENED" in response.text
    assert "Review queue" in response.text
    # the column the order is by is the column the page shows
    assert "Last activity" in response.text
    assert "most recently touched first" in response.text


def test_the_queue_page_shows_the_pin_banner(
    client: TestClient, db_session: Session, declining_deal: Deal
) -> None:
    at(db_session, declining_deal, Status.LOI_SENT)
    hard_flagged(db_session, declining_deal)
    response = client.get("/queue")
    assert response.status_code == 200
    assert "Hard flags after LOI or handoff" in response.text
    assert "SPEC" in response.text


def test_an_empty_queue_page_points_at_the_entry_form(client: TestClient) -> None:
    response = client.get("/queue")
    assert response.status_code == 200
    assert "Nothing in the queue yet" in response.text


def test_the_new_deal_page_renders_the_team_entry_form(client: TestClient) -> None:
    response = client.get("/queue/new")
    assert response.status_code == 200
    assert 'action="/intake/team"' in response.text
    assert 'name="borrower_phone"' in response.text
    assert 'name="matter_code"' in response.text


def test_an_unknown_deal_page_goes_back_to_the_queue(client: TestClient) -> None:
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
    assert "Not screened yet" in response.text
    assert "Not underwritten yet" in response.text
    assert 'name="estimated_sale_price_team"' in response.text


def test_the_deal_page_renders_a_screen_and_an_underwrite(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    run_underwrite(db_session, deal_with_overrides.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    db_session.commit()

    response = client.get(f"/queue/deals/{deal_with_overrides.id}")
    assert response.status_code == 200
    body = response.text
    # the screen summary (SPEC §9.1)
    assert "Score components" in body
    assert "Suggested reply" in body
    assert "sent by hand" in body
    assert "Copy reply" in body
    # the sizing table and the §9 result sections, in their SPEC order
    assert "Sizing against caps" in body
    assert ">LTV<" in body and ">LTC<" in body
    assert "LTARV" not in body
    assert body.index("Return Overview") < body.index("Flip Analysis")
    assert body.index("Flip Analysis") < body.index("Rental Analysis")
    assert body.index("Rental Analysis") < body.index("Take-Back Analysis")
    assert "Future Draws" in body  # the ledger's own column
    assert "Yield (Profit / Costs)" in body
    assert "DSCR at loan cost" in body
    # the audit trail
    assert AuditAction.UNDERWRITE_RUN.value in body


def test_the_run_buttons_screen_and_underwrite_the_deal(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    screened = client.post(f"/queue/deals/{deal_with_overrides.id}/screen", follow_redirects=False)
    assert screened.status_code == 303
    priced = client.post(
        f"/queue/deals/{deal_with_overrides.id}/underwrite", follow_redirects=False
    )
    assert priced.status_code == 303

    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None and deal.status is Status.UNDERWRITING


def test_the_underwrite_button_runs_without_a_rent(
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

    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/underwrite", follow_redirects=False
    )
    assert response.status_code == 303, response.text
    page = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert "NOT_EVALUATED" in page
    assert "MONTHLY_RENT_MISSING" in page


def test_the_underwrite_button_names_what_the_deal_still_needs(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """A deal with no rate on it is screened and not priced.  # SPEC §8.1"""
    deal_with_overrides.interest_rate = None
    db_session.commit()

    response = client.post(f"/queue/deals/{deal_with_overrides.id}/underwrite")
    assert response.status_code == 422
    assert "deal.interest_rate" in response.text
    # and the page said so before the button was pressed
    assert "Run underwrite is off until these are entered" in response.text


def test_the_underwrite_button_refuses_a_closed_deal_and_keeps_the_page(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    at(db_session, deal_with_overrides, Status.DEAD)
    response = client.post(f"/queue/deals/{deal_with_overrides.id}/underwrite")
    assert response.status_code == 409
    assert "cannot be underwritten" in response.text


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
    """A fixture the CLI can run is a fixture the deal page can show."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    deal = store_deal(db_session, fixture["team_entry"])
    request = UnderwriteRequest(**fixture["underwrite"]["request"])

    run_screen(db_session, deal.id, CONFIG, actor=ACTOR)
    run_underwrite(db_session, deal.id, request, CONFIG, actor=ACTOR)
    db_session.commit()

    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200, response.text
    assert "Score components" in response.text
    assert "Return Overview" in response.text
    assert "Take-Back Analysis" in response.text
