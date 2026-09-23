"""Calendar-month arithmetic for the loan term.  # SPEC §8.1, §8.3

The ledger's rows are months, so the term is a whole number of them and the payoff date is
``closing_date`` plus that many calendar months. "Calendar months" means the same day of a
later month, clamped to the end of a short one: 31 January plus one month is 28 February, not
3 March. Clamping is not reversible - 28 February plus one month is 28 March, not 31 March -
so the two directions are written separately rather than one being assumed to undo the other.

The team enters either the term or the payoff date and the other derives (SPEC §8.1). A
payoff date that is not exactly a whole number of months after closing is refused by name:
there is no row in the ledger for half a month, and rounding one silently would move a date
somebody typed on purpose.

Pure date arithmetic, no config and no clock, so the schema, the intake form, the queue's
override block and the engine all measure a term the same way.
"""

from __future__ import annotations

import calendar
from datetime import date


class TermDatesError(ValueError):
    """The closing date, the term and the payoff date cannot all be true at once."""


def add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months, clamped to the end of a short month."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def months_between(start: date, end: date) -> int | None:
    """The whole calendar months from ``start`` to ``end``, or None when it is not whole.

    Not a subtraction: ``add_months`` clamps, so the answer is the month count that
    reproduces ``end`` exactly, and there is none for a date that falls mid-month.
    """
    if end <= start:
        return None
    months = (end.year - start.year) * 12 + (end.month - start.month)
    # Clamping can leave the arithmetic count one short (28 Feb -> 31 Mar reads as 1).
    for candidate in (months, months + 1):
        if candidate >= 1 and add_months(start, candidate) == end:
            return candidate
    return None


def bracketing_payoff_dates(start: date, end: date) -> tuple[date, date]:
    """The whole-month payoff dates either side of ``end``, for a message that names them."""
    months = max(1, (end.year - start.year) * 12 + (end.month - start.month))
    while months > 1 and add_months(start, months) > end:
        months -= 1
    while add_months(start, months + 1) <= end:
        months += 1
    return add_months(start, months), add_months(start, months + 1)


def payoff_date_for(closing_date: date, term_months: int) -> date:
    """``closing_date`` + ``term_months`` calendar months: the last row of the ledger."""
    if term_months < 1:
        raise ValueError(f"term_months must be at least 1, got {term_months}")
    return add_months(closing_date, term_months)


def term_from_dates(
    closing_date: date | None, term_months: int | None, payoff_date: date | None
) -> int | None:
    """The term in months, deriving it from a payoff date when that is what was entered.

    ``None`` back means nobody has said how long the loan runs, which a partial intake is
    entitled to. Everything else that cannot be true at once raises ``TermDatesError`` with
    the dates in the message: a payoff date with no closing date to count from, a payoff date
    that is not a whole number of months after closing, or a term and a payoff date that
    disagree - which is a person having edited one box and left the other one behind.
    """
    if payoff_date is None:
        return term_months
    if closing_date is None:
        raise TermDatesError(
            f"payoff date {payoff_date.isoformat()} needs a closing date to count the "
            "months from; enter one, or enter the term in months instead"
        )
    months = months_between(closing_date, payoff_date)
    if months is None:
        earlier, later = bracketing_payoff_dates(closing_date, payoff_date)
        raise TermDatesError(
            f"payoff date {payoff_date.isoformat()} is not a whole number of months after "
            f"closing {closing_date.isoformat()}; the nearest dates that are are "
            f"{earlier.isoformat()} and {later.isoformat()}"
        )
    if term_months is not None and term_months != months:
        raise TermDatesError(
            f"the term of {term_months} months and the payoff date "
            f"{payoff_date.isoformat()} disagree: that date is {months} months after "
            f"closing {closing_date.isoformat()}. Clear whichever one you did not mean"
        )
    return months
