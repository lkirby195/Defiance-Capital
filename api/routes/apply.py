"""The public borrower form.  # SPEC §4.2

Four routes and no session: ``GET /apply`` is the form, ``POST /apply`` stores it, and
``GET /apply/thanks`` and ``GET /apply/thanks/{token}`` are the thank-you page - the second
the one a submission is sent to. A deal posted here lands in the queue as the normalizer
decides (SPEC §4.1) - ``NEEDS_INFO`` when something is still missing, else screened and
priced on the way in (``services/autorun.py``, SPEC §4.6) on the config defaults and the
borrower's own numbers, so the team opens it on a verdict. The borrower is shown the team
will be in touch and, under a disclaimer, their **indicative terms**: the loan amounts and
rates the pricing matrix quotes (profit and yield beside them when they gave a sale price),
only when the arrival run said Go or Conditional and only for ``web_intake.thanks_token_hours``
on the token the submission was sent to (``services/indicative.py``). Never a verdict, an
IRR or a flag: a borrower who has been told "Go" by a web page has been told something
nobody at GLENWOOD has decided. What the page showed, and when, is recorded on the deal.

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

import secrets
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
from db.models import Deal
from db.session import get_session
from services import (
    IndicativeTerms,
    auto_run,
    create_deal,
    deal_by_thanks_token,
    indicative_terms,
    issue_thanks_token,
    record_shown,
)

router = APIRouter(tags=["apply"])

SessionDep = Annotated[Session, Depends(get_session)]

APPLY_PATH = "/apply"
THANKS_PATH = "/apply/thanks"
# The audit actor for a deal the borrower typed in themselves (SPEC §11): named by the email
# they gave, so the trail says who, and prefixed so it can never be mistaken for a team member.
WEB_ACTOR_PREFIX = "web:"


def web_actor(deal: Deal) -> str:
    """The borrower as the audit trail names them: ``web:<the email they gave>``."""
    email = deal.borrower.email if deal.borrower is not None and deal.borrower.email else ""
    return f"{WEB_ACTOR_PREFIX}{email or 'borrower'}"


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
    thanks_query = f"{LANGUAGE_FIELD}={language}"

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
        # A box no person can see was filled in. Nothing is stored, and the sender is sent
        # where a real borrower would be - a token nobody was issued opens the plain page -
        # so there is nothing to learn from the reply.
        return redirect(f"{THANKS_PATH}/{secrets.token_urlsafe(32)}?{thanks_query}")

    entry, problems = read_form(posted, t)
    if entry is None:
        return back(problems, status.HTTP_422_UNPROCESSABLE_CONTENT)
    record = intake_record(entry, language=language, intake_source=source)
    actor = f"{WEB_ACTOR_PREFIX}{entry.borrower_email}"
    deal = create_deal(session, record, actor=actor)
    token = issue_thanks_token(session, deal, actor=actor)
    # Screened and priced on arrival, as the system; the thank-you page decides what of it
    # the borrower may see (``services/indicative.py``).
    auto_run(session, deal)
    session.commit()
    return redirect(f"{THANKS_PATH}/{token}?{thanks_query}")


def thanks_page(
    request: Request, *, language: str, terms: IndicativeTerms | None, token: str | None
) -> HTMLResponse:
    """The thank-you page, with the indicative terms table when there is one to show."""
    base = THANKS_PATH if token is None else f"{THANKS_PATH}/{token}"
    return page(
        request,
        "apply_thanks.html",
        {
            "t": strings(language),
            "language": language,
            "terms": terms,
            "toggle_href": f"{base}?{LANGUAGE_FIELD}={other_language(language)}",
            "again_href": f"{APPLY_PATH}?{LANGUAGE_FIELD}={language}",
        },
    )


@router.get(THANKS_PATH, response_class=HTMLResponse, response_model=None)
def apply_thanks(request: Request) -> HTMLResponse:
    """The plain thank-you: the team will be in touch."""
    return thanks_page(request, language=_language(request), terms=None, token=None)


@router.get(f"{THANKS_PATH}/{{token}}", response_class=HTMLResponse, response_model=None)
def apply_thanks_with_terms(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    """The page a submission is sent to: the thank-you, and the indicative terms the token's
    deal qualifies for (SPEC §4.2). An unknown or expired token is the plain thank-you.

    The first time the table is shown it is recorded on the deal and in the audit trail,
    which is the one write a GET here makes.
    """
    language = _language(request)
    deal = deal_by_thanks_token(session, token)
    terms = indicative_terms(session, deal) if deal is not None else None
    if deal is not None and terms is not None:
        if record_shown(session, deal, terms, language=language, actor=web_actor(deal)):
            session.commit()
    return thanks_page(request, language=language, terms=terms, token=token)
