"""The review queue: the Home page, the deal page, and every action on it.  # SPEC §9.1, §12

Server-rendered HTML, form posts, POST-redirect-GET. No frontend framework and no client-side
state: the page a person is looking at is a render of the database, and the only script in
the whole queue is the input masks.

The deal page has one button for the engine: **Run Analysis** runs the screen and then the
underwrite (``services/autorun.run_analysis``) as the person who pressed it, and the page
reloads on the verdict and the ledger. The two stages are not named anywhere a person reads;
the JSON routes (``api/routes/deals.py``), the CLI and the services keep them.

Every action calls a service and commits once. The services do the deciding - which statuses
an action runs from, what the audit row says, where a re-opened deal lands - and this module
only turns their refusals into something readable on the page. A successful action redirects,
so a refresh re-reads the deal instead of re-running the action; a failed one re-renders with
the reason, because a failure usually has something to fix on the page.

The three controls on a Home row - Progress, Pause, Kill - post to the same routes the deal
page's buttons do, carrying ``return_to=home`` so the redirect lands back on the list. Kill
goes through a confirmation page first (``kill.html``): a dead deal is not re-opened, and a
button that does that in one click on a list is a button somebody will press by mistake.
Server-rendered, like everything else here - a ``confirm()`` dialog would be script that
decides something, which the queue does not have (CLAUDE.md).

Three edits run the engine again on their way out - the Inputs block, Edit Intake and a
restored intake version (``services/autorun.py``, SPEC §4.6) - so the verdict and the ledger
on the page are never older than the inputs beside them; the redirect's notice says what ran
and what stood down.
Opening a deal populates its SPEC §8.1 defaults if it was stored before they existed
(``services/defaults.py``): the one write a GET makes, idempotent, and recorded.

The team-entry form posts to ``/intake/team``, the route the JSON API already uses. It is one
intake path with two content types, not two paths that can drift. Editing the intake on a deal
that already exists is the one thing that route cannot do - it creates - so that lives here,
rendered from the same template through the same ``team_entry_page``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse

from api.forms import FormDep, fields, rows
from api.intake_form import (
    TEAM_ENTRY_NAMES,
    blank_form_values,
    deal_defaults,
    deal_holding_costs_hint,
    default_marks,
    intake_form_values,
    intake_record,
    read_form,
    redisplay_values,
    submitted_holding_costs_hint,
    text_value,
)
from api.masks import holding_costs_amount, mask_one, masked, unmasked
from api.problems import NO_PROBLEMS, FormProblems, at_top, from_validation_error
from api.render import page, redirect
from api.security import PageUser, require_csrf
from db.models import Deal
from db.session import get_session
from schema.dates import payoff_date_for
from schema.models import (
    SPLIT_PRODUCTS,
    Channel,
    CourtFlag,
    CourtRecordsStatus,
    ExperienceBucket,
    LienKind,
    LoanPurpose,
    Product,
    State,
    Tranche,
)
from services import (
    DECLINE_FROM,
    DEFAULTABLE,
    EDIT_INTAKE_FROM,
    MARK_DEAD_FROM,
    PAUSE_FROM,
    PROGRESS_FROM,
    REOPEN_FROM,
    SYSTEM_ACTOR,
    ActionNotAllowed,
    DealNotFound,
    ReasonRequired,
    TeamOverrides,
    add_note,
    apply_defaults,
    auto_run,
    deal_history,
    deal_trail,
    decline,
    home_view,
    is_defaulted,
    latest_screen,
    latest_submission,
    latest_underwrite,
    load_deal,
    mark_dead,
    pause,
    progress,
    reopen,
    restore_intake,
    run_analysis,
    save_overrides,
    screen_is_stale,
    screen_result,
    underwrite_readiness,
    underwrite_result,
    update_intake,
)
from services.readiness import deal_term

# Every state-changing request through this router carries a CSRF token, by living here
# rather than by each route remembering to ask (``api/security.py``).
router = APIRouter(tags=["queue"], dependencies=[Depends(require_csrf)])

SessionDep = Annotated[Session, Depends(get_session)]

HOME_PATH = "/queue"
# The hidden field a Home row's controls carry, so the action goes back to the list.
RETURN_TO = "return_to"
HOME = "home"

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
        "court_flag": [member.value for member in CourtFlag],
        "court_records_status": [member.value for member in CourtRecordsStatus],
        "experience_bucket": [member.value for member in ExperienceBucket],
        "lien_kind": [member.value for member in LienKind],
        "loan_purpose": [member.value for member in LoanPurpose],
        "product": [member.value for member in Product],
        "state": [member.value for member in State],
        "tranche": [member.value for member in Tranche],
    }


OVERRIDE_NAMES: tuple[str, ...] = (
    "loan_purpose",
    "product",
    "closing_date",
    "term_months",
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
    "loan_purchase_portion",
    "loan_rehab_portion",
    "estimated_sale_price_team",
    "monthly_rent",
    "flip_analysis",
    "rental_analysis",
    "court_records_status",
    "court_records_as_of",
)


# Every box the override block renders, so a complaint about one of them lands under it and
# anything else Pydantic names goes to the top (``api/problems.py``).
OVERRIDE_BOX_NAMES: frozenset[str] = frozenset(OVERRIDE_NAMES)


def override_form(deal: Deal) -> dict[str, str]:
    """The Inputs block as form values, so the page renders what the deal currently says.

    Masked on the way out (``api/masks.py``): a rate shows as ``12%`` and a price as
    ``$425,000``. The §8.1 inputs with a default - the rate, the four fees, the closing date,
    the loan split on a split product - are pre-filled with it where the deal carries none,
    and the page tags them: a box holding the default and a box holding a number somebody
    chose look different on purpose.
    """
    values: dict[str, object | None] = {name: getattr(deal, name) for name in OVERRIDE_NAMES}
    for name, default in deal_defaults(deal).items():
        if values.get(name) is None:
            values[name] = default
    return {name: mask_one(name, value) for name, value in values.items()}


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
        "progress": deal.status in PROGRESS_FROM,
        "pause": deal.status in PAUSE_FROM,
        "decline": deal.status in DECLINE_FROM,
        "kill": deal.status in MARK_DEAD_FROM,
        "reopen": deal.status in REOPEN_FROM,
        "edit_intake": deal.status in EDIT_INTAKE_FROM,
    }


def deal_path(deal_id: UUID) -> str:
    return f"/queue/deals/{deal_id}"


def return_path(deal_id: UUID, return_to: str | None) -> str:
    """Where an action sends the browser afterwards: the list it came from, else the deal."""
    return HOME_PATH if return_to == HOME else deal_path(deal_id)


def _submitted_language(session: Session, deal: Deal) -> str | None:
    """The language the borrower filled the public form in, off its submission row.

    None on every other channel: the team form has no language, and a web deal whose intake
    the team has since re-applied keeps the borrower's choice only as long as the latest
    submission is still theirs - which is the honest answer.
    """
    if deal.channel is not Channel.WEB:
        return None
    submission = latest_submission(session, deal.id)
    if submission is None or not isinstance(submission.raw_payload, dict):
        return None
    language = submission.raw_payload.get("language")
    return language if isinstance(language, str) else None


def deal_payoff_date(deal: Deal) -> date | None:
    """The last row of the ledger: the month end the term lands on, stub included.  # SPEC §8.1"""
    term = deal_term(deal)
    if deal.closing_date is None or term is None or not term.is_positive:
        return None
    return payoff_date_for(deal.closing_date, term.full_months, term.stub_days)


def economics_in_force(deal: Deal) -> dict[str, Any]:
    """Each defaultable §8.1 economic as the deal page shows it, and which are defaults.

    The value on the deal, else the stand-in the engine would read in its place
    (``services/defaults.py``) - so a deal stored before the defaults existed and not yet
    opened still shows the numbers it would be priced on. The loan split on a single-note
    product is the whole loan at closing and nothing held back (SPEC §8.2), shown as the
    default it is rather than stored.
    """
    defaults = deal_defaults(deal)
    shown: dict[str, Any] = {}
    for name in DEFAULTABLE:
        value = getattr(deal, name)
        shown[name] = value if value is not None else defaults.get(name)
    defaulted = {name for name in DEFAULTABLE if is_defaulted(deal, name)}
    split_product = deal.product in SPLIT_PRODUCTS
    if not split_product and deal.loan_requested is not None:
        shown["loan_purchase_portion"] = deal.loan_requested
        shown["loan_rehab_portion"] = Decimal(0)
    # ``shown`` rather than ``values``: a template reading ``economics.values`` would get the
    # dict's own method, not the key.
    return {"shown": shown, "defaulted": defaulted, "split_product": split_product}


def render_deal(
    request: Request,
    session: Session,
    deal_id: UUID,
    user: PageUser,
    *,
    complaints: FormProblems | None = None,
    form: dict[str, str] | None = None,
    matters: list[dict[str, str]] | None = None,
    holding_costs_hint: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The deal page, rebuilt from the database plus whatever the last post left behind.

    ``form`` and ``matters`` are the values a rejected submission was carrying: re-rendering
    with them means a person fixes one box rather than retyping the block. Left out, the page
    shows what the deal currently holds. The Inputs section opens on a page that carries a
    complaint - the box the complaint is under has to be visible - and stays collapsed
    otherwise.
    """
    deal = load_deal(session, deal_id)
    screen_row = latest_screen(session, deal.id)
    underwrite_row = latest_underwrite(session, deal.id)
    return page(
        request,
        "deal.html",
        {
            "deal": deal,
            # Where the deal came in and in which language (SPEC §4.2), for a web deal.
            "submitted_language": _submitted_language(session, deal),
            # Derived, not stored (SPEC §8.1): the last day of the month the term lands on,
            # shown read-only beside the term it comes from.
            "payoff_date": deal_payoff_date(deal),
            # The §8.1 economics as the run reads them, and which of them are stand-ins
            # (SPEC §8.1, §8.2), for the "default" tag beside each.
            "economics": economics_in_force(deal),
            "allowed": _allowed(deal),
            "enums": enum_values(),
            # An edit re-runs the screen (``services/autorun.py``); this is only ever true
            # when that run could not go ahead, and the page says so.
            "screen_is_stale": screen_is_stale(session, deal.id),
            # What the ledger would run on, and why it is off when it is off (SPEC §8.1).
            # The refusal behind the button reads the same rules, so the page cannot
            # promise a run the assembly then declines.
            "readiness": underwrite_readiness(deal),
            "defaults": default_marks(deal),
            "form": form if form is not None else override_form(deal),
            "matters": matters if matters is not None else matter_rows(deal.court_records_team),
            # What the holding-cost percentage in force comes to in dollars (SPEC §8.1),
            # printed under the box that holds the percentage.
            "holding_costs_hint": (
                holding_costs_hint
                if holding_costs_hint is not None
                else deal_holding_costs_hint(deal)
            ),
            # The same figure for the Deal Economics facts, on the percentage in force.
            "holding_costs_entered": holding_costs_amount(
                economics_in_force(deal)["shown"]["holding_costs_pct_of_cost"],
                deal.purchase_price,
                deal.rehab_costs,
            ),
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
            # Every intake version, screen and underwrite, newest first, Restore on each
            # version (``services/history.py``); then the audit trail under it.
            "history": deal_history(session, deal.id),
            "trail": deal_trail(session, deal.id),
            "inputs_open": complaints is not None and bool(complaints),
            "problems": complaints if complaints is not None else NO_PROBLEMS,
        },
        user=user,
        status_code=status_code,
    )


# --- pages ---------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False, response_model=None)
def home() -> RedirectResponse:
    return redirect(HOME_PATH)


@router.get("/queue", response_class=HTMLResponse, response_model=None)
def home_page(request: Request, session: SessionDep, user: PageUser) -> HTMLResponse:
    """Home: every deal in one of four sections, most recently touched first.  # SPEC §9.1"""
    return page(request, "home.html", {"view": home_view(session)}, user=user)


def team_entry_page(
    request: Request,
    user: PageUser,
    *,
    action: str,
    heading: str,
    submit_label: str,
    back_url: str,
    form: dict[str, str],
    complaints: FormProblems,
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
            "submit_label": submit_label,
            "back_url": back_url,
            # Every name the template renders, so a dropped blank is still a blank box.
            "form": {**blank_form_values(), **form},
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
        heading="New Deal",
        submit_label="Create deal",
        back_url="",
        form={},
        complaints=NO_PROBLEMS,
    )


@router.get("/queue/deals/{deal_id}", response_class=HTMLResponse, response_model=None)
def deal_page(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """One deal: the verdict and the ledger, the §9 sections, the inputs, the history.

    A deal stored before the SPEC §8.1 defaults existed is populated with them here, on its
    next open, and the write committed before the page renders what it wrote. Idempotent:
    every later open writes nothing (``services/defaults.py``).
    """
    try:
        if apply_defaults(session, deal_id, actor=SYSTEM_ACTOR):
            session.commit()
        return render_deal(request, session, deal_id, user)
    except DealNotFound:
        return redirect(HOME_PATH, "That deal does not exist.")


# --- the analysis (SPEC §4.6, §7, §8) -------------------------------------------------------------


@router.post("/queue/deals/{deal_id}/run", response_model=None)
def run_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Run Analysis: the screen, then the underwrite, recorded against the person who asked.

    No form: every SPEC §8.1 input lives on the deal by now (the Inputs block), so the button
    runs the deal as it stands, a paused deal included. What ran is kept whatever happened
    next; what stood down is said on the page - as a complaint when the deal is short of
    something or the engine will not price it, as a conflict when its status refused the
    ledger - rather than in a notice that reads as success.
    """
    try:
        deal = load_deal(session, deal_id)
    except DealNotFound:
        return redirect(HOME_PATH, "That deal does not exist.")
    run = run_analysis(session, deal, actor=user.email)
    if not run.ran:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=at_top(*run.skipped),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    session.commit()
    if run.skipped:
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=at_top(*run.skipped),
            status_code=(
                status.HTTP_409_CONFLICT if run.refused else status.HTTP_422_UNPROCESSABLE_CONTENT
            ),
        )
    return redirect(deal_path(deal_id), run.summary())


# --- the intake, edited (SPEC §4.1) ---------------------------------------------------------------


def _edit_page(
    request: Request,
    user: PageUser,
    deal_id: UUID,
    *,
    form: dict[str, str],
    complaints: FormProblems,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return team_entry_page(
        request,
        user,
        action=f"{deal_path(deal_id)}/intake",
        heading="Edit Intake",
        submit_label="Save intake",
        back_url=deal_path(deal_id),
        form=form,
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
        return redirect(HOME_PATH, "That deal does not exist.")
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
        complaints=NO_PROBLEMS,
    )


@router.post("/queue/deals/{deal_id}/intake", response_model=None)
def edit_intake_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Re-apply an edited intake: a new submission row, fresh ``missing_fields``, an audit row.

    The status may move (``NEEDS_INFO`` to ``NEW`` once the intake is complete), and the
    screen and the underwrite run again on what was saved (``services/autorun.py``); the
    notice says what ran.
    """
    submitted = fields(form)
    entry, complaints = read_form(submitted)

    def back(problems: FormProblems, code: int) -> HTMLResponse:
        return _edit_page(
            request,
            user,
            deal_id,
            form=redisplay_values(submitted),
            complaints=problems,
            status_code=code,
        )

    if entry is None:
        return back(complaints, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        deal = update_intake(session, deal_id, intake_record(entry), actor=user.email)
    except DealNotFound:
        session.rollback()
        return redirect(HOME_PATH, "That deal does not exist.")
    except ActionNotAllowed as exc:
        session.rollback()
        return back(at_top(str(exc)), status.HTTP_409_CONFLICT)
    except ValidationError as exc:
        # Everything the intake models refuse that the form did not: the deal is rebuilt
        # from the record on the way in, and a rule only the stored shape can test - the
        # split against a product the normalizer inferred - is caught there rather than here.
        session.rollback()
        problems = from_validation_error(exc, TEAM_ENTRY_NAMES)
        return back(problems, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except ValueError as exc:
        session.rollback()
        return back(at_top(str(exc)), status.HTTP_422_UNPROCESSABLE_CONTENT)
    run = auto_run(session, deal)
    session.commit()
    return redirect(deal_path(deal_id), f"Intake updated. {run.summary()}".strip())


@router.post("/queue/deals/{deal_id}/intake/{submission_id}/restore", response_model=None)
def restore_intake_action(
    request: Request, deal_id: UUID, submission_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Restore an earlier intake version: a new submission row, an audit row, a re-run.

    Append-only (SPEC §5): the version restored is still in the History afterwards, and so
    is the one it replaced. The screen and the underwrite run again on what was restored
    (``services/autorun.py``), and the notice says what ran.
    """
    try:
        deal = restore_intake(session, deal_id, submission_id, actor=user.email)
    except DealNotFound:
        session.rollback()
        return redirect(HOME_PATH, "That deal does not exist.")
    except ActionNotAllowed as exc:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=at_top(str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        # A version that is not this deal's, or whose payload no parser reads back; the
        # pydantic refusal of a stale payload is a ValueError too.
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=at_top(str(exc)),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    run = auto_run(session, deal)
    session.commit()
    return redirect(deal_path(deal_id), f"Intake restored. {run.summary()}".strip())


# --- team entry ----------------------------------------------------------------------------------


@router.post("/queue/deals/{deal_id}/overrides", response_model=None)
def overrides_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Save the Inputs block: the economics, the valuation, the rent, the court search.  # SPEC §6.1

    A box left holding its default is stored and tagged as the default; a box left blank on
    a defaulted input gets the default back (``services/defaults.py``). The analysis then
    runs again on the deal as saved (``services/autorun.py``).
    """
    submitted = fields(form, skip=("matter_",))
    matters = submitted_matters(form)
    try:
        deal = load_deal(session, deal_id)
    except DealNotFound:
        return redirect(HOME_PATH, "That deal does not exist.")
    values = unmasked(submitted)

    def back(problems: FormProblems, code: int) -> HTMLResponse:
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=problems,
            form={**dict.fromkeys(override_form(deal), ""), **masked(values)},
            matters=redisplay(matters),
            holding_costs_hint=submitted_holding_costs_hint(submitted),
            status_code=code,
        )

    try:
        overrides = TeamOverrides.model_validate({**values, "court_records_team": matters})
    except ValidationError as exc:
        session.rollback()
        problems = from_validation_error(exc, OVERRIDE_BOX_NAMES)
        return back(problems, status.HTTP_422_UNPROCESSABLE_CONTENT)
    try:
        saved = save_overrides(session, deal_id, overrides, actor=user.email)
    except ValueError as exc:
        # The split against the loan amount (SPEC §8.2), and the database's own rules (a
        # term of no time at all), reach here as a ValueError or an integrity error on the
        # flush - still a complaint about what was typed rather than a fault of the server's.
        session.rollback()
        return back(at_top(str(exc)), status.HTTP_422_UNPROCESSABLE_CONTENT)
    run = auto_run(session, saved)
    session.commit()
    return redirect(deal_path(deal_id), f"Inputs saved. {run.summary()}".strip())


# --- lifecycle actions (SPEC §4.6) ---------------------------------------------------------------


def _act(
    request: Request,
    session: Session,
    deal_id: UUID,
    user: PageUser,
    run: Any,
    done: str,
    return_to: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Apply one action, commit it, and say so; or re-render with why it did not apply.

    ``return_to`` is where the browser goes afterwards: Home when the action came off a Home
    row, the deal page otherwise. A refusal is always the deal page, because that is where
    the reason reads beside the deal it is about.
    """
    try:
        run()
    except DealNotFound:
        session.rollback()
        return redirect(HOME_PATH, "That deal does not exist.")
    except (ActionNotAllowed, ReasonRequired) as exc:
        session.rollback()
        return render_deal(
            request,
            session,
            deal_id,
            user,
            complaints=at_top(str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    session.commit()
    return redirect(return_path(deal_id, return_to), done)


@router.post("/queue/deals/{deal_id}/advance", response_model=None)
def advance_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Progress: the next status, or the prior one off a pause (``services.actions.progress``)."""
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: progress(session, deal_id, actor=user.email),
        "Progressed.",
        fields(form).get(RETURN_TO),
    )


@router.post("/queue/deals/{deal_id}/pause", response_model=None)
def pause_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: pause(session, deal_id, actor=user.email),
        "Paused.",
        fields(form).get(RETURN_TO),
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


def kill_page(
    request: Request,
    session: Session,
    deal_id: UUID,
    user: PageUser,
    *,
    return_to: str | None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse | RedirectResponse:
    """The confirmation in front of Kill: the deal by name, and the two ways out."""
    try:
        deal = load_deal(session, deal_id)
    except DealNotFound:
        return redirect(HOME_PATH, "That deal does not exist.")
    return page(
        request,
        "kill.html",
        {
            "deal": deal,
            "allowed": _allowed(deal),
            "return_to": return_to or "",
            "back_url": return_path(deal_id, return_to),
        },
        user=user,
        status_code=status_code,
    )


@router.get("/queue/deals/{deal_id}/kill", response_class=HTMLResponse, response_model=None)
def kill_confirm_page(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser
) -> HTMLResponse | RedirectResponse:
    """Ask before killing: a dead deal is not re-opened (SPEC §4.6)."""
    return kill_page(request, session, deal_id, user, return_to=request.query_params.get(RETURN_TO))


@router.post("/queue/deals/{deal_id}/kill", response_model=None)
def kill_action(
    request: Request, deal_id: UUID, session: SessionDep, user: PageUser, form: FormDep
) -> HTMLResponse | RedirectResponse:
    """Kill the deal, once the confirmation page's own form has said so.

    A post that does not carry the confirmation - a stale link, a form from somewhere else -
    is answered with the confirmation page rather than a dead deal.
    """
    posted = fields(form)
    return_to = posted.get(RETURN_TO)
    if posted.get("confirm") != "yes":
        return kill_page(
            request,
            session,
            deal_id,
            user,
            return_to=return_to,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    reason = posted.get("reason")
    return _act(
        request,
        session,
        deal_id,
        user,
        lambda: mark_dead(session, deal_id, actor=user.email, reason=reason),
        "Killed.",
        return_to,
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
