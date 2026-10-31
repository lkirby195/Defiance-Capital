"""The deal page's six tabs: which section sits under which, and the hash that keeps the
active one across a reload.  # SPEC §9 (v0.20)

The tabs are script, and the test client runs none - so what is tested here is the contract
the script works from: one link per panel, each link's hash naming its panel, every panel
on the page (none hidden by the server, so a browser with script off sees them all in
order), and the script itself reading the hash, writing it back and listening for it to
change. The membership is tested on the markup: each panel holds its sections and nothing
of another's.
"""

from __future__ import annotations

import re

from api.routes.queue import ASSUMPTION_NAMES
from db.models import Deal
from tests.conftest import QueueClient, requires_db
from tests.test_deal_page_layout import between, page, run

pytestmark = requires_db

TABS = (
    ("tab-summary", "Summary"),
    ("tab-overview", "Deal Overview"),
    ("tab-cash-flows", "Cash Flows"),
    ("tab-sources-uses", "Sources &amp; Uses"),
    ("tab-scenarios", "Scenarios"),
    ("tab-assumptions", "Assumptions"),
)

# What each panel holds, by the first thing that names each section, in order.
MEMBERS: dict[str, tuple[str, ...]] = {
    "tab-summary": (
        '<section class="panel" id="return-overview">',
        '<section class="panel chart" id="pricing">',
        '<div class="pair">',
        '<section class="panel chart" id="lender-sensitivity">',
        '<section class="panel chart" id="borrower-sensitivity">',
        '<section class="panel chart" id="leverage">',
        "<h2>Leverage against caps</h2>",
        '<section class="panel" id="actions">',
    ),
    "tab-overview": (
        '<section class="panel chart" id="economics">',
        "<h2>Deal Economics and Property</h2>",
        '<dl class="kv" id="intake-facts">',
        '<div class="kv-section">Overview</div>',
        '<div class="kv-section">About their investing</div>',
        '<div class="kv-section">Property Overview</div>',
        '<div class="kv-section">Deal Economics</div>',
        "<h3>Still needed from the borrower</h3>",
        '<section class="panel chart" id="market-data">',
        "<h2>Market data</h2>",
        '<section class="panel chart" id="scored-as">',
        "<h2>Scored as</h2>",
        '<details class="panel" id="court">',
    ),
    "tab-cash-flows": ('<section class="panel chart" id="cash-flows">', "<h2>Cash Flows</h2>"),
    "tab-sources-uses": ('<section class="panel chart" id="sources-uses">',),
    "tab-scenarios": (
        '<div class="trio">',
        "<h2>Flip Analysis</h2>",
        "<h2>Rental Analysis</h2>",
        "<h2>Take-Back Analysis",
    ),
    "tab-assumptions": (
        '<section class="panel" id="assumptions">',
        "<h2>Underwriting Assumptions</h2>",
        '<div class="kv-grid structure" id="assumption-rows">',
        ">Save &amp; Run</button>",
        ">Reset all to defaults</a>",
        '<section class="panel chart" id="history">',
        "<h2>Versions</h2>",
        "<h3>Versions and edits</h3>",
        "<h3>Actions and notes</h3>",
    ),
}

PANEL = re.compile(r'<div class="tab-panel" id="(tab-[a-z-]+)" data-tab="(tab-[a-z-]+)"(.*?)>')


def panels(body: str) -> dict[str, str]:
    """Each panel's markup, by its id, in page order."""
    starts = [(m.start(), m.group(1)) for m in PANEL.finditer(body)]
    out: dict[str, str] = {}
    for index, (at, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else body.index("<script", at)
        out[name] = body[at:end]
    return out


def tab_row(body: str) -> str:
    return between(body, '<nav class="tabs" id="deal-tabs"', "</nav>")


# --- the tabs and their panels --------------------------------------------------------------------


def test_six_tabs_in_order_each_naming_its_panel(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    row = tab_row(body)
    links = re.findall(r'<a class="tab" href="#([a-z-]+)" data-tab="([a-z-]+)">([^<]+)</a>', row)
    assert [(href, name) for href, name, _ in links] == [(key, key) for key, _ in TABS]
    assert [label for _, _, label in links] == [label for _, label in TABS]
    found = panels(body)
    assert list(found) == [key for key, _ in TABS]
    # the row sits under the header and above the first panel
    assert body.index("</header>") < body.index('id="deal-tabs"') < body.index('id="tab-summary"')
    # nothing is hidden by the server: a browser with script off sees every panel, in order
    for match in PANEL.finditer(body):
        assert "hidden" not in match.group(3), match.group(1)
    for key, _ in TABS:
        assert body.count(f'id="{key}"') == 1, key


def test_each_panel_holds_its_sections_and_nothing_of_another_s(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    found = panels(body)
    for key, markers in MEMBERS.items():
        panel = found[key]
        at = [panel.find(marker) for marker in markers]
        missing = [m for m, i in zip(markers, at, strict=True) if i < 0]
        assert not missing, (key, missing)
        assert at == sorted(at), key
        for other, other_markers in MEMBERS.items():
            if other == key:
                continue
            for marker in other_markers:
                assert marker not in panel, (key, other, marker)
    # the whole of the content after the tab row is the six panels and the script
    tabs_end = body.index("</nav>", body.index('id="deal-tabs"')) + len("</nav>")
    after = body[tabs_end:]
    gaps = PANEL.split(after)
    assert gaps[0].strip() == ""
    # every section the page has is inside one panel: nothing of substance between them
    for key in MEMBERS:
        assert body.count(f'data-tab="{key}"') == 2, key  # the link and the panel


def test_the_same_panels_analyzed_or_not(client: QueueClient, deal_with_overrides: Deal) -> None:
    before = panels(page(client, deal_with_overrides))
    assert list(before) == [key for key, _ in TABS]
    for key in ("tab-summary", "tab-cash-flows", "tab-sources-uses", "tab-scenarios"):
        assert "No analysis yet." in before[key], key
    assert '<section class="panel chart" id="cash-flows">' in before["tab-cash-flows"]
    run(client, deal_with_overrides)
    after = panels(page(client, deal_with_overrides))
    assert list(after) == list(before)
    assert "No analysis yet." not in after["tab-cash-flows"]


# --- the hash -------------------------------------------------------------------------------------


def test_the_active_tab_is_the_url_hash(client: QueueClient, deal_with_overrides: Deal) -> None:
    """The script reads the hash on load, writes it on a click, and follows it when it
    changes; and the only script on the page beside the masks is that one."""
    body = page(client, deal_with_overrides)
    scripts = re.findall(r"<script>(.*?)</script>", body, re.S)
    tabs = [script for script in scripts if "#deal-tabs" in script]
    assert len(tabs) == 1 and len(scripts) == 2  # the masks, and the tabs
    script = tabs[0]
    assert "window.location.hash" in script
    assert 'addEventListener("hashchange"' in script
    assert "history.replaceState" in script
    assert "panel.hidden = panel.dataset.tab !== name" in script
    assert 'classList.toggle("on", on)' in script
    # it decides nothing: no fetch, no form, no validation
    for forbidden in ("fetch(", "XMLHttpRequest", "submit(", "setCustomValidity", "localStorage"):
        assert forbidden not in script, forbidden
    # the script sits inside the page's content, after the last panel
    assert body.index("<script>" + script) > body.index('id="tab-assumptions"')
    # every link's hash is a panel id, so the hash a person lands with names a real tab
    hrefs = re.findall(r'<a class="tab" href="#([a-z-]+)"', tab_row(body))
    ids = re.findall(r'<div class="tab-panel" id="([a-z-]+)"', body)
    assert hrefs == ids and len(set(ids)) == len(ids)


# --- the grids ------------------------------------------------------------------------------------


def test_the_overview_is_one_grid_under_section_rules(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    run(client, deal_with_overrides)
    body = page(client, deal_with_overrides)
    grid = between(body, '<dl class="kv" id="intake-facts">', "</dl>")
    rules = re.findall(r'<div class="kv-section">([^<]+)</div>', grid)
    assert rules == ["Overview", "About their investing", "Property Overview", "Deal Economics"]
    # label/value pairs, and the three old columns are gone
    assert grid.count("<dt>") == grid.count("<dd") and grid.count("<dt>") > 30
    assert 'class="facts-grid"' not in body and "<h3>Overview</h3>" not in body
    # the guarantor's name links to the borrower's page
    borrower = deal_with_overrides.borrower
    assert borrower is not None
    assert f'<dt>Guarantor Name</dt>\n    <dd><a href="/borrowers/{borrower.id}">' in grid
    # the sizing table and the score components left the card for their own
    assert "Leverage against caps" not in between(body, 'id="economics"', "</section>")
    assert "Scored as" not in between(body, 'id="economics"', "</section>")


def test_the_panel_is_rows_of_label_then_box_with_the_tag_at_the_edge(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    body = page(client, deal_with_overrides)
    grid = between(body, '<div class="kv-grid structure" id="assumption-rows">', "</form>")
    rows = re.findall(r'<div class="kv-row" data-box="([a-z_]+)">', grid)
    assert rows == [*ASSUMPTION_NAMES, "version_name"]
    for name in ASSUMPTION_NAMES:
        row = between(grid, f'<div class="kv-row" data-box="{name}">', "</div>\n  </div>")
        assert row.index("<label") < row.index('<div class="v">'), name
        if name == "product":
            assert 'role="radiogroup"' in row
            continue
        assert re.search(rf'<input id="{name}" name="{name}"', row), name
        tag = re.search(
            rf'<span class="dflt" data-default-tag="{name}"( hidden)?>default</span>', row
        )
        reset = row.find(f'data-reset="{name}"')
        if tag is not None:
            # the reset link, then the tag, after the box
            assert row.index(f'id="{name}"') < reset < tag.start(), name
        else:
            assert reset < 0, name
    # the rehab portion row is the one the Holdback radio reveals
    assert '<div class="holdback-only">' in grid
    assert 'class="grid2' not in grid
