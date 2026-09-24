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

Nothing is public. Every route in every router is behind a session cookie, because there is
nothing here that is not a deal: credit ranges, court findings, and what GLENWOOD decided
about a borrower (SPEC §11).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER, HTTP_403_FORBIDDEN

from api.render import templates
from api.routes.auth import router as auth_router
from api.routes.deals import router as deals_router
from api.routes.intake import router as intake_router
from api.routes.queue import router as queue_router
from api.security import SIGN_IN_PATH, CsrfRejected, NotSignedIn

app = FastAPI(title="glenwood-uw", version="0.1.0")
app.include_router(auth_router)
app.include_router(queue_router)
app.include_router(intake_router)
app.include_router(deals_router)


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
