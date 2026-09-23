"""Calendar-month arithmetic for the term and the payoff date.  # SPEC §8.1

The ledger's rows are months, so the two directions have to agree about what a month is -
and they cannot simply be each other's inverse, because clamping a short month loses
information. Every case below is one a person could check on a wall calendar.
"""

from __future__ import annotations

from datetime import date

import pytest

from schema.dates import (
    TermDatesError,
    add_months,
    bracketing_payoff_dates,
    months_between,
    payoff_date_for,
    term_from_dates,
)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2027, 1, 1), 0, date(2027, 1, 1)),
        (date(2027, 1, 1), 1, date(2027, 2, 1)),
        (date(2027, 1, 1), 12, date(2028, 1, 1)),
        (date(2027, 1, 1), 18, date(2028, 7, 1)),
        # A short month clamps to its own last day rather than spilling into the next.
        (date(2027, 1, 31), 1, date(2027, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # 2028 is a leap year
        (date(2027, 3, 31), 1, date(2027, 4, 30)),
        (date(2027, 3, 31), 3, date(2027, 6, 30)),
        (date(2027, 3, 31), 6, date(2027, 9, 30)),
        (date(2026, 12, 15), 12, date(2027, 12, 15)),
    ],
)
def test_add_months_is_the_same_day_of_a_later_month_clamped(
    start: date, months: int, expected: date
) -> None:
    assert add_months(start, months) == expected


def test_clamping_does_not_undo_itself() -> None:
    """31 Jan + 1 month is 28 Feb, and 28 Feb + 1 month is 28 Mar, not 31 Mar.

    Which is why ``months_between`` reproduces the date rather than subtracting: there is no
    whole number of months from 28 February to 31 March, and saying there is one would put a
    payoff row three days from where the team put it.
    """
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2027, 2, 28), 1) == date(2027, 3, 28)
    assert months_between(date(2027, 2, 28), date(2027, 3, 31)) is None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2027, 1, 1), date(2027, 2, 1), 1),
        (date(2027, 1, 1), date(2028, 1, 1), 12),
        (date(2027, 3, 31), date(2027, 9, 30), 6),
        (date(2027, 1, 31), date(2027, 2, 28), 1),
        (date(2027, 1, 1), date(2027, 1, 15), None),  # mid-month
        (date(2027, 1, 1), date(2027, 1, 1), None),  # a term of no months is not a term
        (date(2027, 2, 1), date(2027, 1, 1), None),  # backwards
    ],
)
def test_months_between_is_whole_months_or_nothing(
    start: date, end: date, expected: int | None
) -> None:
    assert months_between(start, end) == expected


def test_bracketing_dates_name_the_two_a_person_could_have_meant() -> None:
    earlier, later = bracketing_payoff_dates(date(2027, 1, 1), date(2027, 4, 15))
    assert (earlier, later) == (date(2027, 4, 1), date(2027, 5, 1))
    # A date before the first whole month still brackets from one month out.
    earlier, later = bracketing_payoff_dates(date(2027, 1, 1), date(2027, 1, 10))
    assert (earlier, later) == (date(2027, 2, 1), date(2027, 3, 1))


def test_payoff_date_for_refuses_a_term_of_no_months() -> None:
    assert payoff_date_for(date(2027, 1, 1), 9) == date(2027, 10, 1)
    with pytest.raises(ValueError, match="at least 1"):
        payoff_date_for(date(2027, 1, 1), 0)


# --- term_from_dates: enter either, the other derives (SPEC §8.1) --------------------------------


def test_a_term_with_no_payoff_date_is_itself() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, None) == 9
    assert term_from_dates(None, 9, None) == 9
    assert term_from_dates(date(2027, 1, 1), None, None) is None


def test_a_payoff_date_derives_the_term() -> None:
    assert term_from_dates(date(2027, 1, 1), None, date(2027, 10, 1)) == 9
    assert term_from_dates(date(2027, 3, 31), None, date(2027, 9, 30)) == 6


def test_a_payoff_date_agreeing_with_the_term_is_accepted() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, date(2027, 10, 1)) == 9


def test_a_payoff_date_needs_a_closing_date_to_count_from() -> None:
    with pytest.raises(TermDatesError, match="needs a closing date"):
        term_from_dates(None, None, date(2027, 10, 1))


def test_a_payoff_date_mid_month_is_refused_and_names_the_two_nearest() -> None:
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), None, date(2027, 4, 15))
    message = str(caught.value)
    assert "not a whole number of months" in message
    assert "2027-04-01" in message and "2027-05-01" in message


def test_a_term_and_a_payoff_date_that_disagree_are_refused() -> None:
    """The one case a person can create by editing one box and leaving the other behind."""
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), 6, date(2027, 10, 1))
    message = str(caught.value)
    assert "disagree" in message
    assert "6 months" in message and "9 months after" in message
    assert "Clear whichever one you did not mean" in message
