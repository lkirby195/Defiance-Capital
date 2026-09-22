"""The team-entry form: its fields, which of them are required, and how a deal fills it in.

One module because three routes need the same three things and must not disagree about any
of them: ``GET /queue/new`` renders the blank form, ``GET /queue/deals/{id}/intake`` renders
the same form filled in from the deal, and ``POST`` of either decides whether what came back
is complete.

**Required is the minimum viable intake** (SPEC §4.1) and nothing else. Everything else on
the form is a thing the team may or may not have: an email, a county, a valuation the
adapters will eventually produce. The page marks every field one way or the other, the
browser refuses to post without the required ones, and this module refuses again on the
server - the attribute is a courtesy to a person typing, not a control.

A partial capture is still a real thing (SPEC §4.1, ``NEEDS_INFO``): it arrives through the
channels that take one, which is every channel but this form, and through ``POST
/intake/team`` with a JSON body. What the form insists on is that a person sitting in front
of it finishes the ten boxes rather than leaving a deal nobody can price.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from api.forms import problems
from db.models import Deal
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import Channel, IntakeRecord, ProductSource, StateSource

# Every name the form renders, so a blank the browser dropped is still a blank box.
TEAM_ENTRY_FIELDS: tuple[str, ...] = (
    "borrower_name",
    "borrower_phone",
    "borrower_email",
    "entity_name",
    "credit_range",
    "experience_bucket",
    "repeat_borrower",
    "address",
    "listing_url",
    "county",
    "state",
    "purchase_price",
    "rehab_budget",
    "loan_requested",
    "term_bucket",
    "product",
    "asset_type",
    "stated_exit",
    "as_is_value_team",
    "arv_team",
    "actual_annual_taxes_usd",
    "actual_annual_insurance_usd",
    "actual_annual_utilities_usd",
    "market_rent_monthly",
    "court_records_status",
    "court_records_as_of",
)

# The minimum viable intake (SPEC §4.1), in the order the form asks for it, each with the
# name the refusal calls it by. A field name would be accurate and would read like a bug
# report; a person needs the caption above the box they left empty.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("borrower_name", "Name"),
    ("borrower_phone", "Phone"),
    ("credit_range", "Credit range"),
    ("experience_bucket", "Deals in the last 36 months"),
    ("repeat_borrower", "Repeat borrower"),
    ("address", "Address"),
    ("purchase_price", "Purchase price"),
    ("rehab_budget", "Rehab budget"),
    ("loan_requested", "Loan requested"),
    ("term_bucket", "Term"),
)
REQUIRED_NAMES: frozenset[str] = frozenset(name for name, _ in REQUIRED_FIELDS)


def missing_required(submitted: Mapping[str, str]) -> list[str]:
    """One line per required box left empty, naming it.  # SPEC §4.1

    ``api.forms.fields`` has already dropped the blanks, so an absent key is an empty box.
    Returned in form order, so the list reads down the page rather than in whatever order a
    browser happened to send.
    """
    return [
        f"{label} is required."
        for name, label in REQUIRED_FIELDS
        if not str(submitted.get(name, "")).strip()
    ]


def text_value(value: Any) -> str:
    """A stored value as the string an ``<input>`` shows; None and absent both blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(getattr(value, "value", value))


def intake_form_values(deal: Deal) -> dict[str, str]:
    """The deal as the flat form that produced it, ready to be edited and posted back.

    Two fields are deliberately blank when the deal never carried an answer of its own.
    ``state`` and ``product`` are both stored with a source column, and both are inferred
    from something else when nobody chose (SPEC §3, §4.5); rendering the inferred value into
    the box would have the next save record it as a person's choice. A blank re-infers, which
    is what the deal currently says.
    """
    borrower = deal.borrower
    prop = deal.property
    # A borrower keeps every entity they have ever inquired under (they are many-to-many and
    # shared with their other deals), so the box shows the newest - the one this deal was
    # most likely entered against - rather than whichever the join happened to return first.
    entities = sorted(borrower.entities, key=lambda e: (e.created_at, e.name)) if borrower else []
    values: dict[str, Any] = {
        "borrower_name": borrower.name if borrower is not None else None,
        "borrower_phone": borrower.phone if borrower is not None else None,
        "borrower_email": borrower.email if borrower is not None else None,
        "entity_name": entities[-1].name if entities else None,
        "credit_range": deal.credit_range_self_reported,
        "experience_bucket": deal.experience_bucket_self_reported,
        "repeat_borrower": deal.repeat_borrower_self_reported,
        "address": prop.address_raw if prop is not None else None,
        "listing_url": prop.listing_url if prop is not None else None,
        "county": prop.county if prop is not None else None,
        "state": (
            prop.state if prop is not None and prop.state_source is StateSource.ENTERED else None
        ),
        "purchase_price": deal.purchase_price,
        "rehab_budget": deal.rehab_budget,
        "loan_requested": deal.loan_requested,
        "term_bucket": deal.term_bucket,
        "product": deal.product if deal.product_source is ProductSource.ENTERED else None,
        "asset_type": deal.asset_type,
        "stated_exit": deal.stated_exit,
        "as_is_value_team": deal.as_is_value_team,
        "arv_team": deal.arv_team,
        "actual_annual_taxes_usd": deal.actual_annual_taxes_usd,
        "actual_annual_insurance_usd": deal.actual_annual_insurance_usd,
        "actual_annual_utilities_usd": deal.actual_annual_utilities_usd,
        "market_rent_monthly": deal.market_rent_monthly,
        "court_records_status": deal.court_records_status,
        "court_records_as_of": deal.court_records_as_of,
    }
    # Keyed by TEAM_ENTRY_FIELDS rather than by ``values``, so a name that drifts out of one
    # of the two renders as an empty box instead of a StrictUndefined blowing up the page;
    # tests/test_intake_form.py asserts the two agree.
    return {name: text_value(values.get(name)) for name in TEAM_ENTRY_FIELDS}


def read_form(
    submitted: Mapping[str, str], matters: list[dict[str, str]]
) -> tuple[TeamEntryForm | None, list[str]]:
    """The posted form as a model, or the lines saying why it is not one.

    Both halves run: a missing Required box and a price with three decimal places are two
    different problems with the same submission, and a person fixing one at a time is a
    person posting twice. ``missing_required`` comes first because it is the complaint that
    names a box they can see.
    """
    missing = missing_required(submitted)
    try:
        form = TeamEntryForm.model_validate({**submitted, "court_records_team": matters})
    except ValidationError as exc:
        return None, missing + problems(exc)
    return (None, missing) if missing else (form, [])


def intake_record(form: TeamEntryForm) -> IntakeRecord:
    """Normalize a validated team-entry form into the record both intake writes take.

    The raw payload kept on the immutable ``intake_submissions`` row is the validated form
    rather than the bytes that arrived, so a JSON post and a form post of the same deal are
    stored identically and neither carries a stray control field.
    """
    payload = form.model_dump(mode="json", exclude_none=True)
    return normalize(parse_team_form(form), Channel.TEAM, raw_payload=payload)
