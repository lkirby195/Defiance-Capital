"""Turn parsed intake into an ``IntakeRecord`` and list what is still missing.  # SPEC §4.1, §4.5

Every channel's parser (``intake/parsers/``) produces a ``ParsedIntake``; this
module is the only writer of ``missing_fields`` and of the initial ``status``:
``NEEDS_INFO`` while anything from the minimum viable intake is absent, ``NEW``
once it is complete and ready to screen (SPEC §7 screens every complete intake).

Normalization here is deliberately light: the phone goes to E.164 for NANP
numbers so it works as the borrower match key (SPEC §5), the address gets
whitespace/case cleanup, and the state is inferred from the address text unless a
person entered it (``state_source`` records which).
Parcel, county, and USPS-form addresses come from enrichment (SPEC §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from schema.models import (
    BorrowerInfo,
    Channel,
    DealInfo,
    IntakeRecord,
    PropertyInfo,
    State,
    StateSource,
    Status,
)

# The minimum viable intake, in the order the team should ask for it.  # SPEC §4.1
MINIMUM_FIELDS: tuple[str, ...] = (
    "property.address_raw",
    "deal.purchase_price",
    "deal.rehab_budget",
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


def missing_fields(borrower: BorrowerInfo, prop: PropertyInfo, deal: DealInfo) -> list[str]:
    """Minimum-viable fields (SPEC §4.1) that are still absent, in asking order."""
    present = {
        # A listing/auction link satisfies the property requirement (SPEC §4.1 row 1).
        "property.address_raw": bool(prop.address_raw or prop.listing_url),
        "deal.purchase_price": deal.purchase_price is not None,
        "deal.rehab_budget": deal.rehab_budget is not None,  # 0 is allowed
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
            "county": clean_text(parsed.property.county),
            "state": state,
            "state_source": state_source,
        }
    )
    missing = missing_fields(borrower, prop, parsed.deal)
    return IntakeRecord(
        channel=channel,
        raw_payload=raw_payload,
        borrower=borrower,
        property=prop,
        deal=parsed.deal,
        missing_fields=missing,
        status=Status.NEEDS_INFO if missing else Status.NEW,
    )
