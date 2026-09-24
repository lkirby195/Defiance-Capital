"""What a person types where the model stores a number.  # SPEC §4.1, §8.1

``schema/labels.py`` is this module's opposite number: that one turns a stored *code* into
words, this one turns a stored *number* into the text a box shows and back again. Three
conventions, and each is symmetric - what ``*_display`` writes, ``*_parse`` reads:

    phone       stored as digits, shown and typed as ``###-###-####``
    money       stored as ``Decimal``, shown and typed as ``$1,234`` (cents only when there
                are any)
    percent     stored as a fraction, shown and typed as ``12%`` - a person types 12 and the
                deal carries 0.12

The percent convention is the one worth stating twice, because the same number means two
different things either side of it. Every rate in ``engine/`` is a fraction of one
(``interest_rate`` is ``Decimal("0.12")``), and every rate a person reads or types is
percent. The conversion happens here and nowhere else, at the form boundary
(``api/masks.py``): the models, the database and the engine never see 12.

``*_parse`` returns a string rather than a ``Decimal`` because its caller is a form dict of
strings on its way into Pydantic, which does the coercion and owns the complaint when the
text is not a number at all. A parse that cannot make sense of its input returns it
unchanged for exactly that reason - "not a number" is the model's message to write, not this
module's.

Pure text and ``Decimal``: no config, no I/O, so the form, the deal page, the CLI and the
workbook all mask a number the same way.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NON_DIGIT = re.compile(r"\D+")
_MONEY_NOISE = re.compile(r"[$,\s]")
_PCT_NOISE = re.compile(r"[%\s]")

NANP_DIGITS = 10
HUNDRED = Decimal(100)


def digits(text: str | None) -> str:
    """Every digit in the text, in order; everything else dropped."""
    if text is None:
        return ""
    return _NON_DIGIT.sub("", text)


def phone_digits(text: str | None) -> str | None:
    """A phone as the digits it is stored as: ``(555) 123-4567`` -> ``5551234567``.

    NANP numbers only, with or without the country code. Anything else - an extension, an
    international number, a note somebody typed in the box - is returned stripped and
    unchanged, because guessing at its shape would be worse than storing what was said.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    only = digits(stripped)
    if len(only) == NANP_DIGITS:
        return only
    if len(only) == NANP_DIGITS + 1 and only.startswith("1"):
        return only[1:]
    return stripped


def phone_display(value: str | None) -> str:
    """``5551234567`` -> ``555-123-4567``; anything that is not ten digits is left alone."""
    if value is None:
        return ""
    only = digits(value)
    if len(only) != NANP_DIGITS:
        return value
    return f"{only[:3]}-{only[3:6]}-{only[6:]}"


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money_display(value: Decimal | int | None) -> str:
    """``1234`` -> ``$1,234``; ``1234.56`` -> ``$1,234.56``. Empty for no number.

    Cents appear only when there are any: a purchase price is a round number nine times out
    of ten, and ``$425,000.00`` in a box a person is about to edit is four characters of
    noise.
    """
    if value is None:
        return ""
    amount = Decimal(value)
    if amount == amount.to_integral_value():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def money_parse(text: str | None) -> str:
    """``$1,234.56`` -> ``1234.56``. The dollar sign, the commas and the spaces come off."""
    if text is None:
        return ""
    return _MONEY_NOISE.sub("", text)


def pct_display(value: Decimal | None) -> str:
    """``0.12`` -> ``12%``; ``0.125`` -> ``12.5%``. Empty for no number.

    Trailing zeros go: the stored ``0.12000`` a NUMERIC(7,5) column hands back is 12%, and
    ``12.000%`` in a box invites somebody to think the extra digits mean something.
    """
    if value is None:
        return ""
    percent = (value * HUNDRED).normalize()
    if percent == percent.to_integral_value():
        percent = percent.to_integral_value()
    return f"{percent:f}%"


def pct_parse(text: str | None) -> str:
    """``12%`` -> ``0.12``: what a person typed as a percent, as the fraction it is stored as.

    The text is returned unchanged when it is not a number, so the model's own complaint is
    about what was typed rather than about something this function made of it.
    """
    if text is None:
        return ""
    stripped = _PCT_NOISE.sub("", text)
    if not stripped:
        return ""
    value = _decimal(stripped)
    if value is None:
        return text
    return f"{value / HUNDRED:f}"
