"""Sign in, sign out, and change your own password.  # SPEC §11

Two pages and a post each, plus the one page a signed-in person has for themselves: Change
password, which asks for the current password and the new one twice. There is deliberately
nothing else: no self-signup, no reset-by-email, no invite flow. Accounts come from
``glenwood users create`` and a forgotten password from ``glenwood users reset-password``,
which means the set of people who can read credit and court data is a list someone maintains
on purpose.

A changed password ends every other session on the account (``api/security.py``): the
session that made the change is handed a fresh cookie with the redirect, so the person who
pressed the button stays signed in and everyone holding an older cookie does not.

A failed sign-in says "email or password is wrong" whatever went wrong - unknown address,
bad password, deactivated account - so the page cannot be used to find out which addresses
have accounts. ``services.authenticate`` already answers that way; this only has to not
undo it.

Both writes record an ``audit_log`` row. SPEC §11 requires access to be logged, and a sign-in
is the access: every deal action after it carries the same actor, so the trail runs from the
sign-in through what the person then did.

The sign-in also writes two lines to the process log, one when the credentials are accepted
and one when the cookie goes out with the redirect. They are there for the deploy's own
logs: a 500 that lands on the request *after* a sign-in has nothing on the deal page to say
what went wrong, and the two lines bracket exactly where the request before it ended - so a
traceback that follows them belongs to the redirected page, not to the sign-in.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from api.forms import FormDep, fields, safe_next
from api.render import page, redirect
from api.security import (
    MaybeUser,
    PageUser,
    attach,
    clear,
    over_https,
    require_csrf,
    signed_in,
)
from db.session import get_session
from schema.models import AuditAction
from services import USERS, WeakPassword, WrongPassword, authenticate, change_password, record_audit
from services.passwords import MIN_LENGTH

router = APIRouter(tags=["auth"])

SessionDep = Annotated[Session, Depends(get_session)]
QUEUE_PATH = "/queue"
PASSWORD_PATH = "/account/password"
log = logging.getLogger("glenwood.auth")
WRONG = "Email or password is wrong, or the account is no longer active."
MISMATCH = "The two new passwords do not match."
WRONG_CURRENT = "The current password is wrong."
CHANGED = "Password changed. Every other session on your account has been signed out."


@router.get("/login", response_class=HTMLResponse, response_model=None)
def sign_in_page(request: Request, user: MaybeUser) -> HTMLResponse | RedirectResponse:
    """The sign-in form; someone already signed in goes straight to the queue."""
    if user is not None:
        return redirect(QUEUE_PATH)
    return page(
        request,
        "login.html",
        {
            "email": "",
            "next_url": safe_next(request.query_params.get("next"), QUEUE_PATH),
            "problems": [],
        },
    )


@router.post("/login", response_model=None)
def sign_in(
    request: Request, form: FormDep, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Check the credentials, set the session cookie, record the access."""
    submitted = fields(form)
    email = submitted.get("email", "")
    target = safe_next(submitted.get("next"), QUEUE_PATH)
    user = authenticate(session, email=email, password=submitted.get("password", ""))
    if user is None:
        return page(
            request,
            "login.html",
            {"email": email, "next_url": target, "problems": [WRONG]},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    log.info("sign-in accepted for user %s", user.id)
    record_audit(
        session,
        actor=user.email,
        action=AuditAction.SIGNED_IN,
        table_name=USERS,
        row_id=user.id,
        after={"email": user.email},
    )
    session.commit()
    response = redirect(target)
    attach(response, user, secure=over_https(request))
    log.info(
        "sign-in recorded for user %s; redirecting to %s (cookie secure=%s)",
        user.id,
        target,
        over_https(request),
    )
    return response


def password_page(
    request: Request, user: PageUser, problems: list[str], status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    return page(
        request,
        "password.html",
        {"min_length": MIN_LENGTH, "problems": problems},
        user=user,
        status_code=status_code,
    )


@router.get(PASSWORD_PATH, response_class=HTMLResponse, response_model=None)
def change_password_page(request: Request, user: PageUser) -> HTMLResponse:
    """The signed-in person's own page: the current password, the new one twice."""
    return password_page(request, user, [])


@router.post(PASSWORD_PATH, response_model=None, dependencies=[Depends(require_csrf)])
def change_password_action(
    request: Request, form: FormDep, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Change the password, record it, re-issue this session's cookie; or say what was wrong.

    The two new boxes are compared here, before anything is verified or hashed: a typo in
    one of them is the common case and costs nothing to catch. The current password is then
    the service's to check (``services.change_password``), so a stolen open session cannot
    set a new one.
    """
    submitted = fields(form)
    new = submitted.get("new_password", "")
    if new != submitted.get("new_password_again", ""):
        return password_page(request, user, [MISMATCH], status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        change_password(
            session,
            user,
            current=submitted.get("current_password", ""),
            new=new,
            actor=user.email,
        )
    except WrongPassword:
        session.rollback()
        return password_page(request, user, [WRONG_CURRENT], status.HTTP_422_UNPROCESSABLE_CONTENT)
    except WeakPassword as exc:
        session.rollback()
        return password_page(
            request,
            user,
            [f"{str(exc)[0].upper()}{str(exc)[1:]}."],
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    session.commit()
    response = redirect(QUEUE_PATH, CHANGED)
    # Every cookie issued under the old password is dead now, this one included; the person
    # who pressed the button gets a fresh one with the redirect and never notices.
    attach(response, user, secure=over_https(request))
    return response


# Signing somebody out from another site is a nuisance attack rather than a theft, but it
# is still a state change on a session, so it carries a token like everything else.
@router.post("/logout", response_model=None, dependencies=[Depends(require_csrf)])
def sign_out(request: Request, session: SessionDep) -> RedirectResponse:
    """Drop the cookie. Signing out of a session nobody is in is a no-op, not an error."""
    user = signed_in(request, session)
    if user is not None:
        record_audit(
            session,
            actor=user.email,
            action=AuditAction.SIGNED_OUT,
            table_name=USERS,
            row_id=user.id,
            after={"email": user.email},
        )
        session.commit()
    response = redirect("/login", "Signed out.")
    clear(response, secure=over_https(request))
    return response
