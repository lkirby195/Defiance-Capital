"""Calendar-month arithmetic for the loan term.  # SPEC §8.1, §8.3

A term is a whole number of monthly periods plus, when the payoff date falls between two of
them, a **stub**: the days left over. ``Term`` is that pair, and it is what the ledger lays
out - one row per full period, then one short row ending on the payoff date the team typed.

"Monthly period" means the same day of a later month, anchored to the closing date's
day-of-month and clamped to the end of a short one: 31 January plus one month is 28 February,
not 3 March. Clamping is not reversible - 28 February plus one month is 28 March, not 31
March - so the two directions are written separately rather than one being assumed to undo
the other, and the stub is measured from the anchor rather than by subtracting dates.

The team enters either the term or the payoff date and the other derives (SPEC §8.1).
Entering a term still means a whole number of months, and its payoff date is the anchor with
no stub. Entering a payoff date allows any date after closing: the term is the anchors that
fit inside it, and whatever is left is the stub. What is still refused is a payoff date with
no closing date to count from, a payoff date on or before the closing date, and a term and a
payoff date that disagree - which is a person having edited one box and left the other behind.

Pure date arithmetic, no config and no clock, so the schema, the intake form, the queue's
override block and the engine all measure a term the same way.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import NamedTuple


class TermDatesError(ValueError):
    """The closing date, the term and the payoff date cannot all be true at once."""


class Term(NamedTuple):
    """A loan term as the ledger lays it out: whole monthly periods, then a stub.

    ``full_months`` can be 0 - a loan that pays off inside its first month is all stub - and
    ``stub_days`` is 0 on a term that lands exactly on an anchor, which is every term entered
    as a number of months.
    """

    full_months: int
    stub_days: int

    @property
    def has_stub(self) -> bool:
        return self.stub_days > 0

    @property
    def is_positive(self) -> bool:
        """A term that runs for some time. A payoff on the closing date is not one."""
        return self.full_months > 0 or self.stub_days > 0


WHOLE_MONTHS = 0  # the stub of a term entered as a number of months


def add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months, clamped to the end of a short month."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def full_months_between(start: date, end: date) -> int:
    """How many whole anchored monthly periods fit between ``start`` and ``end``.

    Not a subtraction: ``add_months`` clamps, so the answer is the largest month count whose
    anchor is on or before ``end``. 0 when ``end`` falls inside the first month, which is a
    loan that is all stub.
    """
    if end <= start:
        raise TermDatesError(
            f"{end.isoformat()} is not after {start.isoformat()}; a term runs forwards"
        )
    months = max(0, (end.year - start.year) * 12 + (end.month - start.month))
    while months > 0 and add_months(start, months) > end:
        months -= 1
    while add_months(start, months + 1) <= end:
        months += 1
    return months


def split_term(closing_date: date, payoff_date: date) -> Term:
    """The payoff date as whole monthly periods plus the days left over.  # SPEC §8.1"""
    if payoff_date <= closing_date:
        raise TermDatesError(
            f"payoff date {payoff_date.isoformat()} is on or before the closing date "
            f"{closing_date.isoformat()}; a term runs forwards"
        )
    full = full_months_between(closing_date, payoff_date)
    return Term(full, (payoff_date - add_months(closing_date, full)).days)


def payoff_date_for(closing_date: date, term_months: int, stub_days: int = WHOLE_MONTHS) -> date:
    """The last row of the ledger: the term's anchor, plus the stub days.  # SPEC §8.1, §8.3"""
    if term_months < 0 or stub_days < 0:
        raise ValueError(f"a term cannot be negative, got {term_months} months {stub_days} days")
    if term_months == 0 and stub_days == 0:
        raise ValueError("a term of no months and no days is not a term")
    return add_months(closing_date, term_months) + timedelta(days=stub_days)


def term_from_dates(
    closing_date: date | None, term_months: int | None, payoff_date: date | None
) -> Term | None:
    """The term, deriving it from a payoff date when that is what was entered.  # SPEC §8.1

    ``None`` back means nobody has said how long the loan runs, which a partial intake is
    entitled to. Everything that cannot be true at once raises ``TermDatesError`` with the
    dates in the message.
    """
    if payoff_date is None:
        return None if term_months is None else Term(term_months, WHOLE_MONTHS)
    if closing_date is None:
        raise TermDatesError(
            f"payoff date {payoff_date.isoformat()} needs a closing date to count the "
            "months from; enter one, or enter the term in months instead"
        )
    term = split_term(closing_date, payoff_date)
    if term_months is not None and term != Term(term_months, WHOLE_MONTHS):
        implied = payoff_date_for(closing_date, term_months) if term_months > 0 else closing_date
        raise TermDatesError(
            f"the term of {term_months} month(s) and the payoff date "
            f"{payoff_date.isoformat()} disagree: {term_months} month(s) after closing "
            f"{closing_date.isoformat()} is {implied.isoformat()}, and the date entered is "
            f"{describe(term)} after it. Clear whichever one you did not mean"
        )
    return term


def describe(term: Term) -> str:
    """A term as a person reads it: ``9 months``, ``9 months and 11 days``, ``11 days``."""
    months = f"{term.full_months} month(s)" if term.full_months else ""
    days = f"{term.stub_days} day(s)" if term.stub_days else ""
    return " and ".join(part for part in (months, days) if part) or "no time at all"
