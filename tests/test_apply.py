"""The public borrower form at /apply.  # SPEC §4.2

Nothing here needs a session: the page is the one thing a stranger can read and the one
thing a stranger can write. What is under test is that it reads in both languages and stores
in one; that the browser-side courtesies (the ``required`` attributes, the button that stays
off) are wired and that the server refuses the same things again by name; that a bot's post
is dropped and a flood is turned away; and that one honest submission lands in the queue as
a WEB deal, with its source and its note, screened and priced on the way in on the config
defaults and the borrower's own numbers (SPEC §4.6), with the borrower sent to a thank-you
page of their own that shows them their indicative terms - and nothing else of the analysis
- when the arrival run allows it (``services/indicative.py``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.apply_form import APPLY_FIELDS, HONEYPOT, REQUIRED_FIELDS
from api.i18n import STRINGS, from_accept_language
from api.ratelimit import LIMITER, RateLimiter
from api.render import dollars, pct, quoted
from config.config import DEFAULT_PATH, Config, get_config, load_yaml
from db.models import AuditLog, Borrower, Deal, IntakeSubmission, Screen, Underwrite
from schema.models import (
    AuditAction,
    Channel,
    ExperienceBucket,
    PricingMatrix,
    Product,
    ProductSource,
    State,
    StateSource,
    Status,
    TermBucket,
    Tranche,
    Verdict,
)
from services import (
    ASSUMPTIONS,
    InputSource,
    Section,
    TeamOverrides,
    get_version,
    home_view,
    run_screen,
    save_overrides,
    underwrite_readiness,
    version_results,
)
from tests.conftest import ACTOR, QueueClient, requires_db
from tests.test_form_errors import field_error, flagged, top_problems

pytestmark = requires_db

# What a borrower on a phone posts: masked money, a dashed phone, the stored codes on the
# choices, the hidden language and source. Sending it is the consent (SPEC §4.2).
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
    message = field_error(response.text, name)
    assert message is not None and "This is required." in message, name
    assert flagged(response.text, name) or name in (
        "experience_bucket",
        "repeat_borrower",
        "term_bucket",
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
    response = anon_client.post(
        "/apply", data={**body, "borrower_phone": "12"}, follow_redirects=False
    )
    assert response.status_code == 422
    assert '<html lang="es">' in response.text
    assert field_error(response.text, "borrower_name") == "Este campo es obligatorio."
    assert field_error(response.text, "borrower_phone") == (
        "Ingrese un número de teléfono de 10 dígitos."
    )
    assert deals(db_session) == []


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("borrower_phone", "555-0142", "10-digit phone"),
        ("purchase_price", "nan", "amount in dollars"),
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
    # ...to a thank-you link like anyone else's, on a token nobody was issued
    assert THANKS_LINK.fullmatch(response.headers["location"])
    assert anon_client.get(response.headers["location"]).status_code == 200
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
    assert THANKS_LINK.fullmatch(response.headers["location"])

    (deal,) = deals(db_session)
    assert deal.channel is Channel.WEB
    # sent to its own thank-you page, on a token live for web_intake.thanks_token_hours
    assert response.headers["location"] == f"/apply/thanks/{deal.thanks_token}?lang=en"
    assert deal.thanks_token is not None and deal.thanks_token_expires_at is not None
    hours = get_config().web_intake.thanks_token_hours
    assert abs(deal.thanks_token_expires_at - deal.created_at - timedelta(hours=hours)) < (
        timedelta(minutes=1)
    )
    # complete on arrival, so screened and priced on the way in (SPEC §4.6)
    assert deal.status is Status.UNDERWRITING
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
    # the borrower's own estimates land in their own columns, not the team's (SPEC §4.2)
    assert deal.estimated_sale_price_borrower == Decimal("290000")
    assert deal.monthly_rent_borrower == Decimal("1950")
    assert deal.estimated_sale_price_team is None and deal.monthly_rent is None
    assert str(deal.closing_date) == "2026-11-15"
    # no loan-type selector: the normalizer inferred one, as on every channel
    assert deal.product is Product.SPLIT_DRAW and deal.product_source is ProductSource.INFERRED
    # the §8.1 economics the form does not ask were populated as defaults, and say so
    assert deal.interest_rate == Decimal("0.12")
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (
        Decimal("120000"),
        Decimal("40000"),
    )
    assert set(deal.defaulted_fields) == {
        "interest_rate",
        "contingency_pct",
        "closing_costs_usd",
        "holding_costs_pct_of_cost",
        "origination_fee_pct",
        *ASSUMPTIONS,
        "loan_purchase_portion",
        "loan_rehab_portion",
    }
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
    # the screen and the underwrite ran on arrival, as the system (SPEC §4.6)
    assert count(db_session, Screen) == 1 and count(db_session, Underwrite) == 1
    # the submission row keeps the stored form and the language, immutable
    assert len(deal.submissions) == 1
    payload = deal.submissions[0].raw_payload
    assert deal.submissions[0].channel is Channel.WEB
    assert payload["language"] == "en"
    assert payload["consent"] is True, "sending the form is the consent, and it is recorded"
    assert payload["purchase_price"] == "185000" and payload["borrower_phone"] == "9185550142"
    # ...and the audit row names the borrower as the actor, and the runs - one version,
    # Auto v1 - the system
    rows = list(db_session.scalars(select(AuditLog).order_by(AuditLog.created_at)))
    assert [row.action for row in rows] == [
        AuditAction.INTAKE_CREATED.value,
        AuditAction.THANKS_TOKEN_ISSUED.value,
        AuditAction.SCREEN_RUN.value,
        AuditAction.UNDERWRITE_RUN.value,
        AuditAction.VERSION_RECORDED.value,
    ]
    created, tokened, screened, priced, versioned = rows
    assert created.actor == "web:maria@ortizbuilds.example"
    # the link's expiry is on the record; the token never is
    assert tokened.actor == created.actor and tokened.after is not None
    assert tokened.after == {"expires_at": deal.thanks_token_expires_at.isoformat()}
    assert deal.thanks_token not in str(tokened.after)
    assert created.after is not None and created.after["channel"] == "WEB"
    assert created.after["intake_source"] == "tulsa-reia"
    assert created.after["defaults"]["interest_rate"] == "0.12"
    assert screened.actor == "system" and priced.actor == "system"
    assert versioned.actor == "system" and versioned.after is not None
    assert versioned.after["name"] == "Auto v1" and versioned.after["working"] is True
    # on Home, under Borrower Submissions, at UNDERWRITING
    view = home_view(db_session)
    [entry] = view.section(Section.BORROWER).entries
    assert entry.deal.id == deal.id and entry.deal.status is Status.UNDERWRITING
    assert view.section(Section.DCF).entries == []


def test_the_queue_and_the_deal_page_say_it_came_from_the_web(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    assert post(anon_client, lang="es").status_code == 303
    (deal,) = deals(db_session)

    home_body = client.get("/queue").text
    assert "Web · tulsa-reia" in home_body  # the Source column: the channel and the slug
    assert home_body.index("Borrower Submissions") < home_body.index(str(deal.id))
    assert home_body.index(str(deal.id)) < home_body.index("DCF New Deals")

    deal_body = client.get(f"/queue/deals/{deal.id}").text
    assert "From the public form" in deal_body
    assert "Source: tulsa-reia" in deal_body
    assert "How they heard of us: A friend at the Tulsa REIA" in deal_body
    assert "<dd>Spanish" in deal_body
    assert re.search(r"<dt>Requested term.*?</dt>\s*<dd>12\+ months</dd>", deal_body, re.S)
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


def test_the_plain_thanks_page_says_only_that_the_team_will_be_in_touch(
    anon_client: QueueClient,
) -> None:
    """The page with no token - a bookmark, an old link - says the team will be in touch."""
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


# --- the borrower's own numbers are the borrower's (SPEC §4.2, §6.1) -----------------------------


def test_the_borrower_estimates_are_third_in_line_and_say_so(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    """Readiness, the deal page and the stored screen all call them the borrower's."""
    assert post(anon_client).status_code == 303
    (deal,) = deals(db_session)
    rows = {row.key: row for row in underwrite_readiness(deal).rows}
    assert rows["estimated_sale_price"].source is InputSource.BORROWER
    assert rows["estimated_sale_price"].value == Decimal("290000")
    assert rows["monthly_rent"].source is InputSource.BORROWER
    assert rows["monthly_rent"].value == Decimal("1950")

    body = client.get(f"/queue/deals/{deal.id}").text
    assert ">Borrower<" in body
    assert "Their sale price after rehab" in body and "$290,000.00" in body
    assert ">BORROWER<" not in body

    result = run_screen(db_session, deal.id, actor=ACTOR)
    db_session.commit()
    assert result.sizing.estimated_sale_price_source.value == "BORROWER"
    assert "BORROWER_SOURCED_VALUES" in [flag.code.value for flag in result.flags]
    assert any("came from the borrower" in reason for reason in result.reasons)


def test_a_team_entry_replaces_the_borrower_value_and_keeps_it_for_the_record(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    """Adapter over team over borrower, and nothing is overwritten (SPEC §6.1)."""
    assert post(anon_client).status_code == 303
    (deal,) = deals(db_session)
    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(estimated_sale_price_team=Decimal("250000.00")),
        actor=ACTOR,
    )
    db_session.commit()
    db_session.expire_all()
    deal = db_session.get(Deal, deal.id)
    assert deal is not None
    # the team's number is the one in force...
    rows = {row.key: row for row in underwrite_readiness(deal).rows}
    assert rows["estimated_sale_price"].source is InputSource.TEAM
    assert rows["estimated_sale_price"].value == Decimal("250000")
    # ...the rent the team left alone is still the borrower's...
    assert rows["monthly_rent"].source is InputSource.BORROWER
    # ...and the borrower's sale price is still on the deal, untouched, for the audit trail
    assert deal.estimated_sale_price_borrower == Decimal("290000")
    assert deal.estimated_sale_price_team == Decimal("250000")
    trail = list(db_session.scalars(select(AuditLog).where(AuditLog.action == "OVERRIDES_SAVED")))
    assert len(trail) == 1 and trail[0].after is not None
    assert "estimated_sale_price_team" in trail[0].after
    assert "estimated_sale_price_borrower" not in trail[0].after

    body = client.get(f"/queue/deals/{deal.id}").text
    assert ">Team<" in body and ">Borrower<" in body
    assert "$290,000.00" in body  # the borrower's figure, shown as theirs


# --- the lender's name ----------------------------------------------------------------------------


def test_no_borrower_page_names_glenwood(anon_client: QueueClient) -> None:
    """GLENWOOD is the project's name, not the lender's on the page: the borrower reads
    Defiance Capital on the form in both languages, on the complaint re-render in both, on
    the plain thank-you page in both and on the thank-you page with their indicative terms
    in both."""
    link = thanks_link(post(anon_client))
    shown = [
        anon_client.get("/apply"),
        anon_client.get("/apply?lang=es"),
        anon_client.get("/apply/thanks"),
        anon_client.get("/apply/thanks?lang=es"),
        anon_client.get(link),
        anon_client.get(f"{link.split('?')[0]}?lang=es"),
    ]
    assert [page.status_code for page in shown] == [200] * 6
    assert terms_table(shown[4].text) is not None and terms_table(shown[5].text) is not None
    complained = [
        post(anon_client, borrower_name=""),
        post(anon_client, lang="es", borrower_name=""),
    ]
    assert [page.status_code for page in complained] == [422, 422]
    for page in (*shown, *complained):
        assert "GLENWOOD" not in page.text
        assert "Defiance Capital" in page.text
    # the business-purpose line itself, in each language
    assert "Defiance Capital makes business-purpose loans" in shown[0].text
    assert "Defiance Capital otorga préstamos" in shown[1].text


# --- the indicative terms on the thank-you page (SPEC §4.2) --------------------------------------

THANKS_LINK = re.compile(r"/apply/thanks/[A-Za-z0-9_-]{43}\?lang=(en|es)")
DISCLAIMER_EN, DISCLAIMER_ES = STRINGS["terms_disclaimer"]
# A sale price the $160,000 request is 88.9% of: past the 75% cap by more than the 5-point
# band, so the arrival screen declines (SPEC §7.4, §7.5).
DECLINING_SALE_PRICE = "$180,000"


def thanks_link(response: Any) -> str:
    assert response.status_code == 303, response.text
    location: str = response.headers["location"]
    assert THANKS_LINK.fullmatch(location), location
    return location


def between(body: str, start: str, end: str) -> str | None:
    at = body.find(start)
    if at < 0:
        return None
    return body[at : body.index(end, at)]


def terms_table(body: str) -> str | None:
    """The Indicative terms block, or None when the page is the plain thank-you."""
    return between(body, '<div id="indicative-terms">', '<p><a href="/apply?lang=')


def shown_main(body: str) -> str:
    found = re.search(r"<main[^>]*>(.*?)</main>", body, re.S)
    assert found is not None
    return found.group(1)


def arrival(session: Session, deal: Deal) -> tuple[Verdict, PricingMatrix | None]:
    """The arrival run's verdict and pricing matrix: version 1, the system's."""
    results = version_results(session, get_version(session, deal.id, 1))
    assert results.version.actor == "system" and results.screen is not None
    pricing = results.underwrite.pricing if results.underwrite is not None else None
    return results.screen.verdict, pricing


def capped(monkeypatch: pytest.MonkeyPatch, max_quoted_rate: str) -> None:
    """Run the arrival analysis under a lower maximum quoted rate."""
    import services.autorun

    data = load_yaml(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["pricing"]["max_quoted_rate"] = max_quoted_rate
    monkeypatch.setattr(services.autorun, "get_config", lambda: Config.from_dict(data))


def test_a_go_deal_sees_its_indicative_terms_and_nothing_else(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    verdict, pricing = arrival(db_session, deal)
    assert verdict is Verdict.GO and pricing is not None
    assert all(row.cleared for row in pricing.rows), "every placeholder row quotes"

    body = anon_client.get(link).text
    main = shown_main(body)
    assert "A member of our team will be in touch soon" in main
    table = terms_table(body)
    assert table is not None
    assert "<h2>Indicative terms</h2>" in table
    # the required language, whole, above and below the table
    assert table.index(DISCLAIMER_EN) < table.index("<table>")
    assert table.index("</table>") < table.index("For business-purpose loans only.")
    # one row per pricing step, labelled, with the amount, the rate, the profit and the yield
    assert ["Requested", "-5%", "-10%"] == re.findall(r'<th scope="row">([^<]+)</th>', table)
    for cell in ("Loan amount", "Rate", "Estimated profit", "Yield (cash-on-cash)"):
        assert f">{cell}</th>" in table
    for row in pricing.rows:
        assert row.interest_rate is not None and row.borrower_profit is not None
        assert dollars(row.loan_amount) in table
        assert quoted(row.interest_rate) in table
        assert dollars(row.borrower_profit) in table
        assert pct(row.borrower_cash_on_cash) in table
    assert "Add an estimated sale price" not in table
    # nothing else of the analysis: no verdict, no IRR, no flags, no sensitivity
    for forbidden in ("GO", "CONDITIONAL", "DECLINE", "IRR", "Verdict", "flag", "Sensitivity"):
        assert forbidden not in main, forbidden
    for row in pricing.rows:
        assert row.lender_irr is not None and f"{row.lender_irr * 100:.4f}%" not in main
    # the language toggle keeps the token
    token = link.split("?")[0]
    assert f'href="{token}?lang=es"' in body


def test_a_conditional_deal_without_a_sale_price_sees_amounts_and_rates_only(
    anon_client: QueueClient, db_session: Session
) -> None:
    """No sale price: the screen is Conditional, the flip is not evaluated, and the table
    has the two columns and the line asking for a price."""
    link = thanks_link(post(anon_client, estimated_sale_price=""))
    (deal,) = deals(db_session)
    verdict, pricing = arrival(db_session, deal)
    assert verdict is Verdict.CONDITIONAL and pricing is not None
    assert all(row.cleared and row.borrower_profit is None for row in pricing.rows)

    table = terms_table(anon_client.get(link).text)
    assert table is not None
    assert ">Loan amount</th>" in table and ">Rate</th>" in table
    assert "Estimated profit" not in table and "Yield" not in table
    assert "Add an estimated sale price for a profit estimate." in table
    for row in pricing.rows:
        assert dollars(row.loan_amount) in table and quoted(row.interest_rate) in table
    assert DISCLAIMER_EN in table and "For business-purpose loans only." in table


def test_a_declined_deal_gets_the_plain_thank_you(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client, estimated_sale_price=DECLINING_SALE_PRICE))
    (deal,) = deals(db_session)
    verdict, _ = arrival(db_session, deal)
    assert verdict is Verdict.DECLINE

    body = anon_client.get(link).text
    assert terms_table(body) is None
    main = shown_main(body)
    assert "A member of our team will be in touch soon" in main
    for forbidden in ("GO", "CONDITIONAL", "DECLINE", "$", "%", "Indicative"):
        assert forbidden not in main, forbidden
    db_session.expire_all()
    assert deal.indicative_terms_shown_at is None and deal.indicative_terms_shown is None
    # ...and the deal page says nothing was shown
    pricing = between(client.get(f"/queue/deals/{deal.id}").text, 'id="pricing"', "</section>")
    assert pricing is not None
    assert "Shown to borrower" in pricing and "Nothing shown to the borrower." in pricing


def test_rows_above_the_max_quoted_rate_are_left_out(
    anon_client: QueueClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draw deal prices each row at its own rate, the request's the highest; a maximum set
    at the middle row's rate drops the request row from the table, and a maximum below every
    row leaves the plain thank-you."""
    thanks_link(post(anon_client))
    (first,) = deals(db_session)
    _, pricing = arrival(db_session, first)
    assert pricing is not None
    rates = [row.interest_rate for row in pricing.rows]
    assert len(set(rates)) == 3 and rates[0] == max(rate for rate in rates if rate is not None)

    middle = sorted(rate for rate in rates if rate is not None)[1]
    capped(monkeypatch, str(middle))
    link = thanks_link(post(anon_client))
    second = [deal for deal in deals(db_session) if deal.id != first.id][0]
    _, second_pricing = arrival(db_session, second)
    assert second_pricing is not None
    left_out = [row for row in second_pricing.rows if row.over_max_quote]
    kept = [row for row in second_pricing.rows if row.cleared]
    assert [row.label for row in left_out] == ["Requested"] and len(kept) == 2
    table = terms_table(anon_client.get(link).text)
    assert table is not None
    assert "Requested" not in table and dollars(left_out[0].loan_amount) not in table
    assert re.findall(r'<th scope="row">([^<]+)</th>', table) == ["-5%", "-10%"]
    for row in kept:
        assert dollars(row.loan_amount) in table and quoted(row.interest_rate) in table

    capped(monkeypatch, "0.01")
    link = thanks_link(post(anon_client))
    third = [deal for deal in deals(db_session) if deal.id not in {first.id, second.id}][0]
    _, third_pricing = arrival(db_session, third)
    assert third_pricing is not None and not any(row.cleared for row in third_pricing.rows)
    assert terms_table(anon_client.get(link).text) is None


def test_an_expired_or_unknown_token_gets_the_plain_thank_you(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    deal.thanks_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    expired = anon_client.get(link)
    assert expired.status_code == 200 and terms_table(expired.text) is None
    assert "A member of our team will be in touch soon" in expired.text
    for path in ("/apply/thanks/no-such-token", "/apply/thanks/" + "x" * 80):
        unknown = anon_client.get(path)
        assert unknown.status_code == 200 and terms_table(unknown.text) is None
        assert "A member of our team will be in touch soon" in unknown.text
    db_session.expire_all()
    assert deal.indicative_terms_shown_at is None
    trail = list(db_session.scalars(select(AuditLog).where(AuditLog.row_id == str(deal.id))))
    assert len(trail) == 5  # nothing recorded for a page that showed nothing


def test_what_was_shown_is_recorded_on_the_deal_once_and_the_team_sees_it(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client, lang="es"))
    (deal,) = deals(db_session)
    _, pricing = arrival(db_session, deal)
    assert pricing is not None
    assert deal.indicative_terms_shown_at is None

    before = datetime.now(UTC)
    body = anon_client.get(link).text
    db_session.expire_all()
    assert deal.indicative_terms_shown_at is not None
    assert before - timedelta(seconds=5) <= deal.indicative_terms_shown_at
    record = deal.indicative_terms_shown
    assert record is not None
    assert record["language"] == "es" and record["with_profit"] is True
    assert [row["label"] for row in record["rows"]] == ["Requested", "-5%", "-10%"]
    for shown, row in zip(record["rows"], pricing.rows, strict=True):
        assert Decimal(shown["loan_amount"]) == row.loan_amount
        assert Decimal(shown["interest_rate"]) == row.interest_rate
        assert Decimal(shown["profit"]) == row.borrower_profit
        assert Decimal(shown["cash_on_cash"]) == row.borrower_cash_on_cash
        assert shown["is_request"] is (row.step == 0)
    # ...in the audit trail, as the borrower
    rows = list(
        db_session.scalars(
            select(AuditLog).where(AuditLog.action == AuditAction.INDICATIVE_TERMS_SHOWN.value)
        )
    )
    assert len(rows) == 1
    assert rows[0].actor == "web:maria@ortizbuilds.example" and rows[0].after == record

    # a refresh, in the other language even, records nothing new
    first_shown = deal.indicative_terms_shown_at
    again = anon_client.get(f"{link.split('?')[0]}?lang=en").text
    assert terms_table(again) is not None
    db_session.expire_all()
    assert deal.indicative_terms_shown_at == first_shown
    assert deal.indicative_terms_shown == record
    trail = list(db_session.scalars(select(AuditLog).where(AuditLog.row_id == str(deal.id))))
    assert [row.action for row in trail].count(AuditAction.INDICATIVE_TERMS_SHOWN.value) == 1
    assert len(trail) == 6

    # the deal page shows it under Pricing, as rendered
    page = client.get(f"/queue/deals/{deal.id}").text
    pricing_section = between(page, 'id="pricing"', "</section>")
    assert pricing_section is not None
    shown_block = pricing_section[pricing_section.index('<div id="shown-to-borrower">') :]
    assert "Shown to borrower" in shown_block
    assert "<dt>Language</dt><dd>Spanish</dd>" in shown_block
    assert "Nothing shown to the borrower." not in shown_block
    assert re.findall(r'<th scope="row">([^<]+)</th>', shown_block) == ["Requested", "-5%", "-10%"]
    for row in pricing.rows:
        assert dollars(row.loan_amount) in shown_block
        assert quoted(row.interest_rate) in shown_block
        assert dollars(row.borrower_profit) in shown_block
        assert pct(row.borrower_cash_on_cash) in shown_block
    # and the Spanish page the borrower saw read the same numbers
    table = terms_table(body)
    assert table is not None
    for row in pricing.rows:
        assert dollars(row.loan_amount) in table and quoted(row.interest_rate) in table


def test_the_table_reads_in_spanish(anon_client: QueueClient, db_session: Session) -> None:
    link = thanks_link(post(anon_client, lang="es"))
    assert link.endswith("?lang=es")
    body = anon_client.get(link).text
    table = terms_table(body)
    assert table is not None
    assert "<h2>Términos indicativos</h2>" in table
    assert table.index(DISCLAIMER_ES) < table.index("<table>")
    assert table.index("</table>") < table.index("Solo para préstamos con fines comerciales.")
    for cell in (
        "Monto del préstamo",
        "Tasa",
        "Ganancia estimada",
        "Rendimiento (efectivo sobre efectivo)",
    ):
        assert f">{cell}</th>" in table
    assert re.findall(r'<th scope="row">([^<]+)</th>', table) == ["Solicitado", "-5%", "-10%"]
    assert "Requested" not in table and DISCLAIMER_EN not in table
    assert f'href="{link.split("?")[0]}?lang=en"' in body

    # the no-sale-price line, in Spanish
    second = thanks_link(post(anon_client, lang="es", estimated_sale_price=""))
    table = terms_table(anon_client.get(second).text)
    assert table is not None
    assert "Agregue un precio de venta estimado" in table
    assert "Ganancia estimada" not in table
