"""The review queue: the list, the deal page, and every action on it.  # SPEC §9.1, §12

Server-rendered HTML, form posts, POST-redirect-GET. No frontend framework and no client-side
state: the page a person is looking at is a render of the database, and the only script in
the whole queue is the copy button on the suggested reply.

Every action calls a service and commits once. The services do the deciding - which statuses
an action runs from, what the audit row says, where a re-opened deal lands - and this module
only turns their refusals into something readable on the page. A successful action redirects,
so a refresh re-reads the deal instead of re-running the action; a failed one re-renders with
the reason, because a failure usually has something to fix on the page.

The team-entry form posts to ``/intake/team``, the route the JSON API already uses. It is one
intake path with two content types, not two paths that can drift. Editing the intake on a deal
that already exists is the one thing that route cannot do - it creates - so that lives here,
rendered from the same template through the same ``team_entry_page``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from api.forms import FormDep, fields, problems, rows
from api.intake_form import (
    TEAM_ENTRY_FIELDS,
    intake_form_values,
    intake_record,
    read_form,
    text_value,
)
from api.render import page, redirect
from api.security import PageUser, require_csrf
from db.models import Deal
from db.session import get_session
from schema.models import (
    AssetType,
    CourtFlag,
    CourtRecordsStatus,
    ExperienceBucket,
    LienKind,
    Product,
    State,
    StatedExit,
    TermBucket,
    Tranche,
)
from services import (
    ADVANCE_TO_REVIEW_FROM,
    DECLINE_FROM,
    EDIT_INTAKE_FROM,
    MARK_DEAD_FROM,
    REOPEN_FROM,
    ActionNotAllowed,
    DealNotFound,
    DealNotReady,
    DealNotUnderwritable,
    ReasonRequired,
    TeamOverrides,
    UnderwriteRequest,
    add_note,
    advance_to_review,
    deal_trail,
    decline,
    latest_screen,
    latest_underwrite,
    load_deal,
    mark_dead,
    queue_view,
    reopen,
    run_screen,
    run_underwrite,
    save_overrides,
    screen_is_stale,
    screen_result,
    underwrite_readiness,
    underwrite_result,
    update_intake,
)

# Every state-changing request through this router carries a CSRF token, by living here
# rather than by each route remembering to ask (``api/security.py``).
router = APIRouter(tags=["queue"], dependencies=[Depends(require_csrf)])

SessionDep = Annotated[Session, Depends(get_session)]

# Spare rows on the court-matter table. Plain HTML cannot add a row without script, so the
# form ships with room to type into; empty rows are dropped on the way in.
SPARE_MATTER_ROWS = 3
MATTER_FIELDS: tuple[str, ...] = (
    "code",
    "occurred_on",
    "amount_usd",
    "lien_kind",
    "senior",
    "resolved_at_close",
    "description",
)


def enum_values() -> dict[str, list[str]]:
    """The choices every select on the queue offers, by their stored values."""
    return {
        "asset_type": [member.value for member in AssetType],
        "court_flag": [member.value for member in CourtFlag],
        "court_records_status": [member.value for member in CourtRecordsStatus],
        "experience_bucket": [member.value for member in ExperienceBucket],
        "lien_kind": [member.value for member in LienKind],
        "product": [member.value for member in Product],
        "state": [member.value for member in State],
        "stated_exit": [member.value for member in StatedExit],
        "term_bucket": [member.value for member in TermBucket],
        "tranche": [member.value for member in Tranche],
    }


def override_form(deal: Deal) -> dict[str, str]:
    """The override block as form values, so the page renders what the deal currently says."""
    names = (
        "as_is_value_team",
        "arv_team",
        "actual_annual_taxes_usd",
        "actual_annual_insurance_usd",
        "actual_annual_utilities_usd",
        "market_rent_monthly",
        "asset_type",
        "product",
        "stated_exit",
        "court_records_status",
        "court_records_as_of",
    )
    return {name: text_value(getattr(deal, name)) for name in names}


def blank_matter() -> dict[str, str]:
    return dict.fromkeys(MATTER_FIELDS, "")


def matter_rows(stored: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Stored matters as form rows, plus spare blanks to type into."""
    out = [
        {**blank_matter(), **{key: text_value(value) for key, value in matter.items()}}
        for matter in stored
    ]
    out.extend(blank_matter() for _ in range(SPARE_MATTER_ROWS))
    return out


def submitted_matters(form: FormDep) -> list[dict[str, str]]:
    """The court matters as typed, blank rows dropped."""
    return rows(form, "matter", MATTER_FIELDS)


def redisplay(matters: list[dict[str, str]]) -> list[dict[str, str]]:
    """Submitted matters padded back out for the form: every column present, spares below.

    ``rows()`` drops the blank cells, which is what a model wants and not what a template
    does - a row missing a column would render as a missing box rather than an empty one.
    """
    filled = [{**blank_matter(), **matter} for matter in matters]
    filled.extend(blank_matter() for _ in range(SPARE_MATTER_ROWS))
    return filled


def _allowed(deal: Deal) -> dict[str, bool]:
    """Which action buttons this deal's status leaves live.  # SPEC §4.6"""
    return {
        "advance": deal.status in ADVANCE_TO_REVIEW_FROM,
        "decline": deal.status in DECLINE_FROM,
        "dead": deal.status in MARK_DEAD_FROM,
        "reopen": deal.status in REOPEN_FROM,
        "edit_intake": deal.status in EDIT_INTAKE_FROM,
    }


def deal_path(deal_id: UUID) -> str:
    return f"/queue/deals/{deal_id}"


def render_deal(
    request: Request,
    session: Session,
    deal_id: UUID,
    user: PageUser,
    *,
    complaints: list[str] | None = None,
    form: dict[str, str] | None = None,
    matters: list[dict[str, str]] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The deal page, rebuilt from the database plus whatever the last post left behind.

    ``form`` and ``matters`` are the values a rejected submission was carrying: re-rendering
    with them means a person fixes one box rather than retyping the block. Left out, the page
    shows what the deal currently holds.
    """
    deal = load_deal(session, deal_id)
    screen_row = latest_screen(session, deal.id)
    underwrite_row = latest_underwrite(session, deal.id)
    return page(
        request,
        "deal.html",
        {
            "deal": deal,
            "allowed": _allowed(deal),
            "enums": enum_values(),
            # Not "re-screen it": whether an edited intake is worth another Stage 1 run is a
            # person's call (SPEC §7), and the page's job is to make sure they know there is
            # one to make.
            "screen_is_stale": screen_is_stale(session, deal.id),
            # What Run underwrite would run on, and why it is off when it is off
            # (SPEC §8.1). The refusal behind the button reads the same rules, so the page
            # cannot promise a run the assembly then declines.
            "readiness": underwrite_readiness(deal),
            "form": form if form is not None else override_form(deal),
            "matters": matters if matters is not None else matter_rows(deal.court_records_team),
            "screen": (
                None
                if screen_row is None
                else {
                    "id": screen_row.id,
                    "created_at": screen_row.created_at,
                    "result": screen_result(screen_row),
                }
            ),
            "underwrite": (
                None
                if underwrite_row is None
                else {
                    "id": underwrite_row.id,
                    "created_at": underwrite_row.created_at,
                    "result": underwrite_result(underwrite_row),
                }
            ),
            "trail": deal_trail(session, deal.id),
            "problems": complaints or [],
        },
        user=user,
        status_code=status_code,
    )


# --- pages ---------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False, response_model=None)
def home() -> RedirectResponse:
    return redirect("/queue")


@router.get("/queue", response_class=HTMLResponse, response_model=None)
def queue_page(request: Request, session: SessionDep, user: PageUser) -> HTMLResponse:
    """Deals grouped by status, newest first, Hard flags after LOI pinned on top.  # SPEC §12"""
    return page(request, "queue.html", {"view": queue_view(session)}, user=user)


NEW_DEAL_INTRO = (
    "The team-entry channel (SPEC §4.2). The Required boxes are the minimum viable "
    "intake (SPEC §4.1) — a deal cannot be screened without them, so this form "
    "asks for all ten. Everything marked Optional can follow later. It posts to the same "
    "route the API takes."
)
EDIT_INTAKE_INTRO = (
    "The same form the deal was entered on, filled in with what it currently says. Saving "
    "stores a new submission and leaves the old one on the record, immutable; the deal keeps "
    "its id, its screens and its underwrites. A re-screen is not automatic."
)


def team_entry_page(
    request: Request,
    user: PageUser,
    *,
    action: str,
    heading: str,
    intro: str,
    submit_label: str,
    back_url: str,
    form: dict[str, str],
    matters: list[dict[str, str]],
    complaints: list[str],
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The team-entry form, blank or filled in, for whichever route is showing it.

    One renderer for four cases - the new deal, the edit, and each of them re-rendered with
    what a rejected submission was carrying - so the two forms cannot drift into asking for
    different things.
    """
    return page(
        request,
        "team_entry.html",
        {
            "enums": enum_values(),
            "action": action,
            "heading": heading,
            "intro": intro,
            "submit_label": submit_label,
            "back_url": back_url,
            # Every name the template renders, so a dropped blank is still a blank box.
            "form": {**dict.fromkeys(TEAM_ENTRY_FIELDS, ""), **form},
            "matters": matters,
            "problems": complaints,
        },
        user=user,
        status_code=status_code,
    )


@router.get("/queue/new", response_class=HTMLResponse, response_model=None)
def new_deal_page(request: Request, user: PageUser) -> HTMLResponse:
    """The team-entry form (SPEC §4.2); it posts to ``/intake/team``."""
    return team_entry_page(
        request,
        user,
        action="/intake/team",
        heading="New deal",
        intro=NEW_DEAL_INTRO,
        submit_label="Create deal",
        back_url="",
        form={},
        matters=[blank_matter() for _ in range(SPARE_MATTER_ROWS)],
        complaints=[],
    )


@router.get("/queue/deals/{deal_id}", response_class=HTMLResponse, response_model=None)
def deal_page(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """One deal: intake, overrides, the runs, the flags, and what was done to it."""
    try:
        return render_deal(request, session, deal_id, user)
    except DealNotFound:
        return redirect("/queue", "That deal does not exist.")


# --- runs ----------------------------------------------------------------------------------------


@router.post("/queue/deals/{deal_id}/screen", response_model=None)
def screen_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Run the screen (SPEC §7) and record it against the person who asked for it."""
    try:
        result = run_screen(session, deal_id, actor=user.email)
    except DealNotReady as exc:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=[str(exc)],
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    session.commit()
    return redirect(deal_path(deal_id), f"Screen recorded: {result.verdict.value}.")


@router.post("/queue/deals/{deal_id}/underwrite", response_model=None)
def underwrite_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Run the underwrite (SPEC §8) on what the deal already carries.

    No form: every SPEC §8.1 input the team supplies lives on the deal by now (the override
    block above), so the button runs the deal as it stands. A deal missing one is named
    rather than priced on a guess.
    """
    try:
        result = run_underwrite(session, deal_id, UnderwriteRequest(), actor=user.email)
    except DealNotUnderwritable as exc:
        # A refusal can arrive with a real screen behind it: an unscreened deal is screened
        # first (SPEC §8), and that screen ran. Keep it, exactly as the JSON route does.
        session.commit()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=[str(exc)],
            status_code=status.HTTP_409_CONFLICT,
        )
    except DealNotReady as exc:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=[str(exc)],
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    session.commit()
    return redirect(deal_path(deal_id), f"Underwrite recorded at r* {result.solved_rate:.4%}.")


# --- the intake, edited (SPEC §4.1) ---------------------------------------------------------------


def _edit_page(
    request: Request,
    user: PageUser,
    deal_id: UUID,
    *,
    form: dict[str, str],
    matters: list[dict[str, str]],
    complaints: list[str],
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return team_entry_page(
        request,
        user,
        action=f"{deal_path(deal_id)}/intake",
        heading="Edit intake",
        intro=EDIT_INTAKE_INTRO,
        submit_label="Save intake",
        back_url=deal_path(deal_id),
        form=form,
        matters=matters,
        complaints=complaints,
        status_code=status_code,
    )


@router.get("/queue/deals/{deal_id}/intake", response_class=HTMLResponse, response_model=None)
def edit_intake_page(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """The team-entry form, filled in from the deal.  # SPEC §4.1, §4.2"""
    try:
        deal = load_deal(session, deal_id)
    except DealNotFound:
        return redirect("/queue", "That deal does not exist.")
    if deal.status not in EDIT_INTAKE_FROM:
        return redirect(
            deal_path(deal_id),
            f"The intake cannot be edited on a {deal.status.value} deal.",
        )
    return _edit_page(
        request,
        user,
        deal_id,
        form=intake_form_values(deal),
        matters=matter_rows(deal.court_records_team),
        complaints=[],
    )


@router.post("/queue/deals/{deal_id}/intake", response_model=None)
def edit_intake_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Re-apply an edited intake: a new submission row, fresh ``missing_fields``, an audit row.

    The status may move (``NEEDS_INFO`` to ``NEW`` once the intake is complete) but nothing is
    re-run: the deal page says the screen is stale and a person decides.
    """
    submitted = fields(form, skip=("matter_",))
    matters = submitted_matters(form)
    entry, complaints = read_form(submitted, matters)
    if entry is None:
        return _edit_page(
            request,
            user,
            deal_id,
            form=submitted,
            matters=redisplay(matters),
            complaints=complaints,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        update_intake(session, deal_id, intake_record(entry), actor=user.email)
    except DealNotFound:
        session.rollback()
        return redirect("/queue", "That deal does not exist.")
    except ActionNotAllowed as exc:
        session.rollback()
        return _edit_page(
            request,
            user,
            deal_id,
            form=submitted,
            matters=redisplay(matters),
            complaints=[str(exc)],
            status_code=status.HTTP_409_CONFLICT,
        )
    session.commit()
    return redirect(deal_path(deal_id), "Intake updated.")


# --- team entry ----------------------------------------------------------------------------------


@router.post("/queue/deals/{deal_id}/overrides", response_model=None)
def overrides_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Save the team's own valuation, opex, structure and court search.  # SPEC §6.1"""
    submitted = fields(form, skip=("matter_",))
    matters = submitted_matters(form)
    try:
        overrides = TeamOverrides.model_validate({**submitted, "court_records_team": matters})
    except ValidationError as exc:
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=problems(exc),
            form={**dict.fromkeys(override_form(load_deal(session, deal_id)), ""), **submitted},
            matters=redisplay(matters),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    save_overrides(session, deal_id, overrides, actor=user.email)
    session.commit()
    return redirect(deal_path(deal_id), "Team entry saved.")


# --- lifecycle actions (SPEC §4.6) ---------------------------------------------------------------


def _act(
    request: Request,
    session: Session,
    deal_id: UUID,
    user: PageUser,
    run: Any,
    done: str,
) -> HTMLResponse | RedirectResponse:
    """Apply one action, commit it, and say so; or re-render with why it did not apply."""
    try:
        run()
    except (ActionNotAllowed, ReasonRequired) as exc:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=[str(exc)],
            status_code=status.HTTP_409_CONFLICT,
        )
    session.commit()
    return redirect(deal_path(deal_id), done)


@router.post("/queue/deals/{deal_id}/advance", response_model=None)
def advance_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: advance_to_review(session, deal_id, actor=user.email),
        "Advanced to review.",
    )


@router.post("/queue/deals/{deal_id}/decline", response_model=None)
def decline_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    reason = fields(form).get("reason", "")
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: decline(session, deal_id, actor=user.email, reason=reason),
        "Declined.",
    )


@router.post("/queue/deals/{deal_id}/dead", response_model=None)
def dead_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    reason = fields(form).get("reason")
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: mark_dead(session, deal_id, actor=user.email, reason=reason),
        "Marked dead.",
    )


@router.post("/queue/deals/{deal_id}/reopen", response_model=None)
def reopen_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    reason = fields(form).get("reason", "")
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: reopen(session, deal_id, actor=user.email, reason=reason),
        "Re-opened.",
    )


@router.post("/queue/deals/{deal_id}/note", response_model=None)
def note_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    note = fields(form).get("note", "")
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: add_note(session, deal_id, actor=user.email, note=note),
        "Note added.",
    )
