"""The public borrower form.  # SPEC §4.2

Three routes and no session: ``GET /apply`` is the form, ``POST /apply`` stores it, and
``GET /apply/thanks`` is the one thing a borrower is ever told. A deal posted here lands in
the queue as the normalizer decides (SPEC §4.1) - ``NEEDS_INFO`` when something is still
missing, else screened and priced on the way in (``services/autorun.py``, SPEC §4.6) on the
config defaults and the borrower's own numbers, so the team opens it on a verdict. What it
says stays on the team's side of the wall: the thank-you page says the team will be in
touch, and never a verdict, a rate or an amount, because a borrower who has been told "Go"
by a web page has been told something nobody at GLENWOOD has decided.

Nothing here is behind the session cookie, which is the whole point, and so nothing here
carries a CSRF token either: a token is bound to a signed-in user (``api/security.py``) and
there is nobody to bind one to. What stands in for both is that the form can write exactly
one kind of row - a new deal - and two things stand in front of it: a honeypot box a person
never sees, which drops the post silently when a bot fills it, and a per-address rate limit
(``api/ratelimit.py``) that turns the sixth post in an hour away with a polite line. No
CAPTCHA.

The language is the borrower's choice. ``?lang=`` wins when they pressed the toggle, the
browser's ``Accept-Language`` decides otherwise, and the choice rides on a hidden field so a
rejected post comes back in the language it was typed in. ``?src=`` is the slug on the link
they followed - a flyer, a partner, a campaign - and is recorded on the deal as its source.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from api.apply_form import (
    HONEYPOT,
    LANGUAGE_FIELD,
    REQUIRED_NAMES,
    SOURCE_FIELD,
    blank_values,
    clean_source,
    credit_choices,
    experience_choices,
    intake_record,
    read_form,
    redisplay_values,
    state_choices,
    term_choices,
)
from api.forms import FormDep, fields
from api.i18n import choose_language, other_language, strings
from api.problems import FormProblems, at_top
from api.ratelimit import LIMITER, client_address
from api.render import page, redirect
from config.config import get_config
from db.session import get_session
from services import auto_run, create_deal

router = APIRouter(tags=["apply"])

SessionDep = Annotated[Session, Depends(get_session)]

APPLY_PATH = "/apply"
THANKS_PATH = "/apply/thanks"
# The audit actor for a deal the borrower typed in themselves (SPEC §11): named by the email
# they gave, so the trail says who, and prefixed so it can never be mistaken for a team member.
WEB_ACTOR_PREFIX = "web:"


def _language(request: Request, posted: dict[str, str] | None = None) -> str:
    requested = (posted or {}).get(LANGUAGE_FIELD) or request.query_params.get(LANGUAGE_FIELD)
    return choose_language(requested, request.headers.get("accept-language"))


def apply_page(
    request: Request,
    *,
    language: str,
    source: str | None,
    form: dict[str, str],
    complaints: FormProblems,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The form, blank or put back with what a rejected post was carrying."""
    t = strings(language)
    other = other_language(language)
    query = f"?{LANGUAGE_FIELD}={other}" + (f"&{SOURCE_FIELD}={source}" if source else "")
    context: dict[str, Any] = {
        "t": t,
        "language": language,
        "toggle_href": f"{APPLY_PATH}{query}",
        "source": source or "",
        "form": form,
        "required": REQUIRED_NAMES,
        "honeypot": HONEYPOT,
        "credit_choices": credit_choices(t),
        "experience_choices": experience_choices(),
        "term_choices": term_choices(t),
        "state_choices": state_choices(t),
        "problems": complaints,
    }
    return page(request, "apply.html", context, status_code=status_code)


@router.get(APPLY_PATH, response_class=HTMLResponse, response_model=None)
def apply_form(request: Request) -> HTMLResponse:
    """The public form, in the borrower's language, remembering the link's source."""
    return apply_page(
        request,
        language=_language(request),
        source=clean_source(request.query_params.get(SOURCE_FIELD)),
        form=blank_values(),
        complaints=FormProblems(),
    )


@router.post(APPLY_PATH, response_model=None)
def apply_submit(
    request: Request, session: SessionDep, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Store the deal and send the borrower to the thank-you page; or put the form back."""
    posted = fields(form)
    language = _language(request, posted)
    source = clean_source(posted.get(SOURCE_FIELD))
    t = strings(language)
    thanks = f"{THANKS_PATH}?{LANGUAGE_FIELD}={language}"

    def back(problems: FormProblems, code: int) -> HTMLResponse:
        return apply_page(
            request,
            language=language,
            source=source,
            form=redisplay_values(posted),
            complaints=problems,
            status_code=code,
        )

    limit = get_config().web_intake.submissions_per_hour_per_ip
    if not LIMITER.allow(client_address(request), limit):
        return back(at_top(t["rate_limited"]), status.HTTP_429_TOO_MANY_REQUESTS)
    if posted.get(HONEYPOT):
        # A box no person can see was filled in. Nothing is stored, and the sender is shown
        # the same page a real borrower would be, so there is nothing to learn from the reply.
        return redirect(thanks)

    entry, problems = read_form(posted, t)
    if entry is None:
        return back(problems, status.HTTP_422_UNPROCESSABLE_CONTENT)
    record = intake_record(entry, language=language, intake_source=source)
    deal = create_deal(session, record, actor=f"{WEB_ACTOR_PREFIX}{entry.borrower_email}")
    # Screened and priced on arrival, as the system; the borrower is still told nothing.
    auto_run(session, deal)
    session.commit()
    return redirect(thanks)


@router.get(THANKS_PATH, response_class=HTMLResponse, response_model=None)
def apply_thanks(request: Request) -> HTMLResponse:
    """The one thing a borrower is told: the team will be in touch."""
    language = _language(request)
    return page(
        request,
        "apply_thanks.html",
        {
            "t": strings(language),
            "language": language,
            "toggle_href": f"{THANKS_PATH}?{LANGUAGE_FIELD}={other_language(language)}",
            "again_href": f"{APPLY_PATH}?{LANGUAGE_FIELD}={language}",
        },
    )
