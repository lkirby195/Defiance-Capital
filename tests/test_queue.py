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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal
from schema.models import AuditAction, Severity, Status
from services import (
    DEALS,
    TeamOverrides,
    latest_screen,
    queue_view,
    record_audit,
    run_screen,
    run_underwrite,
    save_overrides,
    screen_result,
)
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, requires_db, store_deal

pytestmark = requires_db

CONFIG = Config.load()
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
TEAM_ENTRY = Path(__file__).resolve().parents[1] / "fixtures/synthetic/team_entry_complete.json"


def a_deal(session: Session, **overrides: Any) -> Deal:
    """One complete deal, with whatever the caller wants different about it."""
    payload = json.loads(TEAM_ENTRY.read_text(encoding="utf-8"))
    payload.update(overrides)
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


def test_within_a_group_the_newest_deal_is_first(db_session: Session) -> None:
    deals = [a_deal(db_session, address=f"{n} Elm St, Tulsa OK") for n in range(3)]
    for offset, deal in enumerate(deals):
        deal.created_at = datetime.now(UTC) - timedelta(days=offset)
    db_session.commit()

    view = queue_view(db_session)
    assert len(view.groups) == 1
    assert [entry.deal.id for entry in view.groups[0].entries] == [deal.id for deal in deals]


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

    The stock fixture has no valuation, so its LTV falls back to the purchase price and
    blows the cap: a real Hard flag from the real engine rather than a row written by hand.
    """
    run_screen(session, deal.id, CONFIG, actor=ACTOR)
    session.commit()
    row = latest_screen(session, deal.id)
    assert row is not None
    assert any(flag.severity is Severity.HARD for flag in screen_result(row).flags)


@pytest.mark.parametrize("status", [Status.LOI_SENT, Status.HANDED_OFF])
def test_a_hard_flag_raised_after_the_loi_pins_the_deal(
    db_session: Session, stored_deal: Deal, status: Status
) -> None:
    record_audit(
        db_session,
        actor=ACTOR,
        action=AuditAction.LOI_SENT if status is Status.LOI_SENT else AuditAction.HANDED_OFF,
        table_name=DEALS,
        row_id=stored_deal.id,
        after={"status": status.value},
    )
    at(db_session, stored_deal, status)
    hard_flagged(db_session, stored_deal)

    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [stored_deal.id]
    entry = view.pinned[0]
    assert entry.pinned is True
    assert entry.pin_reason is not None
    assert "Hard flag" in entry.pin_reason
    assert status.value in entry.pin_reason
    assert entry.hard_flags
    # pinned once, at the top, and not again under its own status
    assert statuses(view) == []


def test_a_hard_flag_raised_before_the_loi_does_not_pin(
    db_session: Session, stored_deal: Deal
) -> None:
    """The flag was already on the file when the LOI went out; it is not news."""
    hard_flagged(db_session, stored_deal)
    at(db_session, stored_deal, Status.LOI_SENT)
    record_audit(
        db_session,
        actor=ACTOR,
        action=AuditAction.LOI_SENT,
        table_name=DEALS,
        row_id=stored_deal.id,
        after={"status": Status.LOI_SENT.value},
    )
    db_session.commit()

    view = queue_view(db_session)
    assert view.pinned == []
    assert statuses(view) == [Status.LOI_SENT.value]


def test_a_deal_short_of_the_loi_is_never_pinned(db_session: Session, stored_deal: Deal) -> None:
    """Below LOI_SENT a Decline still closes the deal (SPEC §4.6); nothing needs pinning."""
    hard_flagged(db_session, stored_deal)
    assert stored_deal.status is Status.DECLINED
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
    db_session: Session, stored_deal: Deal
) -> None:
    """No row to compare against, and a missed Hard flag at LOI is the worse mistake."""
    at(db_session, stored_deal, Status.LOI_SENT)
    hard_flagged(db_session, stored_deal)
    view = queue_view(db_session)
    assert [entry.deal.id for entry in view.pinned] == [stored_deal.id]
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


def test_the_queue_page_shows_the_pin_banner(
    client: TestClient, db_session: Session, stored_deal: Deal
) -> None:
    at(db_session, stored_deal, Status.LOI_SENT)
    hard_flagged(db_session, stored_deal)
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
    payload.pop("borrower_phone")
    deal = store_deal(db_session, payload)

    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200
    assert "NEEDS_INFO" in response.text
    assert "borrower.phone" in response.text
    assert "Not screened yet" in response.text
    assert "Not underwritten yet" in response.text
    assert 'name="as_is_value_team"' in response.text


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
    # the sizing table and the yield grid (SPEC §8.2, §8.5)
    assert "Sizing against caps" in body
    assert "LTARV" in body
    assert "Lender yield grid" in body
    assert "Solved rate r*" in body
    # the solved column is named in the header, because the headers round to a tenth
    assert body.count(">r*<") == 1
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


def test_the_underwrite_button_names_what_the_deal_still_needs(
    client: TestClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """Utilities and market rent have no default, so a deal without them is not priced."""
    save_overrides(
        db_session,
        deal_with_overrides.id,
        TeamOverrides(
            as_is_value_team=deal_with_overrides.as_is_value_team,
            arv_team=deal_with_overrides.arv_team,
            court_records_status=deal_with_overrides.court_records_status,
            court_records_as_of=deal_with_overrides.court_records_as_of,
        ),
        actor=ACTOR,
    )
    db_session.commit()

    response = client.post(f"/queue/deals/{deal_with_overrides.id}/underwrite")
    assert response.status_code == 422
    assert "market_rent_monthly" in response.text
    assert "annual_utilities_usd" in response.text


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
    assert "Lender yield grid" in response.text
    assert "Downside" in response.text
