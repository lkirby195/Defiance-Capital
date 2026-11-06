"""The security headers on every response, and HSTS over HTTPS only.  # SPEC §11 (v0.26)

Every kind of response the app gives is asked for - a page, a redirect, a 401, a 403, a
404, a file off the ``/static`` mount, the public form and each of its answers - because a
header set by a middleware is only as good as the responses that pass through it.
``over_https`` is the same test the session cookie's ``Secure`` uses, so a request that
arrives through Render's proxy with ``X-Forwarded-Proto: https`` gets both and a plain-http
one gets neither.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import HSTS, SECURITY_HEADERS
from api.ratelimit import LIMITER
from tests.conftest import QueueClient, requires_db

pytestmark = requires_db

EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
}
TOKEN = "no-such-token"
NO_DEAL = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def fresh_limiter() -> Any:
    LIMITER.reset()
    yield
    LIMITER.reset()


def every_kind_of_response(
    anon: TestClient, signed_in: TestClient, headers: dict[str, str]
) -> dict[str, Any]:
    def get(client: TestClient, path: str) -> Any:
        return client.get(path, headers=headers, follow_redirects=False)

    def post(client: TestClient, path: str, **kwargs: Any) -> Any:
        return client.post(path, headers=headers, follow_redirects=False, **kwargs)

    return {
        "sign-in page": get(anon, "/login"),
        "a failed sign-in": post(anon, "/login", data={"email": "x@y.example", "password": "z"}),
        "a page, signed in": get(signed_in, "/queue"),
        "a page, sent to sign in": get(anon, "/queue"),
        "a JSON 401": get(anon, f"/deals/{NO_DEAL}"),
        "a CSRF refusal": post(signed_in, f"/queue/deals/{NO_DEAL}/note", data={"_csrf": ""}),
        "a 404": get(anon, "/no-such-page"),
        "the docs, off": get(anon, "/docs"),
        "a static file": get(anon, "/static/brand/logo-bar.png"),
        "the form": get(anon, "/apply"),
        "a rejected form": post(anon, "/apply", data={"lang": "en"}),
        "the plain thank-you": get(anon, "/apply/thanks"),
        "a thank-you link": get(anon, f"/apply/thanks/{TOKEN}"),
        "the adjust page": get(anon, f"/apply/thanks/{TOKEN}/adjust"),
        "an adjust post": post(anon, f"/apply/thanks/{TOKEN}/adjust", data={"lang": "en"}),
    }


def test_the_constants_are_the_brief_s() -> None:
    assert {name.lower(): value for name, value in SECURITY_HEADERS.items()} == EXPECTED
    assert HSTS == "max-age=31536000; includeSubDomains"
    # frame-ancestors only: a script-src would have to keep up with the Turnstile script
    assert "script-src" not in SECURITY_HEADERS["Content-Security-Policy"]


def test_every_response_carries_the_four_headers_and_no_hsts_over_http(
    client: QueueClient,
) -> None:
    # ``client`` is the conftest's anonymous client signed in, so the stranger is a second
    # client on the same app (and the same database override)
    with TestClient(client.app) as anon:
        responses = every_kind_of_response(anon, client, {})
    codes = {name: response.status_code for name, response in responses.items()}
    assert codes["a static file"] == 200
    assert codes["a CSRF refusal"] == 403
    assert codes["a 404"] == codes["the docs, off"] == 404
    assert codes["a JSON 401"] == 401
    assert codes["a rejected form"] == 422
    assert codes["a page, sent to sign in"] == 303
    for name, response in responses.items():
        for header, value in EXPECTED.items():
            assert response.headers.get(header) == value, (name, header)
        assert "strict-transport-security" not in response.headers, name


def test_over_https_every_response_also_carries_hsts(client: QueueClient) -> None:
    with TestClient(client.app) as anon:
        responses = every_kind_of_response(anon, client, {"X-Forwarded-Proto": "https"})
    for name, response in responses.items():
        assert response.headers.get("strict-transport-security") == HSTS, name
        for header, value in EXPECTED.items():
            assert response.headers.get(header) == value, (name, header)


def test_the_last_forwarded_proto_hop_is_the_one_believed(anon_client: QueueClient) -> None:
    """The nearest proxy's word, as ``client_address`` takes the last X-Forwarded-For hop."""
    said = {
        "https": True,
        "HTTPS": True,
        "http, https": True,
        "https, http": False,
        "http": False,
        "": False,
    }
    for value, secure in said.items():
        response = anon_client.get("/login", headers={"X-Forwarded-Proto": value})
        assert ("strict-transport-security" in response.headers) is secure, value
