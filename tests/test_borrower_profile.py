"""The borrower's investing profile: the five "About your investing" answers.  # SPEC §4.1 (10c)

Both intake forms ask them, every one optional; the answers live on the ``borrowers`` row
(migration ``0023``) because they describe the person, so a repeat borrower's latest answers
are what every deal of theirs reads. They are shown on the deal page's Deal Overview tab and
on the borrower's page, Edit Intake carries them and audits a change, and Home narrows by
them through the filter chips the search box grew in 10b. Nothing in the engine reads them.

What is under test: the labels (a stored code is never what a person reads), the two forms
(the group, the order, the Optional marks, the Spanish), the write (on the borrower, not the
deal; a blank box leaves the stored answer; the latest answer wins), the two pages, the edit
and its audit row, and the filter - the pure half against ``home_view`` and the page half on
the markup.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.apply_form import REQUIRED_FIELDS
from api.ratelimit import LIMITER
from db.models import AuditLog, Borrower, Deal
from schema.generate import INTAKE_JSON
from schema.labels import PROFILE_CAPTIONS, PROFILE_LABELS, profile_caption, profile_label
from schema.models import (
    BORROWER_PROFILE_FIELDS,
    PROFILE_ENUMS,
    AuditAction,
    ProjectsInProgress,
    Strategy,
    WorkCrew,
    WorkStatus,
    YearsInvesting,
)
from services import HomeFilter, Section, chips_from, home_view
from services.home import chip_matches
from tests.conftest import QueueClient, form_body, requires_db, store_deal
from tests.test_apply import post
from tests.test_borrower_names import overview_value
from tests.test_deal_page_layout import between
from tests.test_form_errors import field_error


@pytest.fixture(autouse=True)
def fresh_limiter() -> Any:
    """Every test starts with nobody having posted to /apply (the per-IP limiter)."""
    LIMITER.reset()
    yield
    LIMITER.reset()


pytestmark = requires_db

# One full set of answers, as either form posts them: the stored codes.
PROFILE: dict[str, str] = {
    "work_status": "PART_TIME",
    "years_investing": "2_5",
    "projects_in_progress": "2_3",
    "strategy": "MIX",
    "work_crew": "GC",
}
# ...and how the team reads them back, in the same order.
READS: dict[str, str] = {
    "Full-time in real estate": "Part-time",
    "Years investing": "2–5",
    "Projects in progress": "2–3",
    "Mostly does": "Mix",
    "Who does the work": "GC",
}

SECOND_BORROWER: dict[str, Any] = {
    "borrower_name": "Priya Natarajan",
    "borrower_phone": "9185550102",
    "borrower_email": "priya@natarajan.example",
    "entity_name": "Natarajan Holdings LLC",
    "address": "915 N Denver Ave, Tulsa, OK 74106",
}


def the_borrower(session: Session) -> Borrower:
    (borrower,) = session.scalars(select(Borrower)).all()
    return borrower


def audit_rows(session: Session, action: AuditAction) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.action == action.value)))


# --- the labels -----------------------------------------------------------------------------------


def test_every_answer_has_words_and_every_question_a_caption() -> None:
    """A stored code is never what a person reads (CLAUDE.md); the five enums share no value,
    so one table labels them all."""
    assert tuple(PROFILE_CAPTIONS) == BORROWER_PROFILE_FIELDS
    assert set(PROFILE_ENUMS) == set(BORROWER_PROFILE_FIELDS)
    values = [member.value for kind in PROFILE_ENUMS.values() for member in kind]
    assert len(values) == len(set(values))
    assert set(PROFILE_LABELS) == set(values)
    assert profile_label(WorkStatus.FIRST_PROJECT) == "This is my first project"
    assert profile_label("2_5") == "2–5" and profile_label(YearsInvesting.TEN_PLUS) == "10+"
    assert profile_label(ProjectsInProgress.FOUR_PLUS) == "4+"
    assert (
        profile_label(Strategy.MIX) == "Mix" and profile_label(WorkCrew.MYSELF) == "Mostly myself"
    )
    assert profile_label(None) == "—"
    assert profile_caption("years_investing") == "Years investing"
    with pytest.raises(KeyError):
        profile_label("SOMETIMES")


def test_the_committed_schema_carries_the_profile() -> None:
    schema = json.loads(INTAKE_JSON.read_text(encoding="utf-8"))
    properties = schema["$defs"]["BorrowerInfo"]["properties"]
    for field in BORROWER_PROFILE_FIELDS:
        assert field in properties, field
    assert schema["$defs"]["YearsInvesting"]["enum"] == ["0_1", "2_5", "6_10", "10_PLUS"]
    assert schema["$defs"]["WorkStatus"]["enum"] == ["FULL_TIME", "PART_TIME", "FIRST_PROJECT"]


# --- the team form --------------------------------------------------------------------------------


def test_the_team_form_asks_the_five_questions_after_the_overview(client: QueueClient) -> None:
    body = client.get("/queue/new").text
    group = between(body, '<section class="panel" id="about-investing">', "</section>")
    assert "<h2>About your investing</h2>" in group
    selects = re.findall(r'<select id="([a-z_]+)" name="([a-z_]+)"', group)
    assert [name for name, _ in selects] == list(BORROWER_PROFILE_FIELDS)
    assert all(box == name for box, name in selects)
    for field, caption in PROFILE_CAPTIONS.items():
        assert (
            f'<label for="{field}">{caption} <span class="mark">Optional</span></label>' in group
        ), field
    # the options post the code and show the words
    assert re.search(r'value="FIRST_PROJECT"[^>]*>\s*This is my first project\s*<', group)
    assert re.search(r'value="2_5"[^>]*>\s*2–5\s*<', group)
    assert re.search(r'value="MYSELF"[^>]*>\s*Mostly myself\s*<', group)
    for code in ("FIRST_PROJECT", "2_5", "4_PLUS", "OWN_CREW", "MIX"):
        assert f">{code}<" not in group, code
    assert " required" not in group
    # between the Overview and the Property Overview
    assert (
        body.index("<h2>Overview</h2>")
        < body.index("<h2>About your investing</h2>")
        < body.index("<h2>Property Overview</h2>")
    )


def test_a_team_deal_stores_the_answers_on_the_borrower(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    response = client.post(
        "/intake/team", data=form_body(team_entry, **PROFILE), follow_redirects=False
    )
    assert response.status_code == 303, response.text
    (deal,) = db_session.scalars(select(Deal)).all()
    borrower = the_borrower(db_session)
    assert deal.borrower is borrower
    assert borrower.work_status is WorkStatus.PART_TIME
    assert borrower.years_investing is YearsInvesting.TWO_TO_FIVE
    assert borrower.projects_in_progress is ProjectsInProgress.TWO_TO_THREE
    assert borrower.strategy is Strategy.MIX
    assert borrower.work_crew is WorkCrew.GC
    # on the row, not the deal: the deal has no such columns
    for field in BORROWER_PROFILE_FIELDS:
        assert not hasattr(Deal, field), field
    # the immutable submission keeps the codes
    assert deal.submissions[-1].raw_payload["strategy"] == "MIX"
    assert deal.submissions[-1].raw_payload["years_investing"] == "2_5"


def test_the_deal_page_and_the_borrower_page_read_the_answers(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    answered = store_deal(db_session, {**team_entry, **PROFILE})
    unanswered = store_deal(db_session, {**team_entry, **SECOND_BORROWER})

    page = client.get(f"/queue/deals/{answered.id}").text
    facts = between(page, '<dl class="kv" id="intake-facts">', "</dl>")
    assert '<div class="kv-section">About their investing</div>' in facts
    assert facts.index("Repeat borrower") < facts.index("About their investing")
    assert facts.index("About their investing") < facts.index("Property Overview")
    for caption, words in READS.items():
        assert overview_value(page, caption) == words, caption
    for code in PROFILE.values():
        if PROFILE_LABELS[code] != code:  # "GC" reads as GC
            assert f">{code}<" not in facts, code

    blank = client.get(f"/queue/deals/{unanswered.id}").text
    for caption in READS:
        assert overview_value(blank, caption) == "—", caption

    own = client.get(f"/borrowers/{answered.borrower_id}").text
    card = between(own, '<section class="panel chart" id="borrower-profile">', "</section>")
    assert "<h2>About their investing</h2>" in card
    for caption, words in READS.items():
        assert f"<dt>{caption}</dt><dd>{words}</dd>" in card, caption
    # the profile card sits between the header and the deals
    assert own.index('id="borrower-chips"') < own.index('id="borrower-profile"')
    assert own.index('id="borrower-profile"') < own.index('id="borrower-deals"')


# --- Edit Intake ----------------------------------------------------------------------------------


def edit(client: QueueClient, deal: Deal, body: dict[str, str]) -> Any:
    response = client.post(f"/queue/deals/{deal.id}/intake", data=body, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def test_edit_intake_carries_the_answers_and_audits_a_change(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any], stored_deal: Deal
) -> None:
    # nothing answered yet: no option is selected on any of the five
    body = client.get(f"/queue/deals/{stored_deal.id}/intake").text
    group = between(body, '<section class="panel" id="about-investing">', "</section>")
    assert " selected" not in group

    edit(client, stored_deal, form_body(team_entry, **PROFILE))
    db_session.expire_all()
    borrower = the_borrower(db_session)
    assert borrower.strategy is Strategy.MIX and borrower.work_crew is WorkCrew.GC
    [row] = audit_rows(db_session, AuditAction.INTAKE_EDITED)
    after = dict(row.after or {})
    after.pop("submission_id")
    assert after == {f"borrower.{field}": value for field, value in PROFILE.items()}
    assert row.before == {f"borrower.{field}": None for field in PROFILE}

    # the form now shows them picked, as the box takes them back
    body = client.get(f"/queue/deals/{stored_deal.id}/intake").text
    group = between(body, '<section class="panel" id="about-investing">', "</section>")
    for value in PROFILE.values():
        assert f'value="{value}" selected' in group, value

    # posted back unchanged: nothing on the profile moves, nothing is audited for it
    edit(client, stored_deal, form_body(team_entry, **PROFILE))
    db_session.expire_all()
    rows = audit_rows(db_session, AuditAction.INTAKE_EDITED)
    assert len(rows) == 2
    assert not any(key.startswith("borrower.") for key in (rows[-1].after or {}))

    # a blank box leaves the stored answer; one answer changed moves that one alone
    edit(client, stored_deal, form_body(team_entry, work_status="FULL_TIME"))
    db_session.expire_all()
    borrower = the_borrower(db_session)
    assert borrower.work_status is WorkStatus.FULL_TIME
    assert (
        borrower.years_investing is YearsInvesting.TWO_TO_FIVE and borrower.strategy is Strategy.MIX
    )
    rows = audit_rows(db_session, AuditAction.INTAKE_EDITED)
    moved = {key: value for key, value in (rows[-1].after or {}).items() if key != "submission_id"}
    assert moved == {"borrower.work_status": "FULL_TIME"}
    assert rows[-1].before == {"borrower.work_status": "PART_TIME"}


def test_the_loan_structure_radio_renders_on_edit_intake(
    client: QueueClient, db_session: Session, stored_deal: Deal, deal_with_overrides: Deal
) -> None:
    """The team form's radio (SPEC §3) is on the edit page too, the deal's own choice checked
    and nothing checked where the structure was inferred."""
    body = client.get(f"/queue/deals/{stored_deal.id}/intake").text
    radios = re.findall(r'<input[^>]*type="radio" name="product" value="([A-Z_]+)"', body)
    assert radios == ["ACQUISITION", "ACQUISITION_REHAB"]
    assert 'role="radiogroup" aria-label="Loan Structure"' in body
    assert "> 100% at close</label>" in body and "> Holdback</label>" in body
    assert not re.search(r'name="product" value="[A-Z_]+"\s+checked', body)  # inferred
    assert '<label for="product">Loan Structure <span class="mark">Optional</span></label>' in body

    chosen = client.get(f"/queue/deals/{deal_with_overrides.id}/intake").text
    assert re.search(r'name="product" value="ACQUISITION"\s+checked', chosen)
    assert not re.search(r'name="product" value="ACQUISITION_REHAB"\s+checked', chosen)


# --- the public form ------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "es"])
def test_the_public_form_asks_them_optional_in_both_languages(
    anon_client: QueueClient, lang: str
) -> None:
    body = anon_client.get(f"/apply?lang={lang}").text
    group = between(body, '<fieldset id="about-investing">', "</fieldset>")
    groups = re.findall(r'role="group" aria-labelledby="([a-z_]+)-label"', group)
    assert groups == list(BORROWER_PROFILE_FIELDS)
    assert " required" not in group
    assert not set(BORROWER_PROFILE_FIELDS) & set(REQUIRED_FIELDS)
    for code, words in PROFILE_LABELS.items():
        assert f'value="{code}"' in group, code
        if words != code:  # "0", "1" and "GC" read as themselves
            assert f">{code}<" not in group and f"> {code}\n" not in group, code
    if lang == "en":
        assert "<legend>About your investing</legend>" in group
        assert "Is real estate your full-time work?" in group
        assert "This is my first project" in group and "Mostly myself" in group
        assert group.count('<span class="mark">Optional</span>') == 5
        assert body.index("<legend>About you</legend>") < body.index('id="about-investing"')
        assert body.index('id="about-investing"') < body.index("<legend>The property</legend>")
    else:
        assert "<legend>Sobre su experiencia como inversionista</legend>" in group
        assert "¿Los bienes raíces son su trabajo de tiempo completo?" in group
        assert "Este es mi primer proyecto" in group and "Principalmente yo" in group
        assert "Un contratista general" in group and "Una mezcla de ambos" in group
        assert group.count('<span class="mark">Opcional</span>') == 5


def test_a_web_submission_stores_the_answers_and_the_latest_answer_wins(
    anon_client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client, **PROFILE).status_code == 303
    borrower = the_borrower(db_session)
    assert borrower.work_status is WorkStatus.PART_TIME and borrower.strategy is Strategy.MIX
    (deal,) = db_session.scalars(select(Deal)).all()
    assert deal.submissions[-1].raw_payload["work_crew"] == "GC"
    # the same borrower again, the questions left blank: the row's answers stand
    assert post(anon_client, address="99 Elm St").status_code == 303
    db_session.expire_all()
    borrower = the_borrower(db_session)
    assert borrower.work_status is WorkStatus.PART_TIME and borrower.work_crew is WorkCrew.GC
    # ...and answered again, the latest answer is the one kept
    assert post(anon_client, address="7 Oak St", projects_in_progress="4_PLUS").status_code == 303
    db_session.expire_all()
    borrower = the_borrower(db_session)
    assert borrower.projects_in_progress is ProjectsInProgress.FOUR_PLUS
    assert borrower.strategy is Strategy.MIX
    assert db_session.scalar(select(Borrower.id)) == borrower.id  # still one row


def test_an_answer_off_the_list_is_refused_by_name_in_the_borrower_s_language(
    anon_client: QueueClient, db_session: Session
) -> None:
    response = post(anon_client, work_status="SOMETIMES")
    assert response.status_code == 422
    message = field_error(response.text, "work_status")
    assert message is not None and "choose one" in message
    spanish = post(anon_client, lang="es", strategy="WHOLESALE")
    assert spanish.status_code == 422
    assert field_error(spanish.text, "strategy") == "Elija una de las opciones."
    assert db_session.scalars(select(Deal)).all() == []


# --- Home's filter chips --------------------------------------------------------------------------


def two_borrowers(session: Session, team_entry: dict[str, Any]) -> tuple[Deal, Deal]:
    """Rafael Ortiz, part-time with a mix; Priya Natarajan, full-time flips."""
    rafael = store_deal(session, {**team_entry, **PROFILE})
    priya = store_deal(
        session,
        {**team_entry, **SECOND_BORROWER, "work_status": "FULL_TIME", "strategy": "FLIPS"},
    )
    return rafael, priya


def test_a_chip_narrows_by_the_borrower_s_answer(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    rafael, priya = two_borrowers(db_session, team_entry)
    assert chip_matches(rafael, "work_status", "PART_TIME")
    assert not chip_matches(rafael, "work_status", "FULL_TIME")
    assert not chip_matches(rafael, "years_investing", "10_PLUS")
    assert not chip_matches(priya, "years_investing", "2_5")  # unanswered matches nothing
    assert chip_matches(rafael, "status", "NEW")  # a deals column still narrows by itself

    def ids(filter: HomeFilter) -> list[Any]:
        return [
            entry.deal.id for entry in home_view(db_session, filter).section(Section.DCF).entries
        ]

    assert ids(HomeFilter(chips=(("work_status", "PART_TIME"),))) == [rafael.id]
    assert ids(HomeFilter(chips=(("work_status", "FULL_TIME"),))) == [priya.id]
    assert ids(HomeFilter(chips=(("work_status", "PART_TIME"), ("strategy", "MIX")))) == [rafael.id]
    assert ids(HomeFilter(chips=(("work_status", "PART_TIME"), ("strategy", "FLIPS")))) == []
    assert ids(HomeFilter(query="priya", chips=(("work_status", "FULL_TIME"),))) == [priya.id]
    assert ids(HomeFilter(query="rafael", chips=(("work_status", "FULL_TIME"),))) == []
    narrowed = home_view(db_session, HomeFilter(chips=(("strategy", "MIX"),)))
    assert narrowed.filter.active and narrowed.total == 1 and narrowed.unfiltered_total == 2


def test_the_filter_reads_and_writes_the_query_string() -> None:
    assert chips_from(
        {"work_status": "FULL_TIME", "strategy": "bogus", "q": "x", "status": "NEW"}
    ) == (("work_status", "FULL_TIME"),)
    assert chips_from({"strategy": " MIX ", "work_status": ""}) == (("strategy", "MIX"),)
    assert chips_from({}) == ()
    both = HomeFilter(query=" a b ", chips=(("work_status", "FULL_TIME"), ("strategy", "MIX")))
    assert both.summary == '"a b", Full-time in real estate: Full-time, Mostly does: Mix'
    assert both.query_string == "q=a+b&work_status=FULL_TIME&strategy=MIX"
    assert both.without("strategy").query_string == "q=a+b&work_status=FULL_TIME"
    assert both.without("strategy").without("work_status").chips == ()
    assert HomeFilter(chips=(("years_investing", "2_5"),)).summary == "Years investing: 2–5"
    assert HomeFilter(query="rafael").summary == '"rafael"'
    assert HomeFilter().query_string == "" and not HomeFilter().active


def test_home_renders_the_selects_the_chips_and_the_narrowed_count(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    rafael, priya = two_borrowers(db_session, team_entry)
    body = client.get("/queue").text
    filters = between(body, '<div class="filters" id="profile-filters">', "</div>")
    selects = re.findall(r'<select name="([a-z_]+)" aria-label="([^"]+)">', filters)
    assert selects == [(field, PROFILE_CAPTIONS[field]) for field in BORROWER_PROFILE_FIELDS]
    assert '<option value="">Years investing</option>' in filters
    assert re.search(r'<option value="FIRST_PROJECT">This is my first project</option>', filters)
    assert " selected" not in filters
    empty = between(
        body, '<div class="chips" id="filter-chips" data-hook="profile-filters">', "</div>"
    )
    assert empty.split(">", 1)[1].strip() == ""  # the row is there and holds no chip

    found = client.get("/queue", params={"work_status": "PART_TIME"}).text
    assert '<option value="PART_TIME" selected>Part-time</option>' in found
    chips = between(
        found, '<div class="chips" id="filter-chips" data-hook="profile-filters">', "</div>"
    )
    assert (
        '<a class="tag fact" data-filter="work_status" href="/queue" title="Remove this filter">'
        "Full-time in real estate: Part-time ×</a>"
    ) in chips
    assert (
        '<p class="lede small" id="search-result">1 of 2 deals match '
        "Full-time in real estate: Part-time.</p>"
    ) in found
    assert '<a class="small" href="/queue">Clear</a>' in found
    assert "Rafael Ortiz" in found and "Priya Natarajan" not in found and str(priya.id) not in found

    both = client.get(
        "/queue", params={"q": "rafael", "work_status": "PART_TIME", "strategy": "MIX"}
    ).text
    chips = between(
        both, '<div class="chips" id="filter-chips" data-hook="profile-filters">', "</div>"
    )
    assert 'data-filter="strategy" href="/queue?q=rafael&amp;work_status=PART_TIME"' in chips
    assert 'data-filter="work_status" href="/queue?q=rafael&amp;strategy=MIX"' in chips
    assert (
        '1 of 2 deals match "rafael", Full-time in real estate: Part-time, Mostly does: Mix.'
        in both
    )
    assert 'name="q" value="rafael"' in both and str(rafael.id) in both
    none = client.get("/queue", params={"q": "priya", "strategy": "MIX"}).text
    assert '0 of 2 deals match "priya", Mostly does: Mix.' in none
    # an answer off the list narrows by nothing and breaks nothing
    assert client.get("/queue", params={"work_status": "SOMETIMES"}).status_code == 200
