"""The deal page's layout: outputs on top, inputs below, and the header chips.  # SPEC §9

The partners' complaint was that the useful information was buried. The page now opens on
the answer - the header's chips and the Pricing table - and puts what the run read
underneath; the CLI report and the workbook keep the SPEC §9 order. These tests hold the
page to its own order, analyzed or not, and to the header: one row of chips (borrower,
entity, loan request, IRR) and the phone, with the controls on the right. No (?) anywhere,
every IRR to two decimals. The same top bar is on every queue page; the borrower's has the
wordmark and the language toggle and nothing else.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from api.routes.queue import assumptions_form
from db.models import Deal
from tests.conftest import USER_EMAIL, USER_NAME, QueueClient, requires_db

pytestmark = requires_db

# The deal page, top to bottom, each section by the first thing that names it.
ORDER = (
    '<header class="deal-head">',
    '<div class="chips" id="deal-chips">',
    '<section class="panel" id="versions">',
    '<form class="run" method="post"',
    '<section class="panel chart" id="pricing">',
    '<div class="pair">',
    '<section class="panel chart" id="lender-sensitivity">',
    '<section class="panel chart" id="borrower-sensitivity">',
    '<div class="trio">',
    "<h2>Flip Analysis</h2>",
    "<h2>Rental Analysis</h2>",
    "<h2>Take-Back Analysis",
    "<h2>Return Overview</h2>",
    '<section class="panel" id="assumptions">',
    '<section class="panel" id="economics">',
    "<h2>Deal Economics and Property</h2>",
    '<section class="panel" id="actions">',
    '<details class="panel" id="court">',
    '<details class="panel" id="history">',
)

CHIPS = '<div class="chips" id="deal-chips">'


def page(client: QueueClient, deal: Deal) -> str:
    response = client.get(f"/queue/deals/{deal.id}")
    assert response.status_code == 200, response.text
    return response.text


def run(client: QueueClient, deal: Deal) -> None:
    response = client.post(f"/queue/deals/{deal.id}/run", follow_redirects=False)
    assert response.status_code == 303, response.text


def between(body: str, start: str, end: str) -> str:
    """The page from ``start`` up to the next ``end`` after it."""
    opening = body.index(start)
    return body[opening : body.index(end, opening)]


def positions(body: str) -> list[int]:
    found = [body.find(marker) for marker in ORDER]
    missing = [marker for marker, at in zip(ORDER, found, strict=True) if at < 0]
    assert not missing, missing
    return found


# --- the order ------------------------------------------------------------------------------------


def test_the_sections_come_in_the_same_order_analyzed_or_not(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    before = positions(page(client, deal_with_overrides))
    assert before == sorted(before) and len(set(before)) == len(before)
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    after = positions(body)
    assert after == sorted(after)
    for marker in ORDER:
        assert body.count(marker) == 1, marker
    # every output is above the first input: the panel and the intake summary come after
    # the ledger, and the collapsed furniture after those
    assert body.index("<h2>Return Overview</h2>") < body.index('id="assumptions"')
    assert body.index('id="economics"') < body.index('id="court"') < body.index('id="history"')


def test_the_sensitivities_share_a_row_and_the_analyses_another(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    pair = between(body, '<div class="pair">', '<div class="trio">')
    assert pair.count('<section class="panel chart"') == 2
    assert 'id="lender-sensitivity"' in pair and 'id="borrower-sensitivity"' in pair
    # the columns are the grid, 12.5% to 17.5%; this deal is priced at 13%, off it
    for table in pair.split('id="borrower-sensitivity"'):
        head = table[table.index("<thead>") : table.index("</thead>")]
        rates = re.findall(r'<th class="num">([\d.]+%)</th>', head)
        assert rates == ["12.5%", "13.5%", "14.5%", "15.5%", "16.5%", "17.5%"]
    assert "13.0%" not in pair and "hit" not in pair
    # a row is its loan amount, with no step under it
    assert "(\u2212$" not in pair and 'class="unit sub"' not in pair
    trio = between(body, '<div class="trio">', "<h2>Return Overview</h2>")
    assert trio.count('<section class="panel stat"') == 3
    # this deal has a sale price and a rent, so all three carry a figure
    assert trio.count('<p class="headline">') == 3
    assert '<span class="headline-label">Net profit</span>' in trio
    assert '<span class="headline-label">DSCR</span>' in trio
    assert '<span class="headline-label">DSCR at loan cost</span>' in trio


def test_an_unevaluated_analysis_is_a_state_not_an_error(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """Flip off, rental on with no rent: the three cards say so quietly and keep their lines."""
    form = {name: value for name, value in assumptions_form(deal_with_overrides).items() if value}
    form.pop("monthly_rent", None)
    form["flip_analysis"] = "false"
    form["rental_analysis"] = "true"  # left at Default it would be Off without a rent
    saved = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions", data=form, follow_redirects=False
    )
    assert saved.status_code == 303, saved.text
    body = page(client, deal_with_overrides)
    trio = between(body, '<div class="trio">', "<h2>Return Overview</h2>")
    flip = between(trio, 'id="flip"', 'id="rental"')
    rental = between(trio, 'id="rental"', 'id="take-back"')
    take_back = trio[trio.index('id="take-back"') :]
    assert '<p class="headline na">Off</p>' in flip
    assert '<p class="headline na">Not evaluated</p>' in rental
    assert '<p class="headline na">Not evaluated</p>' in take_back
    assert '<p class="headline">' not in trio
    assert 'class="problem"' not in trio and 'class="banner"' not in trio
    for card in (flip, rental, take_back):
        assert "<dt>Status" in card and '<dl class="facts small">' in card


def test_a_deal_on_the_grid_has_its_cell_marked_on_both_tables(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """Left at the default rate, 12.5%, the deal is the first column's bottom row."""
    form = {name: value for name, value in assumptions_form(deal_with_overrides).items() if value}
    form.pop("interest_rate")  # blank: the default comes back
    saved = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions", data=form, follow_redirects=False
    )
    assert saved.status_code == 303, saved.text
    body = page(client, deal_with_overrides)
    for section in ("lender-sensitivity", "borrower-sensitivity"):
        table = between(body, f'id="{section}"', "</section>")
        assert table.count('class="num grid-cell hit"') == 1, section
        bottom = table[table.rindex("<tr>") :]
        assert bottom.index('class="num grid-cell hit"') < bottom.index('class="num grid-cell"')


def test_the_cash_flows_are_folded_under_one_line_of_figures(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    overview = between(body, "<h2>Return Overview</h2>", "</section>")
    keys = [key.strip() for key in re.findall(r'<span class="k">([^<]*)', overview)]
    assert keys == ["IRR", "Total interest", "Total fees", "Total profit", "Payoff date"]
    assert overview.index('<ul class="summary">') < overview.index('<details class="ledger chart">')
    # nothing between the figures and the fold: the run line is gone
    gap = between(overview, "</ul>", '<details class="ledger chart">')
    assert gap.strip() == "</ul>"
    assert "engine" not in overview and "rehab month(s)" not in overview
    ledger = overview[overview.index('<details class="ledger chart">') :]
    assert "<summary>Cash Flows</summary>" in ledger and "Ledger" not in ledger
    assert "Future Draws" in ledger and "<tfoot>" in ledger
    assert '<details class="ledger chart" open>' not in overview


def test_no_definitions_and_every_irr_to_two_decimals(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    content = body[body.index('<header class="deal-head">') :]
    assert 'class="help"' not in body and ">?</span>" not in content
    # every percentage on the page has at most two decimals
    assert not re.search(r"\d\.\d{3,}%", content), re.findall(r"[\d.]+\d{3}%", content)
    pricing = between(body, 'id="pricing"', "</section>")
    assert re.search(r'<td class="num">\d+\.\d{2}%</td>', pricing)
    history = between(body, 'id="history"', "</details>")
    assert re.search(r"IRR \d+\.\d{2}%", history)
    # the checklist section is gone: the panel is the one list of what the ledger runs on
    assert "What the ledger runs on" not in body and 'id="readiness"' not in body


# --- the header -----------------------------------------------------------------------------------


def test_the_header_carries_the_chips(client: QueueClient, deal_with_overrides: Deal) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    chips = between(body, CHIPS, "</div>")
    assert '<span class="tag fact" data-chip="borrower">Dana Whitlock</span>' in chips
    assert '<span class="tag fact" data-chip="entity">Whitlock Homes LLC</span>' in chips
    # the request in whole dollars
    assert '<span class="tag fact" data-chip="loan-amount">Loan Request: $120,000</span>' in chips
    irr = re.search(r'data-chip="irr">IRR <strong>(\d+\.\d{2}%)</strong>', chips)
    assert irr is not None
    # those four, in the brief's order, and nothing else: no loan type, term, verdict, status
    assert re.findall(r'data-chip="([^"]+)"', chips) == ["borrower", "entity", "loan-amount", "irr"]
    assert chips.count('<span class="tag') == 4
    assert "Wholetail" not in chips and "month(s)" not in chips
    assert '<span class="tag GO">' not in chips and "UNDERWRITING" not in chips
    assert 'id="working-version"' not in body and 'id="draft-badge"' not in chips
    # the IRR is the version in view's: the Return Overview shows the same
    overview = between(body, "<h2>Return Overview</h2>", "</section>")
    assert f'<span class="v">{irr.group(1)}</span>' in overview
    # the line under the chips is the phone and nothing else; no run line under that
    ident = between(body, '<div class="deal-id">', '<div class="deal-tools">')
    assert re.findall(r'<p class="meta"[^>]*>(.*?)</p>', ident, re.S) == ["918-555-0147"]
    for gone in ("entered ", "via TEAM", 'id="run-line"', "Verdict <strong>", "engine "):
        assert gone not in ident, gone
    # above the first output
    assert body.index(CHIPS) < body.index('id="pricing"')


def test_no_entity_no_chip_and_no_phone_no_line(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    borrower = deal_with_overrides.borrower
    assert borrower is not None
    borrower.entities.clear()
    borrower.phone = None
    db_session.commit()
    body = page(client, deal_with_overrides)
    chips = between(body, CHIPS, "</div>")
    assert 'data-chip="entity"' not in chips
    ident = between(body, '<div class="deal-id">', '<div class="deal-tools">')
    assert '<p class="meta"' not in ident


def test_the_chips_say_what_is_not_known_yet(client: QueueClient, stored_deal: Deal) -> None:
    body = page(client, stored_deal)
    chips = between(body, CHIPS, "</div>")
    assert 'data-chip="irr">IRR <strong>—</strong>' in chips
    assert not re.search(r'<span class="tag (GO|CONDITIONAL|DECLINE)">', chips)
    assert "No versions yet" in body and "No analysis yet." in body
    # the Actions card says where the deal stands, with no verdict yet
    actions = between(body, '<section class="panel" id="actions">', "</section>")
    assert '<p class="small muted" id="deal-standing">Status: New · Verdict: —</p>' in actions


def test_the_actions_card_opens_on_the_status_and_the_verdict(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """Small text atop the card, above Progress / Pause / Kill; the header is unchanged."""
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    actions = between(body, '<section class="panel" id="actions">', "</section>")
    line = '<p class="small muted" id="deal-standing">Status: Underwriting · Verdict: Go</p>'
    assert line in actions
    assert actions.index("<h2>Actions</h2>") < actions.index(line) < actions.index(">Progress<")
    assert actions.index(">Progress<") < actions.index(">Pause<") < actions.index(">Kill<")
    # the line is the card's alone: the header carries neither
    head = between(body, '<header class="deal-head">', "</header>")
    assert "Underwriting" not in head and "Status:" not in head and "Verdict" not in head


def test_the_header_holds_the_controls(client: QueueClient, deal_with_overrides: Deal) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    head = between(body, '<header class="deal-head">', "</header>")
    assert '<section class="panel" id="versions">' in head
    assert 'class="actions switcher"' in head and 'id="download-excel"' in head
    assert ">Restore inputs</button>" in head
    # the version's facts are the History's, not the header's
    versions = between(head, '<section class="panel" id="versions">', "</section>")
    for gone in ("Chosen by a person", "<dl", "<dt>Run</dt>", "<dt>Engine</dt>", "config "):
        assert gone not in versions, gone
    assert ">Run Analysis</button>" in head and 'id="run_version_name"' in head
    assert f'href="/queue/deals/{deal_with_overrides.id}/intake">Edit Intake</a>' in head
    assert head.index('id="versions"') < head.index(">Run Analysis</button>")
    assert head.index(">Run Analysis</button>") < head.index(">Edit Intake</a>")
    # the intake summary offers the same link, beside its title
    economics = between(body, '<section class="panel" id="economics">', "</section>")
    assert f'href="/queue/deals/{deal_with_overrides.id}/intake">Edit Intake</a>' in economics


# --- the top bar ----------------------------------------------------------------------------------

# The wordmark is the logo (static/brand, docs/brand.md), on the queue's bar and the borrower's.
LOGO = '<span class="brand"><img src="/static/brand/logo-bar.png" alt="Defiance Capital"'


def test_the_top_bar_is_the_same_on_every_page(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    deal = f"/queue/deals/{deal_with_overrides.id}"
    for path in ("/queue", "/queue/new", deal, f"{deal}/intake", "/defaults", "/account/password"):
        body = client.get(path).text
        bar = between(body, '<header class="bar">', "</header>")
        assert LOGO in bar, path
        for link in (
            '<a class="btn" href="/queue">Home</a>',
            '<a class="btn" href="/queue/new">New Deal</a>',
            '<a class="btn" href="/defaults">Defaults</a>',
        ):
            assert link in bar, (path, link)
        assert f'<span class="name" title="{USER_EMAIL}">{USER_NAME}</span>' in bar, path
        assert (
            bar.index(">Home</a>")
            < bar.index(USER_NAME)
            < bar.index(">Change password</a>")
            < bar.index(">Sign out</button>")
        ), path
        assert "GLENWOOD" not in bar, path


def test_the_borrower_form_s_bar_holds_the_wordmark_and_the_toggle_only(
    anon_client: QueueClient,
) -> None:
    for path in ("/apply", "/apply?lang=es", "/apply/thanks"):
        body = anon_client.get(path).text
        bar = between(body, '<header class="bar">', "</header>")
        assert LOGO in bar, path
        assert 'class="lang"' in bar, path
        assert "<nav" not in bar and 'href="/queue' not in bar and "Sign out" not in bar, path
    # one column: the borrower's main is the narrow one, and the boxes say which they are in
    # the same words the team form uses
    body = anon_client.get("/apply").text
    assert '<main class="apply">' in body
    assert '<span class="mark">Required</span>' in body
    assert '<span class="mark">Optional</span>' in body
    assert 'class="req"' not in body and 'class="opt"' not in body


def test_the_logo_is_served_to_anybody(anon_client: QueueClient) -> None:
    response = anon_client.get("/static/brand/logo-bar.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(bytes([137, 80, 78, 71]))


# --- Home -----------------------------------------------------------------------------------------


def test_home_rows_carry_chips_and_an_empty_section_says_so_in_one_line(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = client.get("/queue").text
    row = body[body.index(str(deal_with_overrides.id)) :]
    row = row[: row.index("</tr>")]
    assert '<span class="tag GO">Go</span>' in row
    # the verdict chip and nothing else: no version name beside it, no status chip
    assert "Auto v" not in row and 'title="The Working version"' not in row
    assert 'class="tag status"' not in row
    # the IRR to two decimals
    assert re.search(r'<td class="num"><span class="tag fact">\d+\.\d{2}%</span></td>', row)
    # no Open button: the address is the link, then the three controls in order
    assert ">Open<" not in row
    assert row.index(">Progress</button>") < row.index(">Pause</button>") < row.index(">Kill<")
    # three of the four sections are empty on this database
    assert body.count('<p class="small muted" style="margin:0">None.</p>') == 3
    assert body.count('<span class="count">') == 4
