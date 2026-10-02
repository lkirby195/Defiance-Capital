"""The public borrower form: its boxes, which are required, and what it refuses.  # SPEC §4.2

The page marks every box Required or Optional, the browser refuses to post without the
required ones, a few lines of script keep the button off until they are filled, and this
module refuses again on the server - because anything can post a form, and the attribute and
the script are a courtesy to a person typing on a phone, not a control.

Every complaint is written in the borrower's language and filed under the box it is about
(``api/problems.py``). The messages are this module's own rather than Pydantic's: a borrower
is told "please enter a 10-digit phone number", not ``String should match pattern``, and is
told it in Spanish when the page is in Spanish.

**The listing link stands in for a blank address.** A borrower who pastes a Zillow link and
leaves the address boxes empty has told us where the property is; the address is read off the
link's slug before the required check runs (``adapters/listing_url.py``), each blank box is
filled from it, and only what the link did not say is then asked for. A state read from the
link is recorded as inferred rather than chosen (SPEC §4.5). Nothing fetches the page.

**What a borrower types is not what the deal stores.** The money boxes take ``$185,000`` and
the phone box ``918-555-0142``; the model is built from ``185000`` and ``9185550142``. The
masks come off here and go back on for a rejection, so a person who typed ``$185,000`` gets
``$185,000`` back rather than the number it parsed to.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from adapters.listing_url import address_from_url
from api.masks import mask_one
from api.problems import NO_PROBLEMS, FormProblems, at_top, by_field
from config.config import Config, get_config
from intake.normalize import normalize
from intake.parsers.web_form import WebApplyForm, parse_web_form
from schema.labels import experience_label, tranche_label
from schema.masks import money_display, money_parse, phone_digits
from schema.models import (
    Channel,
    ExperienceBucket,
    IntakeRecord,
    State,
    TermBucket,
    Tranche,
)

# Every box the page renders, in the order it asks for them (SPEC §4.2), so a blank the
# browser dropped is still a blank box when the page is put back.
APPLY_FIELDS: tuple[str, ...] = (
    # 1. about you
    "borrower_name",
    "entity_name",
    "borrower_phone",
    "borrower_email",
    "credit_range",
    "experience_bucket",
    "repeat_borrower",
    # 2. the property
    "address",
    "city",
    "state",
    "listing_url",
    # 3. the deal
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "estimated_sale_price",
    "monthly_rent",
    # 4. timing
    "term_bucket",
    "closing_date",
    # 5. how you heard
    "referral_note",
)
# What the page marks Required and the browser's ``required`` attribute is set on - the
# SPEC §4.1 minimum and the way to reach the borrower. Consent to being reached is the
# submission itself: the line above the button says so, and the stored form records it.
REQUIRED_FIELDS: tuple[str, ...] = (
    "borrower_name",
    "borrower_phone",
    "borrower_email",
    "credit_range",
    "experience_bucket",
    "repeat_borrower",
    "address",
    "city",
    "state",
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "term_bucket",
)
REQUIRED_NAMES: frozenset[str] = frozenset(REQUIRED_FIELDS)
# The boxes a listing link can fill in, in the order the page shows them.
ADDRESS_FIELDS: tuple[str, ...] = ("address", "city", "state")
MONEY_FIELDS: tuple[str, ...] = (
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "estimated_sale_price",
    "monthly_rent",
)
# Money that has to be more than nothing: a price of $0 and a loan of $0 are not a deal.
POSITIVE_MONEY: frozenset[str] = frozenset(
    {"purchase_price", "loan_requested", "estimated_sale_price"}
)
MAX_NOTE = 200
MAX_NAME = 200

# The field a person never sees. A bot filling every box fills this one too, and the post is
# dropped on the floor with a thank-you page, so the bot learns nothing (``api/routes/apply``).
HONEYPOT = "homepage"
# The two controls on the page that are not about the deal: the language the borrower chose
# and the ``?src=`` slug the link carried in.
LANGUAGE_FIELD = "lang"
SOURCE_FIELD = "src"
CONTROL_FIELDS: frozenset[str] = frozenset({HONEYPOT, LANGUAGE_FIELD, SOURCE_FIELD})

# What the ``?src=`` slug may look like: short, and nothing a page would need escaping.
MAX_SOURCE = 64


def clean_source(text: str | None) -> str | None:
    """A ``?src=`` slug as it is stored, or None when it is not one.

    Lower-cased letters, digits, dots, dashes and underscores, at most 64 of them. Anything
    else - a URL somebody pasted, a script tag, an empty string - is not a source and is
    dropped rather than refused: the form still works, it just does not say where it came from.
    """
    if not text:
        return None
    slug = text.strip().lower()
    if not slug or len(slug) > MAX_SOURCE:
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in slug):
        return None
    return slug


def fill_from_listing(submitted: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """The submission with any blank address box filled from the listing link.  # SPEC §4.4

    Returns the values and whether the state was read off the link rather than chosen. A
    link that carries no address changes nothing, and a box the borrower filled is never
    overwritten: the link only ever fills a blank.
    """
    values = dict(submitted)
    link = values.get("listing_url")
    if not link or all(values.get(name) for name in ADDRESS_FIELDS):
        return values, False
    found = address_from_url(link)
    if found is None:
        return values, False
    state_from_link = False
    if not values.get("address"):
        values["address"] = found.street
    if not values.get("city"):
        values["city"] = found.city
    if not values.get("state"):
        values["state"] = found.state if found.state in State.__members__ else State.OTHER.value
        state_from_link = True
    return values, state_from_link


def _money(text: str) -> Decimal | None:
    try:
        return Decimal(money_parse(text))
    except InvalidOperation:
        return None


def _is_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("http://", "https://")) and len(text) <= 2000


def _is_email(text: str) -> bool:
    """The least an address needs: one ``@`` with something either side and a dot after it."""
    local, at, domain = text.partition("@")
    return bool(at) and bool(local) and "." in domain and not domain.startswith(".")


def complaints(submitted: Mapping[str, str], t: Mapping[str, str]) -> FormProblems:
    """One line per thing wrong, under the box it is about, in the borrower's language.

    Every rule the page states is tested here: presence for the required boxes, shape for
    the phone, the email, the money and the link, membership for the choices. All of them
    run, so a person who got three boxes wrong reads three lines in three places rather than
    posting three times.
    """
    found: dict[str, list[str]] = {}

    def complain(name: str, key: str) -> None:
        found.setdefault(name, []).append(t[key])

    for name in REQUIRED_FIELDS:
        if not submitted.get(name, "").strip():
            complain(name, "err_required")

    if len(submitted.get("borrower_name", "")) > MAX_NAME:
        complain("borrower_name", "err_too_long")
    if len(submitted.get("entity_name", "")) > MAX_NAME:
        complain("entity_name", "err_too_long")
    phone = submitted.get("borrower_phone", "")
    if phone and len(phone_digits(phone) or "") != 10:
        complain("borrower_phone", "err_phone")
    email = submitted.get("borrower_email", "")
    if email and (not _is_email(email) or len(email) > 254):
        complain("borrower_email", "err_email")
    if submitted.get("credit_range") and submitted["credit_range"] not in Tranche.__members__:
        complain("credit_range", "err_choice")
    bucket = submitted.get("experience_bucket")
    if bucket and bucket not in {member.value for member in ExperienceBucket}:
        complain("experience_bucket", "err_choice")
    if submitted.get("repeat_borrower") not in ("", None, "true", "false"):
        complain("repeat_borrower", "err_choice")
    if submitted.get("state") and submitted["state"] not in State.__members__:
        complain("state", "err_choice")
    link = submitted.get("listing_url", "")
    if link and not _is_url(link):
        complain("listing_url", "err_url")
    for name in MONEY_FIELDS:
        text = submitted.get(name, "")
        if not text:
            continue
        amount = _money(text)
        if amount is None or not amount.is_finite():
            complain(name, "err_amount")
        elif amount < 0 or (name in POSITIVE_MONEY and amount == 0):
            complain(name, "err_amount_positive")
        elif amount >= Decimal("1000000000000") or amount != amount.quantize(Decimal("0.01")):
            complain(name, "err_amount")
    term = submitted.get("term_bucket")
    if term and term not in {member.value for member in TermBucket}:
        complain("term_bucket", "err_choice")
    closing = submitted.get("closing_date", "")
    if closing:
        try:
            date.fromisoformat(closing)
        except ValueError:
            complain("closing_date", "err_date")
    if len(submitted.get("referral_note", "")) > MAX_NOTE:
        complain("referral_note", "err_too_long")
    return by_field(found)


def stored_values(submitted: Mapping[str, str], state_from_link: bool) -> dict[str, Any]:
    """A submission that passed ``complaints`` as the units ``WebApplyForm`` stores."""
    values: dict[str, Any] = {}
    for name in APPLY_FIELDS:
        text = submitted.get(name, "").strip()
        if not text:
            continue
        if name in MONEY_FIELDS:
            values[name] = money_parse(text)
        elif name == "borrower_phone":
            values[name] = phone_digits(text)
        elif name == "repeat_borrower":
            values[name] = text == "true"
        else:
            values[name] = text
    values["state_entered"] = not state_from_link
    # Sending the form is the consent (the line above the button, ``api/i18n.py``), and the
    # stored form keeps the fact that it was given, exactly as the checkbox used to.
    values["consent"] = True
    return values


def read_form(
    submitted: Mapping[str, str], t: Mapping[str, str]
) -> tuple[WebApplyForm | None, FormProblems]:
    """The posted form as a model, or the complaints saying why it is not one."""
    values, state_from_link = fill_from_listing(submitted)
    problems = complaints(values, t)
    if problems:
        return None, problems
    try:
        form = WebApplyForm.model_validate(stored_values(values, state_from_link))
    except ValidationError:
        # ``complaints`` tests every rule the model has, so this is a rule the two disagree
        # on - a bug to fix rather than a message to write - and the page says the one thing
        # it can: look at the boxes.
        return None, at_top(t["fix_marked"])
    return form, NO_PROBLEMS


def redisplay_values(submitted: Mapping[str, str]) -> dict[str, str]:
    """A rejected submission back in its boxes, every box present, the masks back on."""
    values = dict.fromkeys(APPLY_FIELDS, "")
    for name in APPLY_FIELDS:
        text = submitted.get(name, "")
        if not text:
            continue
        if name in MONEY_FIELDS:
            # Re-masked when it is a number; as typed when it is not, so the complaint under
            # the box is about what the person wrote.
            amount = _money(text)
            values[name] = (
                money_display(amount) if amount is not None and amount.is_finite() else text
            )
        elif name == "borrower_phone":
            values[name] = mask_one(name, phone_digits(text) or text)
        else:
            values[name] = text
    return values


def blank_values() -> dict[str, str]:
    return dict.fromkeys(APPLY_FIELDS, "")


def intake_record(form: WebApplyForm, *, language: str, intake_source: str | None) -> IntakeRecord:
    """Normalize a validated web form into the record every intake write takes.

    The raw payload kept on the immutable ``intake_submissions`` row is the validated form
    in stored units plus the language the borrower chose, so the team can see which one to
    call them back in.
    """
    payload = form.model_dump(mode="json", exclude_none=True)
    payload["language"] = language
    return normalize(
        parse_web_form(form),
        Channel.WEB,
        raw_payload=payload,
        intake_source=intake_source,
        referral_note=form.referral_note,
    )


# --- the choices the page offers, labelled in the borrower's language ----------------------------


def credit_choices(t: Mapping[str, str], config: Config | None = None) -> list[tuple[str, str]]:
    """The five tranches as FICO ranges, read from the config cutoffs.  # SPEC §7.1

    The top and bottom ranges are words ("740 or higher", "Under 620") and the words are the
    borrower's; the ranges in between are numbers and need no translating.
    """
    cutoffs = (config if config is not None else get_config()).credit.tranche_cutoffs
    members = list(Tranche)
    out: list[tuple[str, str]] = []
    for tranche in members:
        if tranche is members[0]:
            label = t["credit_top"].format(n=cutoffs[tranche])
        elif tranche is members[-1]:
            label = t["credit_under"].format(n=cutoffs[members[-2]])
        else:
            label = tranche_label(tranche, cutoffs)
        out.append((tranche.value, label))
    return out


def experience_choices() -> list[tuple[str, str]]:
    """``0`` / ``1–2`` / ``3–5`` / ``6+``: numbers, the same in either language."""
    return [(member.value, experience_label(member)) for member in ExperienceBucket]


def term_choices(t: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(member.value, t[f"term_{member.value}"]) for member in TermBucket]


def state_choices(t: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(member.value, t[f"state_{member.value}"]) for member in State]
