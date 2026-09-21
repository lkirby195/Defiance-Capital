"""Who is signed in.  # SPEC §11

A signed cookie, nothing more. The cookie carries the user id and an expiry, and an
HMAC-SHA256 over both keyed by ``SESSION_SECRET``; there is no server-side session table,
because there is nothing in a session worth storing - the user id is the whole of it.

The trade that buys is the usual one: signing out clears the cookie on this browser and does
not invalidate a copy of it taken elsewhere. What closes that hole is that the user row is
loaded and its ``active`` flag checked on every single request, so deactivating a user
(``glenwood users deactivate``) stops every live session at once rather than at its next
expiry. For a two-person internal queue that is the right shape; a stolen cookie is a
deactivation away from useless.

``SameSite=Lax`` is what stops another site posting a form at this one as the signed-in user:
Lax withholds the cookie from cross-site POSTs, which is every state-changing route here.
``HttpOnly`` keeps it away from page scripts, of which there is one, and it does not touch
cookies. ``Secure`` is set whenever the request arrived over HTTPS, so a deployment behind
TLS marks the cookie correctly without a setting to forget and a local ``uvicorn`` over
plain HTTP still works.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from api.forms import is_form_post
from db.models import User
from db.session import DOTENV_PATH, get_session
from services import get_user

COOKIE_NAME = "glenwood_session"
# Long enough for a working day in the queue, short enough that a forgotten laptop expires.
SESSION_HOURS = 12
SIGN_IN_PATH = "/login"


class NotSignedIn(Exception):
    """No valid session on the request; the handler decides what the caller sees."""

    def __init__(self, next_url: str | None = None) -> None:
        super().__init__("not signed in")
        self.next_url = next_url


def session_secret() -> str:
    """``SESSION_SECRET``; a real environment variable wins over ``.env``.

    Refused rather than defaulted. A default secret is a signing key every copy of this
    repository knows, which is the same as no signature at all.
    """
    load_dotenv(DOTENV_PATH, override=False)
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "SESSION_SECRET is not set. Copy .env.example to .env and fill it in with a "
            'random string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`'
        )
    return secret


def _sign(payload: str) -> str:
    digest = hmac.new(session_secret().encode("utf-8"), payload.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue(user: User, now: datetime | None = None) -> str:
    """The cookie value for a user's session."""
    expires = (now or datetime.now(UTC)) + timedelta(hours=SESSION_HOURS)
    payload = f"{user.id}.{int(expires.timestamp())}"
    return f"{payload}.{_sign(payload)}"


@dataclass(frozen=True)
class SessionClaim:
    """What a valid cookie says. Says nothing about whether the user still exists."""

    user_id: UUID
    expires_at: datetime


def read(cookie: str | None, now: datetime | None = None) -> SessionClaim | None:
    """The claim in a cookie, or None if it is absent, malformed, forged or expired."""
    if not cookie:
        return None
    parts = cookie.split(".")
    if len(parts) != 3:
        return None
    user_text, expiry_text, signature = parts
    if not hmac.compare_digest(_sign(f"{user_text}.{expiry_text}"), signature):
        return None
    try:
        user_id = UUID(user_text)
        expires_at = datetime.fromtimestamp(int(expiry_text), UTC)
    except ValueError:
        return None
    if expires_at <= (now or datetime.now(UTC)):
        return None
    return SessionClaim(user_id=user_id, expires_at=expires_at)


def over_https(request: Request) -> bool:
    """True when this request arrived over HTTPS, so the cookie is marked ``Secure``."""
    return request.url.scheme == "https"


def attach(response: Response, user: User, *, secure: bool) -> None:
    """Put a fresh session cookie on the response."""
    response.set_cookie(
        COOKIE_NAME,
        issue(user),
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear(response: Response, *, secure: bool) -> None:
    """Remove the session cookie. The attributes have to match the ones it was set with."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=secure)


def signed_in(request: Request, session: Session) -> User | None:
    """The signed-in, still-active user, or None.

    Both halves matter. The signature says the cookie is ours and unexpired; the row says
    the person is still allowed in. A deactivated user holding a valid cookie is None here,
    which is the whole point of not keeping sessions server-side.
    """
    claim = read(request.cookies.get(COOKIE_NAME))
    if claim is None:
        return None
    user = get_user(session, claim.user_id)
    if user is None or not user.active:
        return None
    return user


def sign_in_url(request: Request) -> str:
    """``/login`` with the page the caller was trying to reach, so it can return there."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if target in {"/", SIGN_IN_PATH}:
        return SIGN_IN_PATH
    return f"{SIGN_IN_PATH}?next={quote(target, safe='')}"


def page_user(request: Request, session: Annotated[Session, Depends(get_session)]) -> User:
    """The signed-in user for an HTML page; ``NotSignedIn`` sends a browser to sign in."""
    user = signed_in(request, session)
    if user is None:
        raise NotSignedIn(sign_in_url(request))
    return user


def api_user(request: Request, session: Annotated[Session, Depends(get_session)]) -> User:
    """The signed-in user for a JSON route; 401 rather than a redirect a client cannot use."""
    user = signed_in(request, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="sign in at /login; this endpoint needs a session cookie",
        )
    return user


def maybe_user(request: Request, session: Annotated[Session, Depends(get_session)]) -> User | None:
    """The signed-in user if there is one; used by the sign-in page itself."""
    return signed_in(request, session)


PageUser = Annotated[User, Depends(page_user)]
ApiUser = Annotated[User, Depends(api_user)]
MaybeUser = Annotated["User | None", Depends(maybe_user)]


def posted_user(request: Request, session: Annotated[Session, Depends(get_session)]) -> User:
    """The signed-in user for a route that takes both a form post and a JSON post.

    A browser gets the redirect it can act on; a JSON client gets the 401 it can act on.
    ``/intake/team`` is the one route with two callers, and neither should be handed the
    other's failure.
    """
    user = signed_in(request, session)
    if user is not None:
        return user
    if is_form_post(request):
        raise NotSignedIn(SIGN_IN_PATH)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="sign in at /login; this endpoint needs a session cookie",
    )


PostedUser = Annotated[User, Depends(posted_user)]
