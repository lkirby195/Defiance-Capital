"""Calendar-month arithmetic for the term and the payoff date.  # SPEC §8.1

A term is whole monthly periods plus a stub, so there are three things to agree about: what a
monthly period is, how many of them fit inside a payoff date, and how many days are left over.
They cannot simply be each other's inverse, because clamping a short month loses information.
Every case below is one a person could check on a wall calendar.
"""

from __future__ import annotations

from datetime import date

import pytest

from schema.dates import (
    Term,
    TermDatesError,
    add_months,
    describe,
    full_months_between,
    payoff_date_for,
    split_term,
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

    Which is why the term is measured from the anchor rather than by subtracting dates: 31
    March is one month and three days after 28 February, not two months and not one.
    """
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2027, 2, 28), 1) == date(2027, 3, 28)
    assert split_term(date(2027, 2, 28), date(2027, 3, 31)) == Term(1, 3)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2027, 1, 1), date(2027, 2, 1), Term(1, 0)),
        (date(2027, 1, 1), date(2028, 1, 1), Term(12, 0)),
        (date(2027, 3, 31), date(2027, 9, 30), Term(6, 0)),
        (date(2027, 1, 31), date(2027, 2, 28), Term(1, 0)),
        # Mid-month: the anchors that fit, and the days after the last one.
        (date(2027, 1, 1), date(2027, 4, 15), Term(3, 14)),
        (date(2027, 3, 15), date(2027, 12, 26), Term(9, 11)),
        # Inside the first month: all stub, no whole period at all.
        (date(2027, 1, 1), date(2027, 1, 15), Term(0, 14)),
        (date(2027, 1, 1), date(2027, 1, 2), Term(0, 1)),
        # The widest stub a clamped anchor can leave: 28 Feb to 31 March is 31 days.
        (date(2027, 1, 31), date(2027, 3, 30), Term(1, 30)),
    ],
)
def test_split_term_is_whole_anchors_then_the_days_left_over(
    start: date, end: date, expected: Term
) -> None:
    assert split_term(start, end) == expected


def test_a_payoff_on_or_before_the_closing_date_is_not_a_term() -> None:
    with pytest.raises(TermDatesError, match="on or before"):
        split_term(date(2027, 1, 1), date(2027, 1, 1))
    with pytest.raises(TermDatesError, match="on or before"):
        split_term(date(2027, 2, 1), date(2027, 1, 1))


def test_full_months_between_counts_anchors_and_refuses_a_backwards_range() -> None:
    assert full_months_between(date(2027, 1, 1), date(2027, 4, 15)) == 3
    assert full_months_between(date(2027, 1, 1), date(2027, 1, 15)) == 0
    with pytest.raises(TermDatesError, match="runs forwards"):
        full_months_between(date(2027, 1, 1), date(2027, 1, 1))


@pytest.mark.parametrize(
    ("months", "stub", "expected"),
    [
        (9, 0, date(2027, 10, 1)),
        (9, 11, date(2027, 10, 12)),
        (0, 20, date(2027, 1, 21)),
    ],
)
def test_payoff_date_for_is_the_anchor_plus_the_stub(
    months: int, stub: int, expected: date
) -> None:
    assert payoff_date_for(date(2027, 1, 1), months, stub) == expected


def test_payoff_date_for_refuses_a_term_of_no_time_at_all() -> None:
    with pytest.raises(ValueError, match="no months and no days"):
        payoff_date_for(date(2027, 1, 1), 0)
    with pytest.raises(ValueError, match="cannot be negative"):
        payoff_date_for(date(2027, 1, 1), -1)


def test_a_payoff_date_and_the_term_it_implies_round_trip() -> None:
    """The one identity the ledger rests on: the term reproduces the date it came from."""
    closing = date(2027, 3, 15)
    for payoff in (date(2027, 12, 26), date(2027, 4, 14), date(2027, 3, 16), date(2028, 3, 15)):
        term = split_term(closing, payoff)
        assert payoff_date_for(closing, term.full_months, term.stub_days) == payoff


# --- term_from_dates: enter either, the other derives (SPEC §8.1) --------------------------------


def test_a_term_with_no_payoff_date_is_itself() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, None) == Term(9, 0)
    assert term_from_dates(None, 9, None) == Term(9, 0)
    assert term_from_dates(date(2027, 1, 1), None, None) is None


def test_a_payoff_date_derives_the_term() -> None:
    assert term_from_dates(date(2027, 1, 1), None, date(2027, 10, 1)) == Term(9, 0)
    assert term_from_dates(date(2027, 3, 31), None, date(2027, 9, 30)) == Term(6, 0)


def test_a_mid_month_payoff_date_is_a_term_with_a_stub_rather_than_a_refusal() -> None:
    """The whole point of SPEC §8.1 now: any date after closing is a term the ledger prices."""
    assert term_from_dates(date(2027, 1, 1), None, date(2027, 4, 15)) == Term(3, 14)
    assert term_from_dates(date(2027, 3, 15), None, date(2027, 12, 26)) == Term(9, 11)


def test_a_payoff_date_agreeing_with_the_term_is_accepted() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, date(2027, 10, 1)) == Term(9, 0)


def test_a_payoff_date_needs_a_closing_date_to_count_from() -> None:
    with pytest.raises(TermDatesError, match="needs a closing date"):
        term_from_dates(None, None, date(2027, 10, 1))


def test_a_term_and_a_payoff_date_that_disagree_are_refused() -> None:
    """The one case a person can create by editing one box and leaving the other behind."""
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), 6, date(2027, 10, 1))
    message = str(caught.value)
    assert "disagree" in message
    assert "6 month(s)" in message and "2027-07-01" in message and "9 month(s)" in message
    assert "Clear whichever one you did not mean" in message


def test_a_term_in_months_beside_a_mid_month_payoff_date_is_refused() -> None:
    """A term said in months lands on its own month; a stub means clearing the term box."""
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), 3, date(2027, 4, 15))
    message = str(caught.value)
    assert "disagree" in message
    assert "3 month(s) and 14 day(s)" in message


def test_a_payoff_date_before_the_closing_date_is_refused_by_name() -> None:
    with pytest.raises(TermDatesError, match="on or before the closing date"):
        term_from_dates(date(2027, 1, 1), None, date(2026, 12, 31))


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        (Term(9, 0), "9 month(s)"),
        (Term(9, 11), "9 month(s) and 11 day(s)"),
        (Term(0, 11), "11 day(s)"),
        (Term(0, 0), "no time at all"),
    ],
)
def test_describe_reads_as_a_person_would_say_it(term: Term, expected: str) -> None:
    assert describe(term) == expected


def test_a_term_knows_whether_it_runs_for_any_time() -> None:
    assert Term(9, 0).is_positive and not Term(9, 0).has_stub
    assert Term(0, 1).is_positive and Term(0, 1).has_stub
    assert not Term(0, 0).is_positive
