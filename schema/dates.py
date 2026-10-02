"""Calendar-month arithmetic for the loan term, anchored to month ends.  # SPEC §8.1, §8.3

**Closing is the last day of its month.** A deal closes on whatever date the team enters,
and the model treats that closing as the last day of that month: month 0 of the ledger is
dated the month end, and every later period is the last day of a later month. A term of
nine months on a deal closing 15 October pays off on 31 July, not on 15 July - the payoff
date is the last day of the month that is the closing month plus the term.

That makes the anchors regular in a way day-of-month anchoring was not: 31 January plus one
month was 28 February and 28 February plus one month was 28 March, so the anchor drifted
and clamping was not reversible. A month-end anchor is the same answer whichever way it is
reached, and ``anchor_date(closing, m)`` is the single place it is worked out.

A term is a whole number of monthly periods plus, when the payoff date falls between two of
them, a **stub**: the days left over. ``Term`` is that pair, and it is what the ledger lays
out - one row per full period, then one short row ending on the payoff date. No form
produces a stub today - the team enters the term in months (SPEC §8.1) - and the engine
keeps the capability for the actual-payoff entry that will one day say when a loan really
paid off; ``split_term`` and ``term_from_dates`` are the pure arithmetic for it.

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


def month_end(day: date) -> date:
    """The last day of the month ``day`` falls in."""
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months, clamped to the end of a short month."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def anchor_date(closing_date: date, months: int) -> date:
    """The last day of the month that is the closing month plus ``months``.  # SPEC §8.3

    Month 0 is the end of the closing month itself, whatever day of it the deal closed on;
    month ``m`` is the end of the month ``m`` later. Every ledger row but a stub row sits on
    one of these.
    """
    return month_end(add_months(closing_date, months))


def full_months_between(start: date, end: date) -> int:
    """How many whole month-end anchors from ``start`` fall on or before ``end``.

    The anchors are month ends (``anchor_date``), so this is the largest month count whose
    anchor is on or before ``end``. The first anchor is the end of the closing month, and a
    date on or before it is not a term at all: the loan would pay off before the ledger's
    month 0.
    """
    if end <= start:
        raise TermDatesError(
            f"{end.isoformat()} is not after {start.isoformat()}; a term runs forwards"
        )
    first = anchor_date(start, 0)
    if end <= first:
        raise TermDatesError(
            f"{end.isoformat()} is not after the end of the closing month "
            f"({first.isoformat()}); the ledger's month 0 is that month end, so a term "
            "runs from there"
        )
    months = max(0, (end.year - start.year) * 12 + (end.month - start.month))
    while months > 0 and anchor_date(start, months) > end:
        months -= 1
    while anchor_date(start, months + 1) <= end:
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
    return Term(full, (payoff_date - anchor_date(closing_date, full)).days)


def payoff_date_for(closing_date: date, term_months: int, stub_days: int = WHOLE_MONTHS) -> date:
    """The last row of the ledger: the term's month-end anchor, plus the stub days.

    # SPEC §8.1, §8.3. The last day of the month that is the closing month plus the term;
    a stub, when there is one, is counted on from there.
    """
    if term_months < 0 or stub_days < 0:
        raise ValueError(f"a term cannot be negative, got {term_months} months {stub_days} days")
    if term_months == 0 and stub_days == 0:
        raise ValueError("a term of no months and no days is not a term")
    return anchor_date(closing_date, term_months) + timedelta(days=stub_days)


def term_from_dates(
    closing_date: date | None, term_months: int | None, payoff_date: date | None
) -> Term | None:
    """The term, deriving it from a payoff date when that is what was given.  # SPEC §8.1

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
        implied = (
            payoff_date_for(closing_date, term_months)
            if term_months > 0
            else anchor_date(closing_date, 0)
        )
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
