"""Cloudflare Turnstile on /apply and Adjust my numbers.  # SPEC §4.2 (v0.26)

Off is how every other test runs (``tests/conftest.py`` parks both keys empty), so it is
checked once here and then turned on with both keys and a fake siteverify on an
``httpx.MockTransport``, handed to the routes through ``api.turnstile.turnstile_transport``.
Nothing here reaches Cloudflare. What is held: the widget is drawn above the submit button
only when on; a passing token lets the post through, a failing or missing one is refused
in the page's language with nothing stored; an outage of any kind, or a refused secret, lets
the post through with a warning; and the rate limit and the honeypot still come first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy.orm import Session

from api.apply_form import HONEYPOT
from api.i18n import STRINGS
from api.main import app
from api.ratelimit import LIMITER
from api.turnstile import (
    RESPONSE_FIELD,
    SCRIPT_URL,
    SECRET_KEY_ENV,
    SITE_KEY_ENV,
    VERIFY_URL,
    Challenge,
    TurnstileKeys,
    check,
    turnstile_keys,
    turnstile_transport,
)
from config.config import get_config
from tests.conftest import QueueClient, requires_db
from tests.test_apply import THANKS_LINK, VALID, deals, thanks_link

pytestmark = requires_db

SITE = "test-site-key-not-a-real-one"
SECRET = "test-secret-key-not-a-real-one"
TOKEN = "a-token-the-widget-wrote"
# The address Render's proxy appends after whatever the client sent.
ADDRESS = "198.51.100.7"
FAILED_EN, FAILED_ES = STRINGS["challenge_failed"]
ADJUST = {
    "lang": "en",
    "estimated_sale_price": "$300,000",
    "rehab_costs": "$45,000",
    "loan_requested": "$150,000",
    "term_bucket": "9",
}


@pytest.fixture(autouse=True)
def fresh_limiter() -> Iterator[None]:
    LIMITER.reset()
    yield
    LIMITER.reset()


class Siteverify:
    """A fake siteverify: records each request it is sent and answers as it was told."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def forms(self) -> list[dict[str, list[str]]]:
        return [parse_qs(request.content.decode("ascii")) for request in self.requests]


def says(body: Any, status_code: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status_code, json=body)


PASS = says({"success": True, "error-codes": []})
FAIL = says({"success": False, "error-codes": ["invalid-input-response"]})


@pytest.fixture
def turnstile_on(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Siteverify]:
    """Both keys set; returns a function that serves siteverify with the given answer."""
    monkeypatch.setenv(SITE_KEY_ENV, SITE)
    monkeypatch.setenv(SECRET_KEY_ENV, SECRET)

    def serve(answer: Callable[[httpx.Request], httpx.Response] = PASS) -> Siteverify:
        fake = Siteverify(answer)
        app.dependency_overrides[turnstile_transport] = lambda: httpx.MockTransport(fake)
        return fake

    return serve


def post(client: QueueClient, token: str | None = TOKEN, **changes: str) -> Any:
    data = {**VALID, **changes}
    if token is not None:
        data[RESPONSE_FIELD] = token
    return client.post(
        "/apply",
        data=data,
        headers={"X-Forwarded-For": f"10.0.0.1, {ADDRESS}"},
        follow_redirects=False,
    )


def adjust(client: QueueClient, base: str, token: str | None = TOKEN, **changes: str) -> Any:
    data = {**ADJUST, **changes}
    if token is not None:
        data[RESPONSE_FIELD] = token
    return client.post(f"{base}/adjust", data=data, follow_redirects=False)


# --- off ------------------------------------------------------------------------------------------


def test_the_keys_are_both_or_nothing() -> None:
    both = {SITE_KEY_ENV: SITE, SECRET_KEY_ENV: SECRET}
    assert turnstile_keys(both) == TurnstileKeys(site_key=SITE, secret_key=SECRET)
    assert turnstile_keys({SITE_KEY_ENV: SITE}) is None
    assert turnstile_keys({SECRET_KEY_ENV: SECRET}) is None
    assert turnstile_keys({SITE_KEY_ENV: " ", SECRET_KEY_ENV: SECRET}) is None
    assert turnstile_keys({}) is None
    # and the suite runs with it off, whatever the shell or .env says
    assert turnstile_keys() is None
    assert check({}, ADDRESS) is Challenge.OFF


def test_off_there_is_no_widget_no_script_and_no_check(
    anon_client: QueueClient, db_session: Session
) -> None:
    form = anon_client.get("/apply").text
    assert "cf-turnstile" not in form and "challenges.cloudflare.com" not in form
    base = thanks_link(post(anon_client, token=None)).split("?")[0]
    page = anon_client.get(f"{base}/adjust").text
    assert 'id="adjust-form"' in page
    assert "cf-turnstile" not in page and "challenges.cloudflare.com" not in page
    assert adjust(anon_client, base, token=None).status_code == 303
    assert len(deals(db_session)) == 1


def test_one_key_alone_is_off(anon_client: QueueClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SITE_KEY_ENV, SITE)
    assert "cf-turnstile" not in anon_client.get("/apply").text
    assert post(anon_client, token=None).status_code == 303


# --- the widget -----------------------------------------------------------------------------------


def test_on_the_widget_sits_above_the_submit_button_in_the_page_s_language(
    anon_client: QueueClient, turnstile_on: Callable[..., Siteverify]
) -> None:
    turnstile_on()
    for language in ("en", "es"):
        body = anon_client.get(f"/apply?lang={language}").text
        widget = (
            f'<div class="cf-turnstile" data-sitekey="{SITE}" data-language="{language}" '
            'data-action="apply"></div>'
        )
        assert body.count(widget) == 1, language
        assert body.index(widget) < body.index('<button type="submit" data-requires-all>')
        assert body.count(f'<script src="{SCRIPT_URL}" async defer></script>') == 1
        # the secret never reaches a page
        assert SECRET not in body
    # the button is not held for it: the gate script reads [required], and the widget's
    # token box is the widget's own, not a required input of the form's
    assert f'name="{RESPONSE_FIELD}"' not in body


def test_the_adjust_page_has_its_own_action(
    anon_client: QueueClient, turnstile_on: Callable[..., Siteverify]
) -> None:
    turnstile_on()
    base = thanks_link(post(anon_client)).split("?")[0]
    body = anon_client.get(f"{base}/adjust?lang=es").text
    widget = (
        f'<div class="cf-turnstile" data-sitekey="{SITE}" data-language="es" '
        'data-action="adjust"></div>'
    )
    assert body.count(widget) == 1
    assert body.index(widget) < body.index('<button type="submit">')
    assert SCRIPT_URL in body and SECRET not in body


# --- the check ------------------------------------------------------------------------------------


def test_a_passing_token_lets_the_post_through_and_asks_with_the_real_address(
    anon_client: QueueClient, db_session: Session, turnstile_on: Callable[..., Siteverify]
) -> None:
    fake = turnstile_on(PASS)
    assert THANKS_LINK.fullmatch(thanks_link(post(anon_client)))
    assert len(deals(db_session)) == 1
    (request,) = fake.requests
    assert str(request.url) == VERIFY_URL and request.method == "POST"
    assert fake.forms() == [{"secret": [SECRET], "response": [TOKEN], "remoteip": [ADDRESS]}]


@pytest.mark.parametrize("language,line", [("en", FAILED_EN), ("es", FAILED_ES)])
def test_a_failed_token_is_refused_in_the_page_s_language_and_nothing_is_stored(
    anon_client: QueueClient,
    db_session: Session,
    turnstile_on: Callable[..., Siteverify],
    language: str,
    line: str,
) -> None:
    turnstile_on(FAIL)
    response = post(anon_client, lang=language)
    assert response.status_code == 422
    assert line in response.text
    # the form is put back as it was typed, with the widget drawn again to try once more
    assert 'value="María Elena Ortiz"' in response.text
    assert 'class="cf-turnstile"' in response.text
    assert deals(db_session) == []


def test_a_missing_token_is_refused_without_asking_cloudflare(
    anon_client: QueueClient, db_session: Session, turnstile_on: Callable[..., Siteverify]
) -> None:
    fake = turnstile_on(PASS)
    for token in (None, "", "   "):
        response = post(anon_client, token=token)
        assert response.status_code == 422, token
        assert FAILED_EN in response.text
    assert fake.requests == []
    assert deals(db_session) == []


def raises(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error

    return answer


@pytest.mark.parametrize(
    "answer",
    [
        raises(httpx.ConnectError("no route to Cloudflare")),
        raises(httpx.ReadTimeout("siteverify took too long")),
        says({"success": False}, status_code=500),
        says({"success": False}, status_code=429),
        lambda request: httpx.Response(200, text="<html>bad gateway</html>"),
    ],
    ids=["unreachable", "timeout", "500", "429", "not json"],
)
def test_an_outage_lets_the_post_through_with_a_warning(
    anon_client: QueueClient,
    db_session: Session,
    turnstile_on: Callable[..., Siteverify],
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    fake = turnstile_on(answer)
    caplog.set_level("WARNING", logger="glenwood.turnstile")
    assert THANKS_LINK.fullmatch(thanks_link(post(anon_client)))
    assert len(deals(db_session)) == 1
    assert len(fake.requests) == 1
    warnings = [r for r in caplog.records if r.name == "glenwood.turnstile"]
    assert warnings and all(r.levelname == "WARNING" for r in warnings)
    assert "letting" in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize(
    "code,through",
    [
        ("invalid-input-secret", True),
        ("missing-input-secret", True),
        ("invalid-input-response", False),
    ],
    ids=["bad secret", "no secret", "bad token"],
)
def test_a_refused_secret_lets_the_post_through_with_a_warning_and_a_refused_token_does_not(
    anon_client: QueueClient,
    db_session: Session,
    turnstile_on: Callable[..., Siteverify],
    caplog: pytest.LogCaptureFixture,
    code: str,
    through: bool,
) -> None:
    # a 200 either way: a secret Cloudflare refused is the deployment's fault, and turning
    # the borrower away for it would turn every borrower away
    fake = turnstile_on(says({"success": False, "error-codes": [code]}))
    caplog.set_level("INFO", logger="glenwood.turnstile")
    response = post(anon_client)
    assert len(fake.requests) == 1
    warned = any(
        r.levelname == "WARNING" and "secret key was refused" in r.getMessage()
        for r in caplog.records
    )
    if through:
        assert THANKS_LINK.fullmatch(thanks_link(response))
        assert len(deals(db_session)) == 1
        assert warned and "letting the post through" in caplog.text
    else:
        assert response.status_code == 422 and FAILED_EN in response.text
        assert deals(db_session) == []
        assert not warned
    assert SECRET not in caplog.text


# --- what comes first -----------------------------------------------------------------------------


def test_the_honeypot_still_wins_and_costs_no_call(
    anon_client: QueueClient, db_session: Session, turnstile_on: Callable[..., Siteverify]
) -> None:
    fake = turnstile_on(PASS)
    for token in (None, TOKEN):
        response = post(anon_client, token=token, **{HONEYPOT: "https://spam.example"})
        assert response.status_code == 303
        assert THANKS_LINK.fullmatch(response.headers["location"])
    assert fake.requests == []
    assert deals(db_session) == []


def test_the_rate_limit_comes_before_turnstile(
    anon_client: QueueClient, db_session: Session, turnstile_on: Callable[..., Siteverify]
) -> None:
    fake = turnstile_on(PASS)
    limit = get_config().web_intake.submissions_per_hour_per_ip
    for _ in range(limit):
        assert post(anon_client).status_code == 303
    assert post(anon_client).status_code == 429
    assert len(fake.requests) == limit
    assert len(deals(db_session)) == limit


# --- Adjust my numbers ----------------------------------------------------------------------------


def test_adjust_my_numbers_is_checked_the_same_way(
    anon_client: QueueClient, db_session: Session, turnstile_on: Callable[..., Siteverify]
) -> None:
    turnstile_on(PASS)
    base = thanks_link(post(anon_client)).split("?")[0]
    (deal,) = deals(db_session)

    turnstile_on(FAIL)
    refused = adjust(anon_client, base)
    assert refused.status_code == 422 and FAILED_EN in refused.text
    assert 'data-action="adjust"' in refused.text
    missing = adjust(anon_client, base, token=None, lang="es")
    assert missing.status_code == 422 and FAILED_ES in missing.text
    db_session.expire_all()
    assert len(deal.submissions) == 1

    # a honeypotted adjust is dropped before Turnstile is asked
    fake = turnstile_on(PASS)
    dropped = adjust(anon_client, base, **{HONEYPOT: "https://spam.example"})
    assert dropped.status_code == 303 and fake.requests == []

    saved = adjust(anon_client, base)
    assert saved.status_code == 303 and saved.headers["location"].startswith(f"{base}?lang=en")
    assert len(fake.requests) == 1
    db_session.expire_all()
    assert len(deal.submissions) == 2
