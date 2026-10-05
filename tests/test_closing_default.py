"""The closing date is optional on every channel and defaults when absent.  # SPEC §8.1

The default is the last day of the month ``closing.default_lead_days`` after the deal came
in, stored on the deal and tagged DEFAULT like the rate and the fees
(``services/defaults.py``), so the ledger always has a month 0 and the team is never refused
a price for want of a date. A later edit makes the date the team's own; a blank puts the
default back; and readiness never names the closing date as a reason the ledger is off.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from api.intake_form import REQUIRED_NAMES
from api.ratelimit import LIMITER
from config.config import DEFAULT_PATH, Config, load_yaml
from db.models import AuditLog, Deal, Underwrite
from schema.dates import month_end
from schema.models import AuditAction, Status
from services import (
    DEALS,
    InputSource,
    TeamOverrides,
    default_closing_date,
    is_defaulted,
    populate,
    run_underwrite,
    save_overrides,
    underwrite_readiness,
)
from services.assemble import underwrite_inputs
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, QueueClient, form_body, requires_db, store_deal
from tests.test_apply import VALID

pytestmark = requires_db

CONFIG = Config.load()
D = Decimal


def expected_default(deal: Deal) -> date:
    return month_end(deal.created_at.date() + timedelta(days=CONFIG.closing.default_lead_days))


def without(payload: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in names}


# --- the arithmetic -------------------------------------------------------------------------------


def test_the_default_is_the_month_end_two_weeks_on() -> None:
    assert default_closing_date(date(2026, 10, 4), CONFIG) == date(2026, 10, 31)
    assert default_closing_date(date(2026, 10, 17), CONFIG) == date(2026, 10, 31)
    assert default_closing_date(date(2026, 10, 18), CONFIG) == date(2026, 11, 30)
    assert default_closing_date(date(2026, 12, 20), CONFIG) == date(2027, 1, 31)
    assert default_closing_date(date(2028, 2, 10), CONFIG) == date(2028, 2, 29)


def test_the_lead_is_config() -> None:
    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["closing"]["default_lead_days"] = 0
    same_month = Config.from_dict(data)
    assert default_closing_date(date(2026, 10, 20), same_month) == date(2026, 10, 31)
    data["closing"]["default_lead_days"] = 45
    later = Config.from_dict(data)
    assert default_closing_date(date(2026, 10, 20), later) == date(2026, 12, 31)


# --- every channel --------------------------------------------------------------------------------


def test_a_json_team_entry_without_a_closing_date_is_dated_tagged_and_priced(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    response = client.post("/intake/team", json=without(team_entry, "closing_date"))
    assert response.status_code == 201, response.text
    deal = db_session.get(Deal, response.json()["id"])
    assert deal is not None
    assert deal.closing_date == expected_default(deal)
    assert "closing_date" in deal.defaulted_fields and is_defaulted(deal, "closing_date")
    # the ledger ran on it: nothing stood down for want of a date
    assert deal.status is Status.UNDERWRITING
    (created,) = db_session.scalars(
        select(AuditLog).where(
            AuditLog.table_name == DEALS,
            AuditLog.row_id == str(deal.id),
            AuditLog.action == AuditAction.INTAKE_CREATED.value,
        )
    )
    assert created.after is not None
    assert created.after["defaults"]["closing_date"] == deal.closing_date.isoformat()


def test_the_team_form_no_longer_requires_the_box(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    assert "closing_date" not in REQUIRED_NAMES
    page = client.get("/queue/new").text
    tag = page[page.index('id="closing_date"') :]
    box = tag[: tag.index(">")]  # the input tag alone; the next box is the required term
    assert "required" not in box
    response = client.post(
        "/intake/team", data=without(form_body(team_entry), "closing_date"), follow_redirects=False
    )
    assert response.status_code == 303, response.text
    (deal,) = list(db_session.scalars(select(Deal)))
    assert deal.closing_date == expected_default(deal)
    assert is_defaulted(deal, "closing_date")


def test_a_web_submission_without_a_closing_date_is_priced_on_the_default(
    anon_client: QueueClient, db_session: Session
) -> None:
    LIMITER.reset()
    body = {**VALID, "closing_date": ""}
    assert anon_client.post("/apply", data=body, follow_redirects=False).status_code == 303
    (deal,) = list(db_session.scalars(select(Deal)))
    assert deal.closing_date == expected_default(deal)
    assert is_defaulted(deal, "closing_date")
    assert deal.status is Status.UNDERWRITING
    assert db_session.scalar(select(func.count()).select_from(Underwrite)) == 1


def test_an_entered_date_is_the_teams_own_and_not_tagged(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)  # closes 2026-10-01
    assert deal.closing_date == date(2026, 10, 1)
    assert not is_defaulted(deal, "closing_date")
    assert "closing_date" not in deal.defaulted_fields


def test_an_entered_date_equal_to_the_default_is_the_default(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The same rule as the rate: a number equal to the default has not been chosen."""
    probe = store_deal(db_session, without(team_entry, "closing_date"))
    typed = store_deal(
        db_session,
        {
            **team_entry,
            "borrower_phone": "720-555-0193",
            "address": "1 Other St, Denver, CO 80211",
            "closing_date": probe.closing_date.isoformat(),
        },
    )
    assert typed.closing_date == expected_default(typed)
    assert is_defaulted(typed, "closing_date")


# --- a later edit ---------------------------------------------------------------------------------


def test_a_later_edit_makes_the_date_the_teams_and_moves_the_ledger(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, without(team_entry, "closing_date"))
    assert is_defaulted(deal, "closing_date")
    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(closing_date=date(2027, 3, 10), term_months=9),
        actor=ACTOR,
    )
    db_session.commit()
    assert deal.closing_date == date(2027, 3, 10)
    assert not is_defaulted(deal, "closing_date")
    result = run_underwrite(db_session, deal.id, UnderwriteRequest(), CONFIG, actor=ACTOR)
    assert result.closing_date == date(2027, 3, 10)
    assert result.payoff_date == date(2027, 12, 31)
    # ...and a blank puts the default back, tagged
    save_overrides(db_session, deal.id, TeamOverrides(term_months=9), actor=ACTOR)
    db_session.commit()
    assert deal.closing_date == expected_default(deal)
    assert is_defaulted(deal, "closing_date")
    saves = list(
        db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.OVERRIDES_SAVED.value)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )
    assert [row.after["closing_date"] for row in saves if row.after] == [
        "2027-03-10",
        expected_default(deal).isoformat(),
    ]


def test_the_default_counts_from_the_day_the_deal_came_in_not_the_edit(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """Anchored to ``created_at``: a default that moved with every save would relabel an
    untouched date as a person's choice."""
    deal = store_deal(db_session, without(team_entry, "closing_date"))
    db_session.execute(
        update(Deal)
        .where(Deal.id == deal.id)
        .values(created_at=datetime(2026, 9, 20, 12, tzinfo=UTC), closing_date=None)
    )
    db_session.commit()
    db_session.expire_all()
    again = db_session.get(Deal, deal.id)
    assert again is not None
    assert populate(again, CONFIG) == {"closing_date": "2026-10-31"}
    assert again.closing_date == date(2026, 10, 31)
    assert populate(again, CONFIG) == {}  # idempotent, whatever today is


# --- the page and the checklist -------------------------------------------------------------------


def test_the_deal_page_tags_the_default_and_offers_a_reset(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, without(team_entry, "closing_date"))
    body = client.get(f"/queue/deals/{deal.id}").text
    shown = expected_default(deal).isoformat()
    row = re.search(r"<dt>Closing Date.*?</dt>\s*<dd>([^<]*)(.*?)</dd>", body, re.S)
    assert row is not None and row.group(1).strip() == shown
    assert 'class="dflt">default</span>' in row.group(2)
    assert re.search(rf'id="closing_date"[^>]*data-default="{shown}"', body)
    assert re.search(rf'id="closing_date"[^>]*value="{shown}"', body)
    assert 'data-reset="closing_date"' in body


def test_readiness_never_blocks_on_the_closing_date(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A deal nobody has populated yet: the checklist says DEFAULT, and the assembly agrees."""
    deal_with_overrides.closing_date = None
    db_session.flush()
    readiness = underwrite_readiness(deal_with_overrides)
    assert readiness.ready and readiness.missing == []
    row = {row.key: row for row in readiness.rows}["deal.closing_date"]
    assert row.source is InputSource.DEFAULT and row.required
    assert row.value == expected_default(deal_with_overrides)
    assembled = underwrite_inputs(deal_with_overrides, UnderwriteRequest())
    assert assembled.closing_date == row.value


def test_the_term_is_the_one_thing_that_still_turns_the_ledger_off(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.closing_date = None
    deal_with_overrides.term_months = None
    db_session.commit()
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert "The ledger is off until these are entered" in body
    assert "Closing date" not in body[body.index("The ledger is off") :][:400]
    assert "Term" in body[body.index("The ledger is off") :][:400]
