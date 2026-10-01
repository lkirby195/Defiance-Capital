"""The public borrower form at /apply.  # SPEC §4.2

Nothing here needs a session: the page is the one thing a stranger can read and the one
thing a stranger can write. What is under test is that it reads in both languages and stores
in one; that the browser-side courtesies (the ``required`` attributes, the button that stays
off) are wired and that the server refuses the same things again by name; that a bot's post
is dropped and a flood is turned away; and that one honest submission lands in the queue as
a WEB deal, with its source and its note, unscreened, with the borrower told nothing but
"we will be in touch".
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.apply_form import APPLY_FIELDS, HONEYPOT, REQUIRED_FIELDS
from api.i18n import from_accept_language
from api.ratelimit import LIMITER, RateLimiter
from config.config import get_config
from db.models import AuditLog, Borrower, Deal, IntakeSubmission, Screen
from schema.models import (
    AuditAction,
    Channel,
    ExperienceBucket,
    Product,
    ProductSource,
    State,
    StateSource,
    Status,
    TermBucket,
    Tranche,
)
from services import InputSource, queue_view, underwrite_readiness
from tests.conftest import QueueClient, requires_db
from tests.test_form_errors import field_error, flagged, top_problems

pytestmark = requires_db

# What a borrower on a phone posts: masked money, a dashed phone, the stored codes on the
# choices, the hidden language and source, the consent box ticked.
VALID: dict[str, str] = {
    "lang": "en",
    "src": "tulsa-reia",
    "borrower_name": "María Elena Ortiz",
    "entity_name": "Ortiz Builds LLC",
    "borrower_phone": "918-555-0142",
    "borrower_email": "maria@ortizbuilds.example",
    "credit_range": "T2",
    "experience_bucket": "3_5",
    "repeat_borrower": "false",
    "address": "1412 S Peoria Ave",
    "city": "Tulsa",
    "state": "OK",
    "listing_url": "",
    "purchase_price": "$185,000",
    "rehab_costs": "$40,000",
    "loan_requested": "$160,000",
    "estimated_sale_price": "$290,000",
    "monthly_rent": "$1,950",
    "term_bucket": "12_PLUS",
    "closing_date": "2026-11-15",
    "referral_note": "A friend at the Tulsa REIA",
    "consent": "true",
}
ZILLOW = "https://www.zillow.com/homedetails/3320-Meade-St-Denver-CO-80211/13241234_zpid/"

CONTROL = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.S)


@pytest.fixture(autouse=True)
def fresh_limiter() -> Any:
    """Every test starts with nobody having posted anything."""
    LIMITER.reset()
    yield
    LIMITER.reset()


def controls(body: str) -> dict[str, list[str]]:
    """Every named control on the page, all of them, by name."""
    found: dict[str, list[str]] = {}
    for tag in CONTROL.findall(body):
        name = re.search(r'name="([^"]+)"', tag)
        if name is not None:
            found.setdefault(name.group(1), []).append(tag)
    return found


def deals(session: Session) -> list[Deal]:
    return list(session.scalars(select(Deal)))


def count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def post(client: QueueClient, **changes: str) -> Any:
    return client.post("/apply", data={**VALID, **changes}, follow_redirects=False)


# --- the page, in both languages ------------------------------------------------------------------


def test_the_form_needs_no_session(anon_client: QueueClient) -> None:
    response = anon_client.get("/apply", follow_redirects=False)
    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert 'id="apply-form"' in response.text
    assert "Tell us about your deal" in response.text
    # ...and nothing of the queue's furniture is on it
    assert "/queue" not in response.text
    assert 'name="_csrf"' not in response.text


def test_the_toggle_switches_the_language_and_keeps_the_source(anon_client: QueueClient) -> None:
    english = anon_client.get("/apply?src=Flyer-1").text
    assert 'href="/apply?lang=es&amp;src=flyer-1"' in english
    assert 'name="src" value="flyer-1"' in english
    assert 'name="lang" value="en"' in english

    spanish = anon_client.get("/apply?lang=es&src=flyer-1").text
    assert '<html lang="es">' in spanish
    assert "Cuéntenos sobre su proyecto" in spanish
    assert 'href="/apply?lang=en&amp;src=flyer-1"' in spanish
    assert 'name="lang" value="es"' in spanish
    assert "Tell us about your deal" not in spanish


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("en-US,en;q=0.9", "en"),
        ("es-MX,es;q=0.9,en;q=0.8", "es"),
        ("es-419", "es"),
        ("fr-FR,fr;q=0.9", "en"),
        ("en;q=0.5, es;q=0.8", "es"),
        ("es;q=0, en", "en"),
        ("*", "en"),
    ],
)
def test_the_default_language_follows_the_browser(header: str | None, expected: str) -> None:
    assert from_accept_language(header) == expected


def test_a_spanish_browser_gets_spanish_and_can_ask_for_english(anon_client: QueueClient) -> None:
    headers = {"accept-language": "es-MX,es;q=0.9,en;q=0.8"}
    assert '<html lang="es">' in anon_client.get("/apply", headers=headers).text
    assert '<html lang="en">' in anon_client.get("/apply?lang=en", headers=headers).text


@pytest.mark.parametrize("lang", ["en", "es"])
def test_every_stored_value_is_the_same_code_in_either_language(
    anon_client: QueueClient, lang: str
) -> None:
    """The words change; the ``value`` a control posts does not.  # SPEC §4.2"""
    body = anon_client.get(f"/apply?lang={lang}").text
    for member in Tranche:
        assert f'value="{member.value}"' in body
    for member in ExperienceBucket:
        assert f'value="{member.value}"' in body
    for member in TermBucket:
        assert f'value="{member.value}"' in body
    for member in State:
        assert f'value="{member.value}"' in body
    # the labels a person reads: FICO ranges off the config cutoffs, and "12+ months"
    if lang == "en":
        assert "740 or higher" in body and "Under 620" in body and "12+ months" in body
        assert "Oklahoma" in body and "Another state" in body
    else:
        assert "740 o más" in body and "Menos de 620" in body and "12+ meses" in body
        assert "Oklahoma" in body and "Otro estado" in body
    assert "700–739" in body
    assert ">T2<" not in body and ">3_5<" not in body and ">12_PLUS<" not in body


def test_the_page_renders_every_box_the_server_reads(anon_client: QueueClient) -> None:
    found = controls(anon_client.get("/apply").text)
    for name in APPLY_FIELDS:
        assert name in found, name
    assert HONEYPOT in found


# --- required: the attribute, the button, and the refusal --------------------------------------


def test_required_boxes_carry_the_attribute_and_the_button_is_wired(
    anon_client: QueueClient,
) -> None:
    """The browser half: ``required`` on every required control, the submit hook, the script."""
    body = anon_client.get("/apply").text
    found = controls(body)
    for name in APPLY_FIELDS:
        tags = found[name]
        marked = all(" required" in tag for tag in tags)
        assert marked == (name in REQUIRED_FIELDS), name
    assert " required" not in found[HONEYPOT][0]
    # the submit button that stays off, and the script that keeps it off
    assert re.search(r"<button type=\"submit\" data-requires-all>", body)
    assert 'querySelector("[data-requires-all]")' in body
    assert "button.disabled = !complete" in body
    # the listing link lifts required off the address boxes
    assert 'data-unlocks="address city state"' in found["listing_url"][0]
    assert 'getAttribute("data-unlocks")' in body


@pytest.mark.parametrize("name", REQUIRED_FIELDS)
def test_a_required_box_left_empty_is_refused_with_the_complaint_beside_it(
    anon_client: QueueClient, db_session: Session, name: str
) -> None:
    """The server half: the same list, refused by name, and nothing stored."""
    body = dict(VALID)
    del body[name]

    response = anon_client.post("/apply", data=body, follow_redirects=False)

    assert response.status_code == 422, name
    expected = "Please tick the box" if name == "consent" else "This is required."
    message = field_error(response.text, name)
    assert message is not None and expected in message, name
    assert flagged(response.text, name) or name in (
        "experience_bucket",
        "repeat_borrower",
        "term_bucket",
        "consent",
    )
    assert top_problems(response.text) == []
    for other in REQUIRED_FIELDS:
        if other != name:
            assert field_error(response.text, other) is None, other
    assert deals(db_session) == []
    # ...and what was typed is still in the boxes, masked the way the page shows it
    assert 'value="Ortiz Builds LLC"' in response.text
    if name != "purchase_price":
        assert 'value="$185,000"' in response.text


def test_the_complaint_is_in_spanish_when_the_page_was(
    anon_client: QueueClient, db_session: Session
) -> None:
    body = {**VALID, "lang": "es"}
    del body["borrower_name"]
    del body["consent"]
    response = anon_client.post("/apply", data=body, follow_redirects=False)
    assert response.status_code == 422
    assert '<html lang="es">' in response.text
    assert field_error(response.text, "borrower_name") == "Este campo es obligatorio."
    assert field_error(response.text, "consent") == (
        "Marque la casilla para que podamos comunicarnos con usted."
    )
    assert deals(db_session) == []


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("borrower_phone", "555-0142", "10-digit phone"),
        ("purchase_price", "nan", "amount in dollars"),
        ("consent", "maybe", "tick the box"),
        ("borrower_email", "maria at ortizbuilds", "valid email"),
        ("credit_range", "T9", "choose one"),
        ("state", "TX", "choose one"),
        ("purchase_price", "$0", "greater than zero"),
        ("loan_requested", "a lot", "amount in dollars"),
        ("rehab_costs", "-$5", "greater than zero"),
        ("listing_url", "zillow.com/homedetails/x", "starting with http"),
        ("closing_date", "soon", "enter a date"),
        ("term_bucket", "24", "choose one"),
        ("referral_note", "x" * 201, "shorter"),
    ],
)
def test_a_box_with_the_wrong_shape_is_refused_by_name(
    anon_client: QueueClient, db_session: Session, name: str, value: str, expected: str
) -> None:
    response = post(anon_client, **{name: value})
    assert response.status_code == 422, (name, value)
    message = field_error(response.text, name)
    assert message is not None and expected in message, (name, message)
    assert deals(db_session) == []


def test_several_wrong_boxes_are_all_answered_at_once(anon_client: QueueClient) -> None:
    body = dict(VALID)
    del body["city"]
    response = anon_client.post(
        "/apply",
        data={**body, "borrower_phone": "12", "purchase_price": "$0"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    for name in ("city", "borrower_phone", "purchase_price"):
        assert field_error(response.text, name), name


# --- spam -----------------------------------------------------------------------------------------


def test_the_honeypot_drops_the_post_silently(
    anon_client: QueueClient, db_session: Session
) -> None:
    """A bot that filled the invisible box is thanked and nothing is stored."""
    response = post(anon_client, **{HONEYPOT: "https://spam.example"})
    assert response.status_code == 303
    assert response.headers["location"] == "/apply/thanks?lang=en"
    assert deals(db_session) == []
    assert count(db_session, IntakeSubmission) == 0
    assert count(db_session, AuditLog) == 0


def test_the_rate_limit_turns_the_next_post_away_politely(
    anon_client: QueueClient, db_session: Session
) -> None:
    limit = get_config().web_intake.submissions_per_hour_per_ip
    for _ in range(limit):
        assert post(anon_client).status_code == 303
    assert len(deals(db_session)) == limit

    refused = post(anon_client)
    assert refused.status_code == 429
    assert "try again a little later" in refused.text
    assert len(deals(db_session)) == limit
    # ...and in Spanish when the page was
    assert "inténtelo de nuevo" in post(anon_client, lang="es").text
    # a refused post still counts, so a bot that keeps trying stays refused
    assert post(anon_client).status_code == 429


def test_the_window_slides_and_every_attempt_counts() -> None:
    from datetime import UTC, datetime, timedelta

    limiter = RateLimiter(window=timedelta(hours=1))
    start = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    for minute in range(5):
        assert limiter.allow("1.2.3.4", 5, now=start + timedelta(minutes=minute))
    assert not limiter.allow("1.2.3.4", 5, now=start + timedelta(minutes=5))
    # another address is its own window
    assert limiter.allow("5.6.7.8", 5, now=start + timedelta(minutes=5))
    # an hour after the first attempt, the first attempt has aged out - but the refused sixth
    # attempt counted, so there are still five inside the window
    assert not limiter.allow("1.2.3.4", 5, now=start + timedelta(minutes=60, seconds=1))
    assert limiter.allow("1.2.3.4", 5, now=start + timedelta(hours=2))


# --- the one honest submission ------------------------------------------------------------------


def test_a_full_submission_lands_in_the_queue_as_a_web_deal(
    anon_client: QueueClient, db_session: Session
) -> None:
    response = post(anon_client)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/apply/thanks?lang=en"

    (deal,) = deals(db_session)
    assert deal.channel is Channel.WEB
    assert deal.status is Status.NEW
    assert deal.missing_fields == []
    assert deal.intake_source == "tulsa-reia"
    assert deal.referral_note == "A friend at the Tulsa REIA"
    # the 12+ bucket seeds a 12-month term and stays on the deal (SPEC §4.1, §4.2)
    assert deal.term_bucket is TermBucket.M12_PLUS
    assert deal.term_months == 12 and deal.term_stub_days is None
    # the self-reported borrower facts, as codes
    assert deal.credit_range_self_reported is Tranche.T2
    assert deal.experience_bucket_self_reported is ExperienceBucket.THREE_TO_FIVE
    assert deal.repeat_borrower_self_reported is False
    # the numbers, unmasked
    assert deal.purchase_price == Decimal("185000")
    assert deal.rehab_costs == Decimal("40000")
    assert deal.loan_requested == Decimal("160000")
    assert deal.estimated_sale_price_team == Decimal("290000")
    assert deal.monthly_rent == Decimal("1950")
    assert str(deal.closing_date) == "2026-11-15"
    # no loan-type selector: the normalizer inferred one, as on every channel
    assert deal.product is Product.SPLIT_DRAW and deal.product_source is ProductSource.INFERRED
    # nothing the team form unlocks was touched
    assert deal.interest_rate is None and deal.loan_purchase_portion is None
    assert deal.court_records_status is None
    # the borrower row, keyed by the phone's digits
    assert deal.borrower is not None
    assert deal.borrower.phone == "9185550142"
    assert deal.borrower.name == "María Elena Ortiz"
    assert deal.borrower.email == "maria@ortizbuilds.example"
    assert [e.name for e in deal.borrower.entities] == ["Ortiz Builds LLC"]
    # the property, with the address written the way every channel writes it
    assert deal.property is not None
    assert deal.property.address_raw == "1412 S Peoria Ave, Tulsa, OK"
    assert deal.property.address_normalized == "1412 S PEORIA AVE, TULSA, OK"
    assert deal.property.city == "Tulsa"
    assert deal.property.state is State.OK
    assert deal.property.state_source is StateSource.ENTERED
    # no screen was run (SPEC §4.2)
    assert count(db_session, Screen) == 0
    # the submission row keeps the stored form and the language, immutable
    assert len(deal.submissions) == 1
    payload = deal.submissions[0].raw_payload
    assert deal.submissions[0].channel is Channel.WEB
    assert payload["language"] == "en" and payload["consent"] is True
    assert payload["purchase_price"] == "185000" and payload["borrower_phone"] == "9185550142"
    # ...and the audit row names the borrower as the actor
    (row,) = list(db_session.scalars(select(AuditLog)))
    assert row.action == AuditAction.INTAKE_CREATED.value
    assert row.actor == "web:maria@ortizbuilds.example"
    assert row.after is not None and row.after["channel"] == "WEB"
    assert row.after["intake_source"] == "tulsa-reia"
    # in the queue, under NEW
    view = queue_view(db_session)
    assert [group.status for group in view.groups] == [Status.NEW]
    assert view.groups[0].entries[0].deal.id == deal.id


def test_the_queue_and_the_deal_page_say_it_came_from_the_web(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client, lang="es").status_code == 303
    (deal,) = deals(db_session)

    queue_body = client.get("/queue").text
    assert '<span class="tag INFO">Web</span>' in queue_body

    deal_body = client.get(f"/queue/deals/{deal.id}").text
    assert "From the public form" in deal_body
    assert "tulsa-reia" in deal_body
    assert "A friend at the Tulsa REIA" in deal_body
    assert "<dd>Spanish" in deal_body
    assert "the borrower asked for\n        12+ months" in deal_body or "12+ months" in deal_body
    assert "The borrower selected 12+ months" in deal_body
    assert "seeded at 12" in deal_body
    assert ">12_PLUS<" not in deal_body

    # the readiness row calls the seeded term what it is
    row = {row.key: row for row in underwrite_readiness(deal).rows}["deal.term_months"]
    assert row.source is InputSource.DEFAULT
    assert "12+ bucket" in row.note and "asked for more than a year" in row.note


def test_the_json_view_carries_the_provenance(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client).status_code == 303
    (deal,) = deals(db_session)
    body = client.get(f"/deals/{deal.id}").json()
    assert body["channel"] == "WEB"
    assert body["intake_source"] == "tulsa-reia"
    assert body["referral_note"] == "A friend at the Tulsa REIA"
    assert body["term_bucket"] == "12_PLUS" and body["term_months"] == 12


def test_a_lesser_bucket_seeds_its_own_months_and_a_bad_source_is_dropped(
    anon_client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client, term_bucket="6", src="<script>alert(1)</script>").status_code == 303
    (deal,) = deals(db_session)
    assert deal.term_bucket is TermBucket.M6 and deal.term_months == 6
    assert deal.intake_source is None


def test_a_repeat_borrower_reuses_their_row(anon_client: QueueClient, db_session: Session) -> None:
    assert post(anon_client).status_code == 303
    assert (
        post(anon_client, address="99 Elm St", borrower_phone="(918) 555-0142").status_code == 303
    )
    assert count(db_session, Borrower) == 1
    assert count(db_session, Deal) == 2


def test_the_thanks_page_says_only_that_the_team_will_be_in_touch(
    anon_client: QueueClient,
) -> None:
    english = anon_client.get("/apply/thanks").text
    assert "A member of our team will be in touch soon" in english
    spanish = anon_client.get("/apply/thanks?lang=es").text
    assert "se comunicará con usted pronto" in spanish
    for body in (english, spanish):
        shown = re.search(r"<main[^>]*>(.*?)</main>", body, re.S)
        assert shown is not None
        for forbidden in ("GO", "CONDITIONAL", "DECLINE", "$", "%", "approved", "aprobad"):
            assert forbidden not in shown.group(1), forbidden
        assert 'href="/apply?lang=' in shown.group(1)


def test_a_signed_in_team_member_can_post_it_too_without_a_token(
    client: QueueClient, db_session: Session
) -> None:
    """The form is outside the CSRF guard: there is no session to bind a token to."""
    response = client.post("/apply", data={**VALID, "_csrf": ""}, follow_redirects=False)
    assert response.status_code == 303
    assert count(db_session, Deal) == 1


# --- the listing link ---------------------------------------------------------------------------


def test_a_listing_link_fills_a_blank_address(
    anon_client: QueueClient, db_session: Session
) -> None:
    """SPEC §4.4: the slug is read, the page is not; a state off the link is inferred."""
    response = post(anon_client, address="", city="", state="", listing_url=ZILLOW)
    assert response.status_code == 303, response.text
    (deal,) = deals(db_session)
    assert deal.property is not None
    assert deal.property.address_raw == "3320 Meade St, Denver, CO"
    assert deal.property.city == "Denver"
    assert deal.property.state is State.CO
    assert deal.property.state_source is StateSource.INFERRED
    assert deal.property.listing_url == ZILLOW


def test_a_link_outside_the_two_states_lands_on_other(
    anon_client: QueueClient, db_session: Session
) -> None:
    austin = "https://www.zillow.com/homedetails/100-Congress-Ave-Austin-TX-78701/1_zpid/"
    assert post(anon_client, address="", city="", state="", listing_url=austin).status_code == 303
    (deal,) = deals(db_session)
    assert deal.property is not None
    assert deal.property.state is State.OTHER
    assert deal.property.address_raw == "100 Congress Ave, Austin"


def test_a_typed_address_is_never_overwritten_by_the_link(
    anon_client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client, listing_url=ZILLOW).status_code == 303
    (deal,) = deals(db_session)
    assert deal.property is not None
    assert deal.property.address_raw == "1412 S Peoria Ave, Tulsa, OK"
    assert deal.property.listing_url == ZILLOW


def test_a_link_that_carries_no_address_still_asks_for_one(
    anon_client: QueueClient, db_session: Session
) -> None:
    response = post(
        anon_client, address="", city="", state="", listing_url="https://www.zillow.com/homes/"
    )
    assert response.status_code == 422
    for name in ("address", "city", "state"):
        assert field_error(response.text, name) == "This is required.", name
    assert deals(db_session) == []
