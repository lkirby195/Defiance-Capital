"""Intake endpoints. Phase 1: team entry only.  # SPEC §4.2

One route, two content types. ``POST /intake/team`` takes the JSON body it always took, and
the same body as an HTML form from the New Deal page. It is deliberately not two routes: the
normalizer decides what is missing and what the initial status is (SPEC §4.1), and a second
path into that would be a second place for the rule to drift.

What differs between the two is only what the caller gets back. JSON gets the
``IntakeRecord``, which is what a client wants; the form gets a redirect to the deal it just
created, which is what a person wants. A rejected form is re-rendered with the values still
in it and one line per problem, rather than Pydantic's own error document.

The write takes an actor and records an ``audit_log`` row like every other service write, so
a deal on the Home page can be traced to whoever typed it in. A deal stored complete is then
screened and priced on the way in (``services/autorun.py``, SPEC §4.6), recorded as the
system's own runs, so the page it redirects to opens on a verdict.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from api.forms import fields, is_form_post
from api.intake_form import intake_record, read_form, redisplay_values
from api.problems import FormProblems, at_top
from api.render import redirect
from api.routes.queue import team_entry_page
from api.security import PostedUser, require_csrf
from db.models import User
from db.session import get_session
from intake.parsers.team_form import TeamEntryForm
from schema.models import IntakeRecord
from services import AutoRun, auto_run, create_deal

# The form half of this route is a browser post like any other, so it is guarded like
# one. A JSON caller signs in the same way and carries the same token.
router = APIRouter(prefix="/intake", tags=["intake"], dependencies=[Depends(require_csrf)])

SessionDep = Annotated[Session, Depends(get_session)]


def store(session: Session, form: TeamEntryForm, actor: str) -> tuple[IntakeRecord, AutoRun]:
    """Normalize, persist, audit, run, commit. The same path for both content types.

    The record comes back carrying the status the deal ended in: a complete intake has been
    screened by the time this returns, and a JSON client that read ``NEW`` would be reading
    the status the deal had for a moment nobody saw.
    """
    record = intake_record(form)
    deal = create_deal(session, record, actor=actor)
    run = auto_run(session, deal)
    session.commit()
    return record.model_copy(update={"status": deal.status}), run


@router.post(
    "/team",
    status_code=status.HTTP_201_CREATED,
    response_model=IntakeRecord,
    responses={303: {"description": "Form post: a redirect to the deal that was created"}},
)
async def submit_team_entry(request: Request, session: SessionDep, user: PostedUser) -> Any:
    """Accept a team-entered deal, store it, return the ``IntakeRecord`` or the deal page."""
    if is_form_post(request):
        return _from_form(request, session, user, await request.form())
    try:
        form = TeamEntryForm.model_validate(await request.json())
    except ValidationError as exc:
        # The body is validated here rather than by a declared parameter, because the route
        # also takes a form. Re-raising as FastAPI's own error keeps the 422 document a JSON
        # client already expects, rather than a 500 or a shape of our own invention.
        raise RequestValidationError(exc.errors()) from exc
    record, _ = store(session, form, user.email)
    return JSONResponse(record.model_dump(mode="json"), status_code=status.HTTP_201_CREATED)


def _from_form(
    request: Request, session: Session, user: User, posted: FormData
) -> HTMLResponse | RedirectResponse:
    """The browser half: validate, and on a rejection put the page back with the values on it.

    The form insists on the minimum viable intake and the JSON body above does not, which is
    the one place the two content types deliberately part company. A partial intake is a real
    thing (SPEC §4.1) and arrives from a channel that only has part of one - an SMS, a
    listing link - or from a client posting JSON. A person sitting in front of this page has
    the deal in front of them; the boxes it marks Required are what it cannot be screened
    without, and a browser that skipped them never reaches here anyway.

    The masks come off inside ``read_form`` and go back on for a rejection, so a person who
    typed ``$425,000`` gets ``$425,000`` back rather than the number it parsed to.
    """
    submitted = fields(posted)
    form, complaints = read_form(submitted)

    def back(problems: FormProblems) -> HTMLResponse:
        return team_entry_page(
            request,
            user,
            action="/intake/team",
            heading="New Deal",
            submit_label="Create deal",
            back_url="",
            form=redisplay_values(submitted),
            complaints=problems,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    if form is None:
        return back(complaints)
    try:
        record, run = store(session, form, user.email)
    except ValueError as exc:
        # The form validated and the stored shape did not: the normalizer infers a product
        # and the split is re-checked against that, and the database has rules of its own.
        # Either way it is something on this page, and the page is where it is said.
        session.rollback()
        return back(at_top(str(exc)))
    return redirect(f"/queue/deals/{record.id}", f"Deal created. {run.summary()}".strip())
