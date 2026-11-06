"""FastAPI application: review queue, team entry, and the JSON deal API.  # SPEC §2, §12

Four routers and two exception handlers.

``NotSignedIn`` is the whole of the "you are not signed in" policy for pages: a dependency
raises it carrying where the caller was going, and this turns it into a 303 to
``/login?next=...`` so a browser lands back on the page it wanted once it has a session. The
JSON routes raise a 401 instead and never see it - a redirect is not something an API client
can act on.

``CsrfRejected`` is a 403 with a page saying what happened and that nothing was changed. It
is not a redirect: a browser that followed one would lose the post and the person would be
left guessing why their form vanished.

Nothing the team reads is public. Every route in every router but one is behind a session
cookie, because there is nothing here that is not a deal: credit ranges, court findings, and
what GLENWOOD decided about a borrower (SPEC §11). The one is ``/apply`` (``api/routes/apply``),
the borrower's own form: it writes exactly one kind of row and reads nothing back but a
thank-you.

The framework's own API docs - ``/docs``, ``/redoc`` and the ``/openapi.json`` they read - are
not served (SPEC §11, v0.26): they are a map of every route for whoever asks, and nobody
outside a developer's machine needs one. ``GLENWOOD_API_DOCS=1`` in the environment when
this module is imported turns them on (README).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER, HTTP_403_FORBIDDEN

from api.render import templates
from api.routes.apply import APPLY_PATH
from api.routes.apply import router as apply_router
from api.routes.auth import router as auth_router
from api.routes.deals import router as deals_router
from api.routes.defaults import router as defaults_router
from api.routes.intake import router as intake_router
from api.routes.queue import router as queue_router
from api.security import SIGN_IN_PATH, CsrfRejected, NotSignedIn, over_https

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
API_DOCS_ENV = "GLENWOOD_API_DOCS"


def api_docs_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``GLENWOOD_API_DOCS`` is ``1``: read once, when the app is built."""
    return (os.environ if environ is None else environ).get(API_DOCS_ENV, "").strip() == "1"


_DOCS = api_docs_enabled()
app = FastAPI(
    title="glenwood-uw",
    version="0.1.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
# The brand assets (static/brand, docs/brand.md): the only files served as they are. Public,
# like the borrower form's own shell that shows them; nothing under static/ is a deal.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(auth_router)
app.include_router(queue_router)
app.include_router(intake_router)
app.include_router(deals_router)
app.include_router(defaults_router)
app.include_router(apply_router)

# Nothing under /apply is for a search engine (SPEC §4.2): the form is linked from where
# GLENWOOD puts it, and a thank-you page with a borrower's terms on it is theirs alone for
# as long as its link is live. The header goes on every response under the path - the
# form, the thank-you, the adjust page, a 422, a 429 - so a crawler that reached one is told
# the same thing whatever it found.
NOINDEX = "noindex"


@app.middleware("http")
async def noindex_the_borrower_pages(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """``X-Robots-Tag: noindex`` on every /apply route."""
    response = await call_next(request)
    path = request.url.path
    if path == APPLY_PATH or path.startswith(APPLY_PATH + "/"):
        response.headers["X-Robots-Tag"] = NOINDEX
    return response


# On every response, the mounted /static and every /apply page included (SPEC §11, v0.26).
# The CSP is frame-ancestors and nothing else - nobody frames these pages, which is also what
# X-Frame-Options says to a browser too old for CSP. A script-src would have to name
# challenges.cloudflare.com for the Turnstile widget on /apply, and is left out rather than
# kept in step with it.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
# Only over HTTPS: a browser ignores it on plain http, and a developer's localhost is better
# off never having been told it. No preload: that is a list a domain cannot easily leave.
HSTS = "max-age=31536000; includeSubDomains"


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """The four headers on every response, and ``Strict-Transport-Security`` over HTTPS."""
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if over_https(request):
        response.headers["Strict-Transport-Security"] = HSTS
    return response


@app.exception_handler(NotSignedIn)
def sign_in_first(request: Request, exc: NotSignedIn) -> Response:
    """Send a browser to the sign-in page, remembering where it was going."""
    del request
    return RedirectResponse(exc.next_url or SIGN_IN_PATH, status_code=HTTP_303_SEE_OTHER)


@app.exception_handler(CsrfRejected)
def csrf_rejected(request: Request, exc: CsrfRejected) -> Response:
    """403 with a page that says what happened, and that nothing was written.

    It touches no database. An error page that needs a query to render is an error page that
    can fail for a second reason while it is explaining the first.
    """
    return templates.TemplateResponse(
        request,
        "csrf_rejected.html",
        {
            "user": None,
            "notice": None,
            "problems": [],
            "errors": {},
            "csrf_token": "",
            "detail": exc.detail,
        },
        status_code=HTTP_403_FORBIDDEN,
    )
