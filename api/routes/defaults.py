"""GET /defaults: every config value, grouped, read-only.  # SPEC §10

Behind sign-in like every queue page (``PageUser`` sends a stranger to /login). There is
nothing to post: the yaml is changed by whoever runs the server, and a change takes effect
when the service restarts and ``Config`` validates it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse

from api.defaults_page import caps_tables, groups
from api.render import page
from api.security import PageUser
from config.config import get_config

router = APIRouter(tags=["defaults"])

DEFAULTS_PATH = "/defaults"


@router.get(DEFAULTS_PATH, response_class=HTMLResponse, response_model=None)
def defaults_page(request: Request, user: PageUser) -> HTMLResponse:
    """Every value in ``config/glenwood.yaml``, by group, with its dotted key."""
    config = get_config()
    return page(
        request,
        "defaults.html",
        {"groups": groups(config), "caps": caps_tables(config), "config_hash": config.config_hash},
        user=user,
    )
