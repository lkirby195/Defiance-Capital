"""The borrower's page: who they are, every deal they brought, the totals.  # SPEC §9.1 (v0.20)

Two deals under one phone are one borrower (``db/repository.find_or_create_borrower``), so
the page lists both with the Working version's verdict and IRR on each, and the foot adds
them up. The Home page and the deal header link to it from the borrower's name.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from sqlalchemy.orm import Session

from db.models import Deal
from services import BorrowerView, borrower_view, mark_dead
from tests.conftest import ACTOR, QueueClient, requires_db, store_deal
from tests.test_deal_page_layout import between, run

pytestmark = requires_db

SECOND_ADDRESS = "2210 E 11th St, Tulsa, OK 74104"


def second_deal(session: Session, payload: dict[str, Any]) -> Deal:
    """Another deal from the same borrower: the same phone, a different house and ask."""
    return store_deal(
        session,
        {
            **payload,
            "address": SECOND_ADDRESS,
            "purchase_price": "185000.00",
            "loan_requested": "140000.00",
            "estimated_sale_price_team": "245000.00",
        },
    )


def totals_row(body: str) -> list[str]:
    row = between(body, '<tr id="borrower-totals">', "</tr>")
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", row, re.S)
    ]


# --- the view -------------------------------------------------------------------------------------


def test_the_view_lists_every_deal_on_the_borrower_and_adds_them_up(
    client: QueueClient,
    db_session: Session,
    deal_with_overrides: Deal,
    team_entry_with_overrides: dict[str, Any],
) -> None:
    other = second_deal(db_session, team_entry_with_overrides)
    assert other.borrower_id == deal_with_overrides.borrower_id
    run(client, deal_with_overrides)
    run(client, other)
    db_session.expire_all()
    assert deal_with_overrides.borrower_id is not None
    view = borrower_view(db_session, deal_with_overrides.borrower_id)
    assert isinstance(view, BorrowerView)
    assert view.borrower.name == "Dana Whitlock" and view.entities == ["Whitlock Homes LLC"]
    assert view.count == 2 and view.live == 2
    # most recently touched first: the second deal ran last
    assert [entry.deal.id for entry in view.entries] == [other.id, deal_with_overrides.id]
    assert view.total_loan == Decimal("140000.00") + Decimal("120000.00")
    rates = [entry.irr for entry in view.entries if entry.irr is not None]
    assert len(rates) == 2
    assert view.average_irr == sum(rates, Decimal(0)) / 2
    assert all(entry.verdict is not None for entry in view.entries)
    # a dead deal stays on the list and in the totals; only "live" moves
    mark_dead(db_session, other.id, actor=ACTOR)
    db_session.commit()
    view = borrower_view(db_session, deal_with_overrides.borrower_id)
    assert view.count == 2 and view.live == 1
    assert view.total_loan == Decimal("260000.00")


def test_a_borrower_with_no_runs_has_no_irr_and_no_average(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    assert deal_with_overrides.borrower_id is not None
    view = borrower_view(db_session, deal_with_overrides.borrower_id)
    assert view.count == 1 and view.average_irr is None and view.total_loan == Decimal("120000.00")
    assert view.entries[0].verdict is None and view.entries[0].irr is None


# --- the page -------------------------------------------------------------------------------------


def test_the_page_shows_the_borrower_every_deal_and_the_totals(
    client: QueueClient,
    db_session: Session,
    deal_with_overrides: Deal,
    team_entry_with_overrides: dict[str, Any],
) -> None:
    other = second_deal(db_session, team_entry_with_overrides)
    run(client, deal_with_overrides)
    run(client, other)
    response = client.get(f"/borrowers/{deal_with_overrides.borrower_id}")
    assert response.status_code == 200, response.text
    body = response.text
    assert "<h1>Dana Whitlock</h1>" in body
    chips = between(body, '<div class="chips" id="borrower-chips">', "</div>")
    assert '<span class="tag fact" data-chip="entity">Whitlock Homes LLC</span>' in chips
    assert '<span class="tag fact" data-chip="phone">918-555-0147</span>' in chips
    assert '<span class="tag fact" data-chip="deals">2 deals</span>' in chips
    table = between(body, '<section class="panel chart" id="borrower-deals">', "</section>")
    assert f'href="/queue/deals/{other.id}"' in table and SECOND_ADDRESS in table
    assert f'href="/queue/deals/{deal_with_overrides.id}"' in table
    assert table.count('<span class="tag GO">Go</span>') == 2
    assert table.count("Underwriting") == 2
    assert len(re.findall(r'<span class="tag fact">\d+\.\d{2}%</span>', table)) == 2
    assert "$140,000.00" in table and "$120,000.00" in table
    # the totals at the foot: the count, the live count, the mean IRR, the sum
    cells = totals_row(body)
    assert cells[:3] == ["Total", "2 deals", "2 live"]
    assert re.fullmatch(r"\d+\.\d{2}%", cells[3])
    assert cells[4] == "$260,000.00"
    for column in ("Address", "Status", "Verdict", "Loan structure", "Last activity"):
        assert f"<th>{column}</th>" in table, column
    assert '<th class="num">IRR</th>' in table and '<th class="num">Loan amount</th>' in table


def test_home_rows_and_the_deal_header_link_to_the_borrower(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    borrower_id = deal_with_overrides.borrower_id
    home = client.get("/queue").text
    row = home[
        home.index(str(deal_with_overrides.id)) - 400 : home.index(str(deal_with_overrides.id))
    ]
    assert f'<a href="/borrowers/{borrower_id}"' in row and "Dana Whitlock" in row
    deal = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    chips = between(deal, '<div class="chips" id="deal-chips">', "</div>")
    assert (
        f'<a class="tag fact" data-chip="borrower" href="/borrowers/{borrower_id}"' in chips
        and ">Dana Whitlock</a>" in chips
    )
    # the page links back to each deal and to nothing else of the queue's
    page = client.get(f"/borrowers/{borrower_id}").text
    assert f'href="/queue/deals/{deal_with_overrides.id}"' in page
    assert '<form method="post"' not in between(page, "<main>", "</main>")


def test_an_unknown_borrower_goes_home(client: QueueClient) -> None:
    """A stranger is sent to sign in instead: ``tests/test_auth_routes.py`` holds the route
    to that with every other page."""
    missing = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/borrowers/{missing}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/queue")
    assert "does not exist" in unquote(response.headers["location"])
