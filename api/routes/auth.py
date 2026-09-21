"""Sign in and sign out of the review queue.  # SPEC §11

Two pages and a post each. There is deliberately nothing else: no self-signup, no password
reset, no invite flow. Accounts come from ``glenwood users create``, which means the set of
people who can read credit and court data is a list someone maintains on purpose.

A failed sign-in says "email or password is wrong" whatever went wrong - unknown address,
bad password, deactivated account - so the page cannot be used to find out which addresses
have accounts. ``services.authenticate`` already answers that way; this only has to not
undo it.

Both writes record an ``audit_log`` row. SPEC §11 requires access to be logged, and a sign-in
is the access: every deal action after it carries the same actor, so the trail runs from the
sign-in through what the person then did.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from api.forms import FormDep, fields, safe_next
from api.render import page, redirect
from api.security import MaybeUser, attach, clear, over_https, require_csrf, signed_in
from db.session import get_session
from schema.models import AuditAction
from services import USERS, authenticate, record_audit

router = APIRouter(tags=["auth"])

SessionDep = Annotated[Session, Depends(get_session)]
QUEUE_PATH = "/queue"
WRONG = "Email or password is wrong, or the account is no longer active."


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
