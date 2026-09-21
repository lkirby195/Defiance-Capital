"""The Jinja environment the review queue renders through.  # SPEC §9.1, §12

Templates live under ``api/templates`` rather than the repository's ``templates/``, which is
gitignored and reserved for the client-supplied docx files (CLAUDE.md).

The filters here are the whole of the presentation logic. Money, rates and ratios are
``Decimal`` everywhere inside the engine and are rounded only at an output boundary
(CLAUDE.md, "Money and rates") - this is that boundary for the queue, exactly as
``outputs/console.py`` is for the CLI. A template that needs a number formatted a new way
gets a filter here; it never does arithmetic of its own.

``page()`` is the one way a response is built, so every page carries the signed-in user, the
notice a redirect brought with it, the problems a rejected form produced, and the CSRF token
its forms post back - without each route remembering to pass them. A page rendered for nobody
gets an empty token, because a page with no session has no form worth posting.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jinja2
from fastapi import Request, status
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, RedirectResponse

from api.security import issue_csrf
from db.models import User

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def money(value: Decimal | None) -> str:
    """``$1,234.56``; an em dash when there is no number."""
    return "—" if value is None else f"${value:,.2f}"


def pct(value: Decimal | None) -> str:
    """``12.5%`` - one decimal, which is how a cap or a haircut is spoken."""
    return "—" if value is None else f"{value:.1%}"


def rate(value: Decimal | None) -> str:
    """``14.8333%`` - four decimals, for a solved rate that is a division."""
    return "—" if value is None else f"{value * 100:.4f}%"


def points(value: Decimal | None) -> str:
    """A tolerance band in percentage points: ``5.0 pts``."""
    return "—" if value is None else f"{value * 100:.1f} pts"


def ratio(value: Decimal | None) -> str:
    """A cover or a DSCR: ``1.09x``."""
    return "—" if value is None else f"{value:.2f}x"


def whole(value: Decimal | int | None) -> str:
    return "—" if value is None else f"{value:,}"


def yes_no(value: bool | None) -> str:
    """``Yes`` / ``No`` / an em dash, because None means nobody said."""
    if value is None:
        return "—"
    return "Yes" if value else "No"


def when(value: datetime | date | None) -> str:
    """A timestamp a person reads, in whatever zone it was stored in."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def plain(value: Any) -> str:
    """Anything with no filter of its own: enums by value, None as an em dash."""
    if value is None:
        return "—"
    return str(getattr(value, "value", value))


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(
    money=money,
    pct=pct,
    rate=rate,
    points=points,
    ratio=ratio,
    whole=whole,
    yes_no=yes_no,
    when=when,
    plain=plain,
)
# A missing name in a template is a bug in the template, not an empty string on the page.
templates.env.undefined = jinja2.StrictUndefined


def page(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    user: User | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render a template with the shared context every page has."""
    shared: dict[str, Any] = {
        "user": user,
        "notice": request.query_params.get("notice"),
        "problems": context.pop("problems", []),
        "csrf_token": issue_csrf(user) if user is not None else "",
    }
    return templates.TemplateResponse(request, name, {**shared, **context}, status_code=status_code)


def redirect(path: str, notice: str | None = None) -> RedirectResponse:
    """POST-redirect-GET, so a refresh re-reads the page instead of re-running the action.

    303 rather than 302: it is the status that says "GET the other thing", which is what a
    browser must do after a form post whatever it sent.
    """
    if notice:
        path = f"{path}{'&' if '?' in path else '?'}notice={quote(notice, safe='')}"
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)
