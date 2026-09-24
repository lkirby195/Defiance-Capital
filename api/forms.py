"""Read an HTML form into the models the services already take.  # SPEC §12

The queue is server-rendered with no frontend framework, so every write arrives as a plain
``application/x-www-form-urlencoded`` POST. That shapes three things.

First, blanks. A browser sends every box, including the empty ones, as the empty string;
Pydantic wants those absent. ``fields()`` drops blanks on the way in, so a field a person
cleared and a field a person never filled reach the model the same way - as None - which is
exactly what a blank means in the override block (``services/requests.py``).

Second, the repeated rows. Court matters (SPEC §7.2) are one typed entry each and there can
be several, which plain HTML does with repeated names: ``matter_code`` three times alongside
``matter_amount_usd`` three times. ``rows()`` zips those parallel lists back into one dict per
row and drops the rows that are entirely blank, so a form rendered with spare empty rows
submits cleanly.

Third, the three-state booleans. ``senior``, ``resolved_at_close`` and ``repeat_borrower`` are
``bool | None``, and None is a real answer meaning nobody said - a checkbox cannot express
that, because an unticked box is indistinguishable from a box nobody looked at and is not
even sent. They are rendered as a blank / Yes / No select instead, which submits "", "true"
or "false" and lands on None, True or False without another rule anywhere.

Nothing here validates. It produces dicts; the Pydantic model on the far side decides whether
they are any good, and ``api/problems.py`` files its complaint under the box each line is
about.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.datastructures import FormData

# The hidden field every form post in the queue carries (``api/security.py``).
CSRF_FIELD = "_csrf"


def fields(form: FormData, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """Every single-valued, non-blank field, stripped.

    ``skip`` drops name prefixes that belong to a repeated group - ``rows()`` reads those,
    and a repeated name would otherwise arrive here as its first value alone and be rejected
    by a model that forbids extras.

    The CSRF field is always dropped, without being asked. It is on every form and it is
    never data; leaving that to each caller to remember would mean every model with
    ``extra="forbid"`` rejecting a perfectly good submission the first time somebody forgot.

    Non-string parts are ignored: v1 takes no upload (contract OCR waits on the LinkedPhone
    recon), and a stray file part must not reach a model as the string "<UploadFile>".
    """
    out: dict[str, str] = {}
    for key in form.keys():
        if key == CSRF_FIELD or any(key.startswith(prefix) for prefix in skip):
            continue
        value = form.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text:
            out[key] = text
    return out


def rows(form: FormData, prefix: str, names: tuple[str, ...]) -> list[dict[str, str]]:
    """Parallel repeated fields as one dict per row, blank cells and blank rows dropped.

    ``rows(form, "matter", ("code", "amount_usd"))`` reads ``matter_code`` and
    ``matter_amount_usd``. Every control in a repeated row is always submitted - the selects
    send "" when unanswered - so the lists stay the same length and position is enough to
    keep a row together. A short list is tolerated anyway rather than trusted.
    """
    columns = {name: [str(v).strip() for v in form.getlist(f"{prefix}_{name}")] for name in names}
    height = max((len(values) for values in columns.values()), default=0)
    out: list[dict[str, str]] = []
    for index in range(height):
        row = {
            name: values[index]
            for name, values in columns.items()
            if index < len(values) and values[index]
        }
        if row:
            out.append(row)
    return out


async def form_data(request: Request) -> FormData:
    """FastAPI dependency: the parsed form body.

    An async dependency in front of a sync route, so the body is read on the event loop and
    the route's database work still runs in the threadpool. Starlette parses
    ``application/x-www-form-urlencoded`` itself; nothing here takes a multipart upload.
    """
    return await request.form()


FormDep = Annotated[FormData, Depends(form_data)]


def safe_next(target: str | None, fallback: str) -> str:
    """A ``?next=`` the caller supplied, refused unless it is a path on this site.

    A sign-in page that redirects to whatever it was handed is an open redirect, which is
    exactly the shape a phishing link wants. Only a single-slash absolute path is allowed;
    ``//elsewhere.example`` is protocol-relative and is not one.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_CONTENT_TYPE = "application/json"


def content_type(request: Request) -> str:
    """The request's media type, without its parameters."""
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def is_json_post(request: Request) -> bool:
    """True when the body is JSON.

    Worth its own predicate because it is the one body an HTML form cannot produce: a form's
    ``enctype`` is urlencoded, multipart or text/plain and nothing else, and a cross-origin
    ``fetch`` that sets ``application/json`` earns a preflight this app answers for nobody.
    """
    return content_type(request) == JSON_CONTENT_TYPE


def is_form_post(request: Request) -> bool:
    """True when the body is an HTML form rather than JSON.

    Read off the request's own ``Content-Type``, which is set before the body is touched, so
    a dependency can branch on it without consuming anything. Deciding on ``Accept`` instead
    would work for a browser and be a coin toss for every other client.
    """
    return content_type(request) == FORM_CONTENT_TYPE
