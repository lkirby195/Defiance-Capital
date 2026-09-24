"""Which box on the queue's forms holds which kind of number.  # SPEC §8.1

``schema/masks.py`` knows how to turn a stored number into the text a box shows and back
again. This module knows *which* boxes: one set of field names per convention, shared by the
team-entry form and the deal page's override block because the two use the same names for the
same things.

Both directions are here and both run at the HTML boundary and nowhere else:

* ``masked`` turns stored values into the strings a form renders, so a box shows ``$425,000``
  and ``12%`` rather than ``425000.00`` and ``0.12``.
* ``unmasked`` turns a submitted form back into the strings Pydantic parses, so ``$425,000``
  and ``12%`` reach the model as ``425000`` and ``0.12``.

The JSON half of ``POST /intake/team`` does not go through either. A client posting JSON
sends the stored form — a rate is a fraction, a price is a number — which is what the API has
always taken and what ``schema/intake.json`` documents; the percent convention is a thing a
person types, not a change to what the deal carries.

**Defaults.** Four of the §8.1 economics have a config default (SPEC §8.1), and the form
pre-fills each box with it rather than leaving a blank that quietly means the same thing. The
deal still stores nothing: ``drop_defaults`` removes a submitted value that is exactly the
default, so the column stays NULL, the engine still reads the config, and the readiness
checklist still says DEFAULT rather than claiming somebody chose it. A person who wants the
default gets it by leaving the box alone; a person who wants 2.5% types 2.5 and the deal
carries it. The "default" tag on the page is that rule, said out loud.

All four are flat config values, so all four pre-fill on a blank new-deal form. Holding costs
are a percentage of the price plus the rehab (SPEC §8.1), and the dollar figure it comes to is
shown beside the box rather than typed into it — ``holding_costs_amount`` is that arithmetic,
and it has an answer only once the price and the rehab are both on the page.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from config.config import Config
from schema.masks import (
    money_display,
    money_parse,
    pct_display,
    pct_parse,
    phone_digits,
    phone_display,
)

# Every box that holds dollars, on either form.
MONEY_FIELDS: frozenset[str] = frozenset(
    {
        "purchase_price",
        "rehab_costs",
        "loan_requested",
        "loan_purchase_portion",
        "loan_rehab_portion",
        "closing_costs_usd",
        "estimated_sale_price_team",
        "monthly_rent",
    }
)
# Every box a person types a percent into. The deal carries the fraction.
PERCENT_FIELDS: frozenset[str] = frozenset(
    {"interest_rate", "contingency_pct", "holding_costs_pct_of_cost", "origination_fee_pct"}
)
PHONE_FIELDS: frozenset[str] = frozenset({"borrower_phone"})

# The four §8.1 economics with a config default, and what the page calls each of them.
DEFAULTED_FIELDS: tuple[str, ...] = (
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
)

ZERO = Decimal(0)


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def mask_one(name: str, value: Any) -> str:
    """One stored value as the text its box shows; anything unmasked as-is.

    A bool comes back as ``true`` / ``false`` because that is what the three-state controls
    submit and select on (``api/templates/_fields.html``); ``True`` would render as a box
    with nothing chosen.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if name in PHONE_FIELDS:
        return phone_display(str(value))
    if name in MONEY_FIELDS or name in PERCENT_FIELDS:
        # Text that is not a number at all reaches here on the way back to a rejected form,
        # and the box gets it back as it was typed: the model's complaint is about what the
        # person wrote, and re-rendering it as something else would answer a different one.
        number = _decimal(str(value))
        if number is None:
            return str(value)
        return money_display(number) if name in MONEY_FIELDS else pct_display(number)
    return str(getattr(value, "value", value))


def masked(values: Mapping[str, Any]) -> dict[str, str]:
    """A form's values as the strings it renders."""
    return {name: mask_one(name, value) for name, value in values.items()}


def unmask_one(name: str, text: str) -> str:
    """One submitted box as the string the model parses."""
    if name in MONEY_FIELDS:
        return money_parse(text)
    if name in PERCENT_FIELDS:
        return pct_parse(text)
    if name in PHONE_FIELDS:
        return phone_digits(text) or ""
    return text


def unmasked(submitted: Mapping[str, str]) -> dict[str, str]:
    """A submitted form with every masked box read back into what it stores.

    Blanks are dropped on the way in (``api/forms.py``), so a value here is something a
    person typed. A box whose text is not a number at all comes through unchanged, and the
    model on the far side is what complains about it - by name, which is what a person
    reading the page needs.
    """
    return {name: unmask_one(name, text) for name, text in submitted.items()}


def holding_costs_amount(
    pct: Decimal | None, purchase_price: Decimal | None, rehab_costs: Decimal | None
) -> Decimal | None:
    """What a holding-cost percentage comes to in dollars.  # SPEC §8.1

    None until the percentage, the price and the rehab are all three known, because a
    percentage of nothing is not a dollar figure - it is zero pretending to be one.
    """
    if pct is None or purchase_price is None or rehab_costs is None:
        return None
    cost = purchase_price + rehab_costs
    if cost <= ZERO:
        return None
    return cost * pct


def config_defaults(config: Config) -> dict[str, Decimal | None]:
    """The four defaulted §8.1 economics, as numbers.  # SPEC §8.1

    Four flat config values now that holding costs are a percentage: none of them depends on
    anything else on the deal, so a blank new-deal form pre-fills all four.
    """
    fees = config.fees
    return {
        "contingency_pct": fees.contingency_default_pct,
        "closing_costs_usd": fees.closing_costs_default_usd,
        "holding_costs_pct_of_cost": fees.holding_costs_default_pct_of_cost,
        "origination_fee_pct": fees.origination_default_pct,
    }


def default_text(defaults: Mapping[str, Decimal | None]) -> dict[str, str]:
    """The same four as the masked strings their boxes are pre-filled with."""
    return {name: mask_one(name, value) for name, value in defaults.items()}


def drop_defaults(
    submitted: Mapping[str, str], defaults: Mapping[str, Decimal | None]
) -> dict[str, str]:
    """Remove a submitted value that is exactly its config default.

    The comparison is numeric, not textual: ``2%`` unmasks to ``0.02`` and the default is
    ``Decimal("0.02")``, and ``$1,000`` and ``1000.00`` are the same thousand dollars. Run
    after ``unmasked``, so both sides are already the stored form.
    """
    out = dict(submitted)
    for name in DEFAULTED_FIELDS:
        default = defaults.get(name)
        text = out.get(name)
        if default is None or text is None:
            continue
        value = _decimal(text)
        if value is not None and value == default:
            del out[name]
    return out
