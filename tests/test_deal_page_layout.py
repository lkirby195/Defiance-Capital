"""The deal page's layout: outputs on top, inputs below, and the header chips.  # SPEC §9

The partners' complaint was that the useful information was buried. The page now opens on
the answer - the header's chips and the Pricing table - and puts what the run read
underneath; the CLI report and the workbook keep the SPEC §9 order. These tests hold the
page to its own order, analyzed or not, and to the header: one row of chips (borrower, loan
type, loan amount, term, verdict, IRR) with the controls on the right. The same top bar is
on every queue page; the borrower's has the wordmark and the language toggle and nothing
else.
"""

from __future__ import annotations

import re

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
    '<details class="panel chart" id="readiness">',
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
    assert pair.count('class="num grid-cell hit"') == 2  # the deal's own cell, on each
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


def test_the_ledger_is_folded_under_one_line_of_figures(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    overview = between(body, "<h2>Return Overview</h2>", "</section>")
    keys = [key.strip() for key in re.findall(r'<span class="k">([^<]*)', overview)]
    assert keys == ["IRR", "Total interest", "Total fees", "Total profit", "Payoff date"]
    assert overview.index('<ul class="summary">') < overview.index('<details class="ledger chart">')
    ledger = overview[overview.index('<details class="ledger chart">') :]
    assert "Future Draws" in ledger and "<tfoot>" in ledger
    assert '<details class="ledger chart" open>' not in overview


# --- the header -----------------------------------------------------------------------------------


def test_the_header_carries_the_chips(client: QueueClient, deal_with_overrides: Deal) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    chips = between(body, CHIPS, "</div>")
    assert '<span class="tag fact" data-chip="borrower">Dana Whitlock</span>' in chips
    assert '<span class="tag fact" data-chip="loan-type">Wholetail</span>' in chips
    assert '<span class="tag fact" data-chip="loan-amount">$120,000.00</span>' in chips
    assert '<span class="tag fact" data-chip="term">6 month(s)</span>' in chips
    assert '<span class="tag GO">Go</span>' in chips
    irr = re.search(r'data-chip="irr">IRR <strong>(\d+\.\d{4}%)</strong>', chips)
    assert irr is not None
    # in the brief's order: borrower, loan type, loan amount, term, verdict, IRR
    facts = ("borrower", "loan-type", "loan-amount", "term")
    at = [chips.index(f'data-chip="{name}"') for name in facts]
    assert at == sorted(at)
    assert at[-1] < chips.index('<span class="tag GO">') < chips.index('data-chip="irr"')
    # the IRR is the version in view's: the run line and the Return Overview show the same
    assert f"· IRR <strong>{irr.group(1)}</strong>" in body
    overview = between(body, "<h2>Return Overview</h2>", "</section>")
    assert f'<span class="v">{irr.group(1)}</span>' in overview
    # above the first output
    assert body.index(CHIPS) < body.index('id="pricing"')


def test_the_chips_say_what_is_not_known_yet(client: QueueClient, stored_deal: Deal) -> None:
    body = page(client, stored_deal)
    chips = between(body, CHIPS, "</div>")
    assert 'data-chip="irr">IRR <strong>—</strong>' in chips
    assert not re.search(r'<span class="tag (GO|CONDITIONAL|DECLINE)">', chips)
    assert "Not analyzed yet." in body


def test_the_header_holds_the_controls(client: QueueClient, deal_with_overrides: Deal) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    head = between(body, '<header class="deal-head">', "</header>")
    assert '<section class="panel" id="versions">' in head
    assert 'class="actions switcher"' in head and 'id="download-excel"' in head
    assert "<dt>Chosen by a person" in head and ">Restore inputs</button>" in head
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
