"""Turn parsed intake into an ``IntakeRecord`` and list what is still missing.  # SPEC §4.1, §4.5

Every channel's parser (``intake/parsers/``) produces a ``ParsedIntake``; this
module is the only writer of ``missing_fields`` and of the initial ``status``:
``NEEDS_INFO`` while anything from the minimum viable intake is absent, ``NEW``
once it is complete and ready to screen (SPEC §7 screens every complete intake).

Normalization here is deliberately light: the phone goes to E.164 for NANP
numbers so it works as the borrower match key (SPEC §5), the address gets
whitespace/case cleanup, and the state is inferred from the address text unless a
person entered it (``state_source`` records which). The product is inferred from the
rehab costs unless the team entered it (``product_source`` records which). The term is the
team's own when they set one and the bucket's number otherwise (SPEC §8.1).
Parcel, county, and USPS-form addresses come from enrichment (SPEC §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from schema.models import (
    BorrowerInfo,
    Channel,
    DealInfo,
    IntakeRecord,
    Product,
    ProductSource,
    PropertyInfo,
    State,
    StateSource,
    Status,
    TermBucket,
    months_for_bucket,
)

# The minimum viable intake, in the order the team should ask for it.  # SPEC §4.1
MINIMUM_FIELDS: tuple[str, ...] = (
    "property.address_raw",
    "deal.purchase_price",
    "deal.rehab_costs",
    "deal.loan_requested",
    "deal.term_bucket",
    "borrower.name",
    "borrower.phone",
    "borrower.credit_range",
    "borrower.experience_bucket",
    "borrower.repeat_borrower",
)

_STATE_WORDS: dict[str, State] = {
    "OK": State.OK,
    "OKLAHOMA": State.OK,
    "CO": State.CO,
    "COLORADO": State.CO,
}
# Trailing "OK", ", Oklahoma", "CO 80202", ", co, 80202-1234"
_TRAILING_STATE = re.compile(
    r"[,\s]+([A-Za-z]{2}|Oklahoma|Colorado)\s*(?:,?\s*\d{5}(?:-\d{4})?)?\s*$",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_COMMA = re.compile(r"\s*,\s*")
_NON_DIGIT = re.compile(r"\D+")


@dataclass(frozen=True)
class ParsedIntake:
    """Channel-agnostic output of every parser; any field may still be None."""

    borrower: BorrowerInfo = field(default_factory=BorrowerInfo)
    property: PropertyInfo = field(default_factory=PropertyInfo)
    deal: DealInfo = field(default_factory=DealInfo)


def clean_text(raw: str | None) -> str | None:
    """Strip surrounding whitespace; empty becomes None."""
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def normalize_phone(raw: str | None) -> str | None:
    """E.164 for 10-digit and 1+10-digit NANP numbers; anything else passes through stripped."""
    text = clean_text(raw)
    if text is None:
        return None
    digits = _NON_DIGIT.sub("", text)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return text


def normalize_address(raw: str | None) -> str | None:
    """Collapse whitespace, tidy commas, drop stray punctuation, upper-case. Not USPS form."""
    if raw is None:
        return None
    text = _WHITESPACE.sub(" ", raw).strip(" ,.;")
    if not text:
        return None
    return _COMMA.sub(", ", text).upper()


def infer_state(address: str | None) -> State:
    """State from a trailing 'OK 73102' / ', Colorado' token; OTHER when absent or elsewhere."""
    if not address:
        return State.OTHER
    match = _TRAILING_STATE.search(address)
    if match is None:
        return State.OTHER
    return _STATE_WORDS.get(match.group(1).upper(), State.OTHER)


def infer_term_months(bucket: TermBucket | None, entered: int | None) -> int | None:
    """The team's own term; the months the bucket names when nobody has set one.  # SPEC §8.1

    The bucket is the borrower's answer to "how long do you need the loan?", so it seeds the
    term and does not fix it: the deal is priced on the team's number - typed, or implied by
    a payoff date - and a deal repriced to 7 months on a 6-month ask is a real thing. Without
    one, the bucket's own number stands, and ``12_PLUS`` names none at all.
    """
    return entered if entered is not None else months_for_bucket(bucket)


def infer_product(rehab_costs: Decimal | None) -> Product | None:
    """NO_DRAW when there are no rehab costs, else SPLIT_DRAW; None until they are known.

    WHOLETAIL and SPLIT_PRINCIPAL are never inferred; the team sets them.  # SPEC §3
    """
    if rehab_costs is None:
        return None
    return Product.NO_DRAW if rehab_costs == 0 else Product.SPLIT_DRAW


def missing_fields(borrower: BorrowerInfo, prop: PropertyInfo, deal: DealInfo) -> list[str]:
    """Minimum-viable fields (SPEC §4.1) that are still absent, in asking order."""
    present = {
        # A listing/auction link satisfies the property requirement (SPEC §4.1 row 1).
        "property.address_raw": bool(prop.address_raw or prop.listing_url),
        "deal.purchase_price": deal.purchase_price is not None,
        "deal.rehab_costs": deal.rehab_costs is not None,  # 0 is allowed
        "deal.loan_requested": deal.loan_requested is not None,
        "deal.term_bucket": deal.term_bucket is not None,
        "borrower.name": bool(borrower.name),
        "borrower.phone": bool(borrower.phone),
        "borrower.credit_range": borrower.credit_range is not None,
        "borrower.experience_bucket": borrower.experience_bucket is not None,
        "borrower.repeat_borrower": borrower.repeat_borrower is not None,
    }
    return [name for name in MINIMUM_FIELDS if not present[name]]


def normalize(
    parsed: ParsedIntake, channel: Channel, raw_payload: dict[str, Any] | str
) -> IntakeRecord:
    """Build the ``IntakeRecord``: light normalization, ``missing_fields``, initial ``status``."""
    borrower = parsed.borrower.model_copy(
        update={
            "name": clean_text(parsed.borrower.name),
            "phone": normalize_phone(parsed.borrower.phone),
            "email": clean_text(parsed.borrower.email),
            "entity_name": clean_text(parsed.borrower.entity_name),
        }
    )
    address_raw = clean_text(parsed.property.address_raw)
    state = parsed.property.state
    state_source = parsed.property.state_source
    if state_source is not StateSource.ENTERED:
        state = infer_state(address_raw)
        state_source = StateSource.INFERRED
    prop = parsed.property.model_copy(
        update={
            "address_raw": address_raw,
            "address_normalized": parsed.property.address_normalized
            or normalize_address(address_raw),
            "listing_url": clean_text(parsed.property.listing_url),
            "city": clean_text(parsed.property.city),
            "county": clean_text(parsed.property.county),
            "state": state,
            "state_source": state_source,
        }
    )
    deal = parsed.deal
    term = infer_term_months(deal.term_bucket, deal.term_months)
    if term != deal.term_months:
        deal = deal.model_copy(update={"term_months": term})
    if deal.product is None:
        inferred = infer_product(deal.rehab_costs)
        if inferred is not None:
            deal = deal.model_copy(
                update={"product": inferred, "product_source": ProductSource.INFERRED}
            )
    missing = missing_fields(borrower, prop, deal)
    return IntakeRecord(
        channel=channel,
        raw_payload=raw_payload,
        borrower=borrower,
        property=prop,
        deal=deal,
        missing_fields=missing,
        status=Status.NEEDS_INFO if missing else Status.NEW,
    )
