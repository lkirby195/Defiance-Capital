"""The team-entry form: its fields, which of them are required, and how a deal fills it in.

One module because three routes need the same three things and must not disagree about any
of them: ``GET /queue/new`` renders the blank form, ``GET /queue/deals/{id}/intake`` renders
the same form filled in from the deal, and ``POST`` of either decides whether what came back
is complete.

**The form is labels and boxes**, three groups (SPEC §8.1): the Overview, the Property
Overview, and seven Deal Economics boxes - Purchase Price, Rehab Costs, Loan Amount, Loan
Purpose, Loan Type, Closing Date, Term (months), in that order. Nothing else is on it. The
§8.1 economics with a config default and the loan split populate from the defaults when the
deal is stored (``services/defaults.py``); the valuation, the rent, the analysis toggles and
the court search are the deal page's. All of it is edited there, and Edit Intake leaves all
of it alone (``db/repository.form_columns``).

**Required is what an engine run cannot proceed without**, and nothing else. That is the
SPEC §4.1 minimum viable intake, less the credit range, plus the two SPEC §8.1 inputs the
ledger has no stand-in for: the closing date and the term. The credit range is not a required
box because a person on the phone often does not have it yet - the screen names it by hand
when it runs without one (``services/assemble.py``), which is a better answer than a form
that will not submit. Nor is the phone: it is the borrower match key when there is one
(SPEC §5), and a deal that arrived by email has a name and no number. The page marks every
box Required or Optional, the browser refuses to post without the required ones, and this
module refuses again on the server - the attribute is a courtesy to a person typing, not a
control.

A partial capture is still a real thing (SPEC §4.1, ``NEEDS_INFO``): it arrives through the
channels that take one, which is every channel but this form, and through ``POST
/intake/team`` with a JSON body. What the form insists on is that a person sitting in front
of it finishes the boxes rather than leaving a deal nobody can price.

**What a person types is not what the deal stores.** A price is typed ``$425,000``, a rate
``12%`` and a phone ``555-123-4567``; the deal carries ``425000``, ``0.12`` and
``5551234567``. ``api/masks.py`` is both halves of that, and it runs here rather than on the
model, so the JSON body of the same route still speaks in what is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from api.masks import (
    config_defaults,
    default_text,
    holding_costs_amount,
    mask_one,
    masked,
    unmasked,
)
from api.problems import NO_PROBLEMS, FormProblems, by_field, from_validation_error
from config.config import Config, get_config
from db.models import Deal
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.masks import money_parse
from schema.models import Channel, IntakeRecord, ProductSource, StateSource
from services.defaults import defaults_for

# Every name the form renders, grouped as SPEC §8.1 groups them and in the order the page
# asks, so a blank the browser dropped is still a blank box.
TEAM_ENTRY_FIELDS: tuple[str, ...] = (
    # Overview
    "entity_name",
    "borrower_name",
    "borrower_phone",
    "borrower_email",
    "credit_range",
    "experience_bucket",
    "repeat_borrower",
    # Property Overview
    "address",
    "listing_url",
    "city",
    "state",
    "units",
    "structures",
    "sf",
    "year_built",
    "year_renovated",
    "beds",
    "baths",
    "garage_spaces",
    # Deal Economics, in this order and no more
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "loan_purpose",
    "product",
    "closing_date",
    "term_months",
)

# What an engine run cannot proceed without, in the order the form asks for it, each with the
# name the refusal calls it by. A field name would be accurate and would read like a bug
# report; a person needs the caption above the box they left empty.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("borrower_name", "Guarantor Name"),
    ("experience_bucket", "Deals in last 36 months"),
    ("repeat_borrower", "Repeat borrower"),
    ("address", "Address"),
    ("purchase_price", "Purchase Price"),
    ("rehab_costs", "Rehab Costs"),
    ("loan_requested", "Loan Amount"),
    ("closing_date", "Closing Date"),
    ("term_months", "Term (months)"),
)
REQUIRED_NAMES: frozenset[str] = frozenset(name for name, _ in REQUIRED_FIELDS)
# Every box this form renders. A complaint about one of these sits under it; anything else
# Pydantic names has nowhere to sit and goes to the top (``api/problems.py``).
TEAM_ENTRY_NAMES: frozenset[str] = frozenset(TEAM_ENTRY_FIELDS)


def missing_required(submitted: Mapping[str, str]) -> FormProblems:
    """One complaint per required box left empty, under that box.  # SPEC §4.1, §8.1

    ``api.forms.fields`` has already dropped the blanks, so an absent key is an empty box.
    Each line sits under the box it names rather than in a list at the top: a person fixing
    an empty box wants to be told about it while they are looking at it.
    """
    return by_field(
        {
            name: [f"{label} is required."]
            for name, label in REQUIRED_FIELDS
            if not str(submitted.get(name, "")).strip()
        }
    )


def text_value(value: Any) -> str:
    """A stored value as the string an ``<input>`` shows; None and absent both blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(getattr(value, "value", value))


def deal_defaults(deal: Deal | None, config: Config | None = None) -> dict[str, Decimal | None]:
    """The default for each defaultable §8.1 economic, on this deal.  # SPEC §8.1, §8.2

    The five config numbers on every deal; on a split product whose loan amount and rehab
    are known, the formula loan split as well (``services.defaults.defaults_for``). Read by
    the deal page's override block, which is where these boxes live now; the team form has
    none of them.
    """
    settings = config if config is not None else get_config()
    if deal is None:
        return config_defaults(settings)
    return dict(defaults_for(deal, settings))


def default_marks(deal: Deal | None, config: Config | None = None) -> dict[str, str]:
    """Each defaulted box's pre-filled text, for the override block's ``default`` tag."""
    return default_text(deal_defaults(deal, config))


def money_value(submitted: Mapping[str, str], name: str) -> Decimal | None:
    """One money box off a submitted form as a number, or None when it is not one."""
    text = money_parse(submitted.get(name, ""))
    try:
        return Decimal(text) if text else None
    except ArithmeticError:
        return None


def holding_costs_hint(
    pct: Decimal | None, purchase_price: Decimal | None, rehab_costs: Decimal | None
) -> str:
    """The line under the deal page's holding-cost box: what that percentage comes to.

    # SPEC §8.1. The box holds a percentage of the price plus the rehab and the figure a
    person checks is the dollars, so the dollars are printed beside it - once the price and
    the rehab are both known. Until then the line says what it is waiting for rather than
    showing a percentage of nothing.
    """
    amount = holding_costs_amount(pct, purchase_price, rehab_costs)
    if amount is None:
        return "of the purchase price plus the rehab costs, over the whole hold"
    cost = (purchase_price or Decimal(0)) + (rehab_costs or Decimal(0))
    return f"${amount:,.2f} over the whole hold, on a ${cost:,.2f} cost basis"


def deal_holding_costs_hint(deal: Deal | None, config: Config | None = None) -> str:
    """The same line, for a deal: its own percentage, else the config default."""
    settings = config if config is not None else get_config()
    if deal is None:
        return holding_costs_hint(None, None, None)
    pct = deal.holding_costs_pct_of_cost
    if pct is None:
        pct = settings.fees.holding_costs_default_pct_of_cost
    return holding_costs_hint(pct, deal.purchase_price, deal.rehab_costs)


def submitted_holding_costs_hint(submitted: Mapping[str, str], config: Config | None = None) -> str:
    """The same line, for an override block on its way back to a person who got something wrong."""
    settings = config if config is not None else get_config()
    values = unmasked(submitted)
    try:
        typed = Decimal(values["holding_costs_pct_of_cost"])
    except (ArithmeticError, KeyError, ValueError):
        typed = settings.fees.holding_costs_default_pct_of_cost
    return holding_costs_hint(
        typed, money_value(submitted, "purchase_price"), money_value(submitted, "rehab_costs")
    )


def intake_form_values(deal: Deal) -> dict[str, str]:
    """The deal as the flat form that produced it, ready to be edited and posted back.

    Two fields are deliberately blank when the deal never carried an answer of its own.
    ``state`` and ``product`` are both stored with a source column, and both are inferred
    from something else when nobody chose (SPEC §3, §4.5); rendering the inferred value into
    the box would have the next save record it as a person's choice. A blank re-derives in
    either case, which is what the deal currently says.
    """
    borrower = deal.borrower
    prop = deal.property
    # A borrower keeps every entity they have ever inquired under (they are many-to-many and
    # shared with their other deals), so the box shows the newest - the one this deal was
    # most likely entered against - rather than whichever the join happened to return first.
    entities = sorted(borrower.entities, key=lambda e: (e.created_at, e.name)) if borrower else []
    values: dict[str, Any] = {
        "entity_name": entities[-1].name if entities else None,
        "borrower_name": borrower.name if borrower is not None else None,
        "borrower_phone": borrower.phone if borrower is not None else None,
        "borrower_email": borrower.email if borrower is not None else None,
        "credit_range": deal.credit_range_self_reported,
        "experience_bucket": deal.experience_bucket_self_reported,
        "repeat_borrower": deal.repeat_borrower_self_reported,
        "address": prop.address_raw if prop is not None else None,
        "listing_url": prop.listing_url if prop is not None else None,
        "city": prop.city if prop is not None else None,
        "state": (
            prop.state if prop is not None and prop.state_source is StateSource.ENTERED else None
        ),
        "units": prop.units if prop is not None else None,
        "structures": prop.structures if prop is not None else None,
        "sf": prop.sf if prop is not None else None,
        "year_built": prop.year_built if prop is not None else None,
        "year_renovated": prop.year_renovated if prop is not None else None,
        "beds": prop.beds if prop is not None else None,
        "baths": prop.baths if prop is not None else None,
        "garage_spaces": prop.garage_spaces if prop is not None else None,
        "purchase_price": deal.purchase_price,
        "rehab_costs": deal.rehab_costs,
        "loan_requested": deal.loan_requested,
        "loan_purpose": deal.loan_purpose,
        "product": deal.product if deal.product_source is ProductSource.ENTERED else None,
        "closing_date": deal.closing_date,
        "term_months": deal.term_months,
    }
    # Keyed by TEAM_ENTRY_FIELDS rather than by ``values``, so a name that drifts out of one
    # of the two renders as an empty box instead of a StrictUndefined blowing up the page;
    # tests/test_intake_form.py asserts the two agree.
    return {name: mask_one(name, values.get(name)) for name in TEAM_ENTRY_FIELDS}


def blank_form_values() -> dict[str, str]:
    """The new-deal form: every box empty."""
    return dict.fromkeys(TEAM_ENTRY_FIELDS, "")


def read_form(submitted: Mapping[str, str]) -> tuple[TeamEntryForm | None, FormProblems]:
    """The posted form as a model, or the complaints saying why it is not one.

    The masks come off first, so what reaches ``TeamEntryForm`` is what the deal stores: a
    price as a number, a phone as digits.

    Both halves of the complaint run: a missing Required box and a price with three decimal
    places are two different problems with the same submission, and a person fixing one at a
    time is a person posting twice. Each lands under the box it is about where it has one
    (``api/problems.py``).
    """
    missing = missing_required(submitted)
    values = unmasked(submitted)
    try:
        form = TeamEntryForm.model_validate(values)
    except ValidationError as exc:
        return None, missing.merge(from_validation_error(exc, TEAM_ENTRY_NAMES))
    return (None, missing) if missing else (form, NO_PROBLEMS)


def redisplay_values(submitted: Mapping[str, str]) -> dict[str, str]:
    """A rejected submission back in the boxes it came out of, still masked.

    The text is re-masked rather than echoed: a person who typed ``425000`` gets
    ``$425,000`` back, which is what the box would have shown them had the post succeeded.
    """
    return masked(unmasked(submitted))


def intake_record(form: TeamEntryForm) -> IntakeRecord:
    """Normalize a validated team-entry form into the record both intake writes take.

    The raw payload kept on the immutable ``intake_submissions`` row is the validated form
    rather than the bytes that arrived, so a JSON post and a form post of the same deal are
    stored identically and neither carries a stray control field.
    """
    payload = form.model_dump(mode="json", exclude_none=True)
    return normalize(parse_team_form(form), Channel.TEAM, raw_payload=payload)
