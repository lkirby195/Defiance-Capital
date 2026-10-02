"""Calendar-month arithmetic for the term and the payoff date, anchored to month ends.

# SPEC §8.1, §8.3

Closing is treated as the last day of its month and every period is a month end, so there
are three things to agree about: what the anchor for month ``m`` is, how many of them fit
inside a payoff date, and how many days are left over. Every case below is one a person could
check on a wall calendar.
"""

from __future__ import annotations

from datetime import date

import pytest

from schema.dates import (
    Term,
    TermDatesError,
    add_months,
    anchor_date,
    describe,
    full_months_between,
    month_end,
    payoff_date_for,
    split_term,
    term_from_dates,
)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2027, 1, 1), date(2027, 1, 31)),
        (date(2027, 1, 31), date(2027, 1, 31)),
        (date(2027, 2, 10), date(2027, 2, 28)),
        (date(2028, 2, 10), date(2028, 2, 29)),  # 2028 is a leap year
        (date(2027, 4, 30), date(2027, 4, 30)),
    ],
)
def test_month_end_is_the_last_day_of_that_month(day: date, expected: date) -> None:
    assert month_end(day) == expected


@pytest.mark.parametrize(
    ("closing", "months", "expected"),
    [
        # Month 0 is the end of the closing month, whatever day the deal closed on.
        (date(2026, 10, 1), 0, date(2026, 10, 31)),
        (date(2026, 10, 15), 0, date(2026, 10, 31)),
        (date(2026, 10, 31), 0, date(2026, 10, 31)),
        # ...and month m is the end of the month m later: the same answer from the 1st, the
        # 15th and the 31st.
        (date(2026, 10, 1), 9, date(2027, 7, 31)),
        (date(2026, 10, 15), 9, date(2027, 7, 31)),
        (date(2026, 10, 31), 9, date(2027, 7, 31)),
        (date(2027, 1, 31), 1, date(2027, 2, 28)),
        (date(2027, 3, 31), 3, date(2027, 6, 30)),
        (date(2026, 11, 15), 12, date(2027, 11, 30)),
        (date(2027, 5, 1), 18, date(2028, 11, 30)),
    ],
)
def test_the_anchor_is_the_month_end_m_months_after_the_closing_month(
    closing: date, months: int, expected: date
) -> None:
    assert anchor_date(closing, months) == expected


def test_anchors_do_not_drift_the_way_day_of_month_clamping_did() -> None:
    """31 Jan + 1 month clamped to 28 Feb, and 28 Feb + 1 month to 28 Mar; a month end stays
    a month end, so the ninth anchor is the same whichever way it is reached."""
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2027, 2, 28), 1) == date(2027, 3, 28)
    assert anchor_date(date(2027, 1, 31), 2) == date(2027, 3, 31)
    assert anchor_date(anchor_date(date(2027, 1, 31), 1), 1) == date(2027, 3, 31)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2027, 1, 1), date(2027, 2, 28), Term(1, 0)),
        (date(2027, 1, 15), date(2027, 2, 28), Term(1, 0)),
        (date(2027, 1, 1), date(2028, 1, 31), Term(12, 0)),
        (date(2027, 3, 31), date(2027, 9, 30), Term(6, 0)),
        # Between two anchors: the anchors that fit, and the days after the last one.
        (date(2027, 1, 1), date(2027, 4, 15), Term(2, 15)),  # anchors 31 Jan, 28 Feb, 31 Mar
        (date(2027, 3, 15), date(2028, 1, 11), Term(9, 11)),
        # Inside the first period, after the closing month's end: all stub.
        (date(2027, 1, 1), date(2027, 2, 15), Term(0, 15)),
        (date(2027, 1, 20), date(2027, 2, 1), Term(0, 1)),
    ],
)
def test_split_term_is_whole_month_end_anchors_then_the_days_left_over(
    start: date, end: date, expected: Term
) -> None:
    assert split_term(start, end) == expected


def test_a_payoff_on_or_before_the_closing_date_is_not_a_term() -> None:
    with pytest.raises(TermDatesError, match="on or before"):
        split_term(date(2027, 1, 1), date(2027, 1, 1))
    with pytest.raises(TermDatesError, match="on or before"):
        split_term(date(2027, 2, 1), date(2027, 1, 1))


def test_a_payoff_inside_the_closing_month_is_refused_because_month_zero_is_its_end() -> None:
    """The ledger's first row is the end of the closing month; nothing pays off before it."""
    with pytest.raises(TermDatesError, match="end of the closing month"):
        split_term(date(2027, 1, 10), date(2027, 1, 20))
    with pytest.raises(TermDatesError, match="2027-01-31"):
        split_term(date(2027, 1, 10), date(2027, 1, 31))


def test_full_months_between_counts_anchors_and_refuses_a_backwards_range() -> None:
    assert full_months_between(date(2027, 1, 1), date(2027, 4, 15)) == 2
    assert full_months_between(date(2027, 1, 1), date(2027, 2, 15)) == 0
    with pytest.raises(TermDatesError, match="runs forwards"):
        full_months_between(date(2027, 1, 1), date(2027, 1, 1))


@pytest.mark.parametrize(
    ("months", "stub", "expected"),
    [
        (9, 0, date(2027, 10, 31)),
        (9, 11, date(2027, 11, 11)),
        (0, 20, date(2027, 2, 20)),
    ],
)
def test_payoff_date_for_is_the_month_end_anchor_plus_the_stub(
    months: int, stub: int, expected: date
) -> None:
    assert payoff_date_for(date(2027, 1, 1), months, stub) == expected


def test_the_payoff_date_is_the_same_whichever_day_of_the_month_the_deal_closes() -> None:
    """SPEC §8.1: a nine-month term closing 15 October pays off 31 July."""
    for day in (1, 15, 31):
        assert payoff_date_for(date(2026, 10, day), 9) == date(2027, 7, 31)


def test_payoff_date_for_refuses_a_term_of_no_time_at_all() -> None:
    with pytest.raises(ValueError, match="no months and no days"):
        payoff_date_for(date(2027, 1, 1), 0)
    with pytest.raises(ValueError, match="cannot be negative"):
        payoff_date_for(date(2027, 1, 1), -1)


def test_a_payoff_date_and_the_term_it_implies_round_trip() -> None:
    """The one identity the ledger rests on: the term reproduces the date it came from."""
    closing = date(2027, 3, 15)
    for payoff in (date(2028, 1, 11), date(2027, 4, 14), date(2027, 4, 1), date(2028, 3, 31)):
        term = split_term(closing, payoff)
        assert payoff_date_for(closing, term.full_months, term.stub_days) == payoff


# --- term_from_dates: the engine's arithmetic for a future actual-payoff entry (SPEC §8.1) ------


def test_a_term_with_no_payoff_date_is_itself() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, None) == Term(9, 0)
    assert term_from_dates(None, 9, None) == Term(9, 0)
    assert term_from_dates(date(2027, 1, 1), None, None) is None


def test_a_payoff_date_derives_the_term() -> None:
    assert term_from_dates(date(2027, 1, 1), None, date(2027, 10, 31)) == Term(9, 0)
    assert term_from_dates(date(2027, 3, 31), None, date(2027, 9, 30)) == Term(6, 0)


def test_a_mid_month_payoff_date_is_a_term_with_a_stub_rather_than_a_refusal() -> None:
    assert term_from_dates(date(2027, 1, 1), None, date(2027, 4, 15)) == Term(2, 15)
    assert term_from_dates(date(2027, 3, 15), None, date(2028, 1, 11)) == Term(9, 11)


def test_a_payoff_date_agreeing_with_the_term_is_accepted() -> None:
    assert term_from_dates(date(2027, 1, 1), 9, date(2027, 10, 31)) == Term(9, 0)


def test_a_payoff_date_needs_a_closing_date_to_count_from() -> None:
    with pytest.raises(TermDatesError, match="needs a closing date"):
        term_from_dates(None, None, date(2027, 10, 1))


def test_a_term_and_a_payoff_date_that_disagree_are_refused() -> None:
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), 6, date(2027, 10, 31))
    message = str(caught.value)
    assert "disagree" in message
    assert "6 month(s)" in message and "2027-07-31" in message and "9 month(s)" in message
    assert "Clear whichever one you did not mean" in message


def test_a_term_in_months_beside_a_mid_month_payoff_date_is_refused() -> None:
    with pytest.raises(TermDatesError) as caught:
        term_from_dates(date(2027, 1, 1), 3, date(2027, 4, 15))
    message = str(caught.value)
    assert "disagree" in message
    assert "2 month(s) and 15 day(s)" in message


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
