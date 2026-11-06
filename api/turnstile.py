"""Cloudflare Turnstile on the public borrower form.  # SPEC §4.2 (v0.26)

The honeypot catches the bots that fill every box and the rate limit the ones that post too
often from one address; Turnstile is for the rest. The widget on ``/apply`` and on Adjust my
numbers (``templates/apply.html``, ``apply_adjust.html``) puts a token in the form as
``cf-turnstile-response``, and the server asks Cloudflare whether it is good before reading
the form (``api/routes/apply.py``: rate limit, honeypot, this, then the form). The page never
holds the submit button off for it; the server is the one that answers.

**Off unless both keys are set**: ``TURNSTILE_SITE_KEY`` (public, printed on the page) and
``TURNSTILE_SECRET_KEY`` (sent to Cloudflare and nowhere else), read like
``services.market_data.rentcast_api_key`` - the environment first, ``.env`` second. Off, there
is no widget, no script and no check. ``tests/conftest.py`` parks both empty, so no test
reaches Cloudflare; a test that wants it on sets both and serves siteverify from an
``httpx.MockTransport`` through ``turnstile_transport``.

**It fails open.** A missing token, or one Cloudflare answers ``success: false`` for, is
refused. But Cloudflare unreachable, slow past ``TIMEOUT`` or answering anything but a 200
with a JSON body lets the post through, with a warning on ``glenwood.turnstile``: a borrower
is not turned away for somebody else's outage, and the honeypot and the rate limit still
stand while it lasts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from fastapi import Depends

from db.session import DOTENV_PATH

SITE_KEY_ENV = "TURNSTILE_SITE_KEY"
SECRET_KEY_ENV = "TURNSTILE_SECRET_KEY"
SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# The form field the widget writes its token into.
RESPONSE_FIELD = "cf-turnstile-response"
# As adapters/rentcast.TIMEOUT: a borrower is waiting on the answer, so not long, and a
# timeout is an outage, which lets the post through.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Cloudflare's codes for a request that was the deployment's fault, not the borrower's: worth
# a warning, because with them every borrower is being refused.
MISCONFIGURED = frozenset({"missing-input-secret", "invalid-input-secret"})

log = logging.getLogger("glenwood.turnstile")


@dataclass(frozen=True)
class TurnstileKeys:
    site_key: str
    secret_key: str


def turnstile_keys(environ: Mapping[str, str] | None = None) -> TurnstileKeys | None:
    """Both keys, or None when either is unset or empty - which is Turnstile off.

    An explicit ``environ`` is read as the whole environment and ``.env`` is not consulted;
    without one, ``.env`` fills in what the environment does not have, and a variable that is
    present, even empty, is left alone - which is how the tests keep it off.
    """
    if environ is None:
        load_dotenv(DOTENV_PATH, override=False)
        environ = os.environ
    site = environ.get(SITE_KEY_ENV, "").strip()
    secret = environ.get(SECRET_KEY_ENV, "").strip()
    return TurnstileKeys(site_key=site, secret_key=secret) if site and secret else None


def site_key() -> str | None:
    """The key the widget is drawn with, or None when Turnstile is off."""
    keys = turnstile_keys()
    return keys.site_key if keys is not None else None


def turnstile_transport() -> httpx.BaseTransport | None:
    """The transport siteverify goes over: httpx's own, unless a test overrides this."""
    return None


TransportDep = Annotated[httpx.BaseTransport | None, Depends(turnstile_transport)]


class Challenge(StrEnum):
    """What the check said, as the route acts on it."""

    OFF = "OFF"  # no keys: nothing was asked
    PASSED = "PASSED"
    FAILED = "FAILED"  # no token, or Cloudflare said no: the post is refused
    UNREACHED = "UNREACHED"  # Cloudflare could not answer: the post goes through


def check(
    posted: Mapping[str, str],
    remote_ip: str,
    *,
    transport: httpx.BaseTransport | None = None,
    keys: TurnstileKeys | None = None,
) -> Challenge:
    """Ask Cloudflare about the token this post carries.  # SPEC §4.2

    ``keys`` defaults to ``turnstile_keys()``. A post without a token is refused without a
    request: the widget always writes one, so its absence is a form that never ran it.
    """
    keys = keys if keys is not None else turnstile_keys()
    if keys is None:
        return Challenge.OFF
    token = posted.get(RESPONSE_FIELD, "").strip()
    if not token:
        log.info("turnstile: no token on the post from %s; refused", remote_ip)
        return Challenge.FAILED
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.post(
                VERIFY_URL,
                data={"secret": keys.secret_key, "response": token, "remoteip": remote_ip},
            )
    except httpx.HTTPError as exc:
        log.warning("turnstile: siteverify unreachable (%s); letting the post through", exc)
        return Challenge.UNREACHED
    if response.status_code != httpx.codes.OK:
        log.warning(
            "turnstile: siteverify answered HTTP %s; letting the post through",
            response.status_code,
        )
        return Challenge.UNREACHED
    try:
        body: Any = response.json()
    except ValueError:
        log.warning("turnstile: siteverify answered a body that is not JSON; letting it through")
        return Challenge.UNREACHED
    if isinstance(body, dict) and body.get("success") is True:
        return Challenge.PASSED
    codes = body.get("error-codes", []) if isinstance(body, dict) else []
    codes = [str(code) for code in codes] if isinstance(codes, list) else [str(codes)]
    if MISCONFIGURED.intersection(codes):
        log.warning("turnstile: the secret key was refused (%s); check the deployment", codes)
    else:
        log.info("turnstile: refused the post from %s (%s)", remote_ip, codes)
    return Challenge.FAILED
