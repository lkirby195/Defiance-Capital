"""The team-entry form: its fields, which of them are required, and how a deal fills it in.

One module because three routes need the same three things and must not disagree about any
of them: ``GET /queue/new`` renders the blank form, ``GET /queue/deals/{id}/intake`` renders
the same form filled in from the deal, and ``POST`` of either decides whether what came back
is complete.

**Required is what an engine run cannot proceed without**, and nothing else. That is the
SPEC §4.1 minimum viable intake, less the credit range, plus the three SPEC §8.1 inputs the
ledger has no stand-in for: the closing date, the term, and the interest rate. The credit
range is not a required box because a person on the phone often does not have it yet - the
screen names it by hand when it runs without one (``services/assemble.py``), which is a
better answer than a form that will not submit. Nor is the phone: it is the borrower match
key when there is one (SPEC §5), and a deal that arrived by email has a name and no number.
Everything else is a thing the team may or may not have: an email, a square footage, a
valuation the adapters will eventually produce. The page marks every field one way or the
other, the browser refuses to post without the required ones, and this module refuses again
on the server - the attribute is a courtesy to a person typing, not a control.

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

from api.forms import problems
from api.masks import (
    DEFAULTED_FIELDS,
    config_defaults,
    default_text,
    drop_defaults,
    mask_one,
    masked,
    unmasked,
)
from config.config import Config, get_config
from db.models import Deal
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.labels import enum_label
from schema.masks import money_parse
from schema.models import SPLIT_PRODUCTS, Channel, IntakeRecord, ProductSource, StateSource

# Every name the form renders, grouped as SPEC §8.1 groups them, so a blank the browser
# dropped is still a blank box. The Overview is who the borrower is; the loan's own terms -
# purpose, type, closing date, term - moved into Deal Economics, beside the money.
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
    "asset_type",
    "stated_exit",
    # Deal Economics
    "loan_purpose",
    "product",
    "closing_date",
    "term_months",
    "payoff_date",
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "loan_purchase_portion",
    "loan_rehab_portion",
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_total_usd",
    "origination_fee_pct",
    # Valuation, rent and the analysis toggles
    "estimated_sale_price_team",
    "monthly_rent",
    "flip_analysis",
    "rental_analysis",
    # Court and filing search
    "court_records_status",
    "court_records_as_of",
)

# What an engine run cannot proceed without, in the order the form asks for it, each with the
# name the refusal calls it by. A field name would be accurate and would read like a bug
# report; a person needs the caption above the box they left empty.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("borrower_name", "Guarantor Name"),
    ("experience_bucket", "Deals in last 36 months"),
    ("repeat_borrower", "Repeat borrower"),
    ("address", "Address"),
    ("closing_date", "Closing Date"),
    ("purchase_price", "Purchase Price"),
    ("rehab_costs", "Rehab Costs"),
    ("loan_requested", "Loan Amount"),
    ("interest_rate", "Interest Rate"),
)
REQUIRED_NAMES: frozenset[str] = frozenset(name for name, _ in REQUIRED_FIELDS)

# Required, but only on a split product (SPEC §8.2). The product box may be blank - the
# normalizer infers one from the rehab costs - so the browser cannot be told which of these
# two states the form is in, and it is marked "Required for a split" and checked here.
SPLIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("loan_purchase_portion", "Advance at Closing"),
    ("loan_rehab_portion", "Rehab Portion"),
)
SPLIT_NAMES: frozenset[str] = frozenset(name for name, _ in SPLIT_FIELDS)

# The term, in the two boxes that can say it (SPEC §8.1). One of them has to be filled in and
# neither on its own can be marked Required, so both carry the same conditional mark and the
# rule is checked here.
TERM_MONTHS = "term_months"
PAYOFF_DATE = "payoff_date"

# What the page prints on a box whose Required-ness depends on another answer, by name. The
# template renders these strings and ``tests/test_intake_form.py`` asserts it does, so the
# marker and the rule behind it cannot drift apart.
CONDITIONAL_MARKS: dict[str, str] = {
    "loan_purchase_portion": "Required for a split",
    "loan_rehab_portion": "Required for a split",
    TERM_MONTHS: "Required, or a payoff date",
    PAYOFF_DATE: "Required, or a term",
}


def missing_required(submitted: Mapping[str, str]) -> list[str]:
    """One line per required box left empty, naming it.  # SPEC §4.1, §8.1

    ``api.forms.fields`` has already dropped the blanks, so an absent key is an empty box.
    Returned in form order, so the list reads down the page rather than in whatever order a
    browser happened to send.
    """
    return [
        f"{label} is required."
        for name, label in REQUIRED_FIELDS
        if not str(submitted.get(name, "")).strip()
    ]


def split_problems(form: TeamEntryForm) -> list[str]:
    """The split the product asks for, or one line per thing wrong with it.  # SPEC §8.2

    Presence only; ``TeamEntryForm`` has already refused a split that does not add up or one
    on a product that has no split. Presence is the form's own rule rather than the model's,
    because a borrower-channel intake legitimately carries a loan amount and no split at all
    - a person filling this form in has the whole loan in front of them.
    """
    if form.effective_product not in SPLIT_PRODUCTS:
        return []
    product = enum_label(form.effective_product)
    return [
        f"{label} is required on a {product} loan."
        for name, label in SPLIT_FIELDS
        if getattr(form, name) is None
    ]


def term_problems(form: TeamEntryForm) -> list[str]:
    """The term, said one way or the other, or the line asking for it.  # SPEC §8.1

    ``TeamEntryForm`` has already refused a payoff date that is not a whole number of months
    after closing, and one that disagrees with a term beside it. What is left for the form is
    presence: the deal is priced on a term (SPEC §8.3) and this form does not ask the SPEC
    §4.1 bucket question, so one of the two boxes has to be filled in.
    """
    if form.effective_term_months is not None:
        return []
    return [
        "Term (months) is required: enter a term, or a payoff date to imply one. "
        "Entering either solves the other."
    ]


def text_value(value: Any) -> str:
    """A stored value as the string an ``<input>`` shows; None and absent both blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(getattr(value, "value", value))


def deal_defaults(deal: Deal | None, config: Config | None = None) -> dict[str, Decimal | None]:
    """The config default for each of the four §8.1 economics, for this deal.  # SPEC §8.1"""
    settings = config if config is not None else get_config()
    return config_defaults(
        settings,
        purchase_price=deal.purchase_price if deal is not None else None,
        rehab_costs=deal.rehab_costs if deal is not None else None,
    )


def submitted_defaults(
    submitted: Mapping[str, str], config: Config | None = None
) -> dict[str, Decimal | None]:
    """The same four, worked out from the price and rehab the submission itself carries.

    On the way in the two the holding-cost default depends on are in front of us, whether or
    not there is a deal behind the form yet, so the blank new-deal form gets the same rule as
    the edit.
    """
    settings = config if config is not None else get_config()

    def number(name: str) -> Decimal | None:
        text = money_parse(submitted.get(name, ""))
        try:
            return Decimal(text) if text else None
        except ArithmeticError:
            return None

    return config_defaults(
        settings, purchase_price=number("purchase_price"), rehab_costs=number("rehab_costs")
    )


def default_marks(deal: Deal | None, config: Config | None = None) -> dict[str, str]:
    """The four defaulted boxes' pre-filled text, for the template's ``default`` tag."""
    return default_text(deal_defaults(deal, config))


def intake_form_values(deal: Deal, config: Config | None = None) -> dict[str, str]:
    """The deal as the flat form that produced it, ready to be edited and posted back.

    Three fields are deliberately blank when the deal never carried an answer of its own.
    ``state`` and ``product`` are both stored with a source column, and both are inferred
    from something else when nobody chose (SPEC §3, §4.5); rendering the inferred value into
    the box would have the next save record it as a person's choice. ``payoff_date`` is not
    stored at all - it is the closing date plus the term (SPEC §8.1) - so the box is an empty
    alternative to the term box beside it rather than a value to edit. A blank re-derives in
    every one of the three cases, which is what the deal currently says.

    The four §8.1 economics with a config default are the opposite case: a blank means the
    default, so the box is pre-filled with it and tagged rather than left empty for a person
    to wonder about. A submission that comes back still holding the default stores nothing
    (``api/masks.py``), so the deal keeps saying "the config decides" until somebody types
    something else.
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
        "asset_type": deal.asset_type,
        "stated_exit": deal.stated_exit,
        "loan_purpose": deal.loan_purpose,
        "product": deal.product if deal.product_source is ProductSource.ENTERED else None,
        "closing_date": deal.closing_date,
        "term_months": deal.term_months,
        "payoff_date": None,
        "purchase_price": deal.purchase_price,
        "rehab_costs": deal.rehab_costs,
        "loan_requested": deal.loan_requested,
        "loan_purchase_portion": deal.loan_purchase_portion,
        "loan_rehab_portion": deal.loan_rehab_portion,
        "interest_rate": deal.interest_rate,
        "contingency_pct": deal.contingency_pct,
        "closing_costs_usd": deal.closing_costs_usd,
        "holding_costs_total_usd": deal.holding_costs_total_usd,
        "origination_fee_pct": deal.origination_fee_pct,
        "estimated_sale_price_team": deal.estimated_sale_price_team,
        "monthly_rent": deal.monthly_rent,
        "flip_analysis": deal.flip_analysis,
        "rental_analysis": deal.rental_analysis,
        "court_records_status": deal.court_records_status,
        "court_records_as_of": deal.court_records_as_of,
    }
    defaults = deal_defaults(deal, config)
    for name in DEFAULTED_FIELDS:
        if values.get(name) is None:
            values[name] = defaults[name]
    # Keyed by TEAM_ENTRY_FIELDS rather than by ``values``, so a name that drifts out of one
    # of the two renders as an empty box instead of a StrictUndefined blowing up the page;
    # tests/test_intake_form.py asserts the two agree.
    return {name: mask_one(name, values.get(name)) for name in TEAM_ENTRY_FIELDS}


def blank_form_values(config: Config | None = None) -> dict[str, str]:
    """The new-deal form: every box empty but the four the config has a default for."""
    defaults = default_text(deal_defaults(None, config))
    return {**dict.fromkeys(TEAM_ENTRY_FIELDS, ""), **defaults}


def read_form(
    submitted: Mapping[str, str],
    matters: list[dict[str, str]],
    config: Config | None = None,
) -> tuple[TeamEntryForm | None, list[str]]:
    """The posted form as a model, or the lines saying why it is not one.

    The masks come off first and the config defaults come out next, so what reaches
    ``TeamEntryForm`` is what the deal stores: a rate as a fraction, a price as a number, and
    nothing at all in a box still holding the value config would have supplied anyway.

    Both halves of the complaint run: a missing Required box and a price with three decimal
    places are two different problems with the same submission, and a person fixing one at a
    time is a person posting twice. ``missing_required`` comes first because it is the
    complaint that names a box they can see.
    """
    missing = missing_required(submitted)
    values = drop_defaults(unmasked(submitted), submitted_defaults(submitted, config))
    try:
        form = TeamEntryForm.model_validate({**values, "court_records_team": matters})
    except ValidationError as exc:
        return None, missing + problems(exc)
    complaints = missing + term_problems(form) + split_problems(form)
    return (None, complaints) if complaints else (form, [])


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
