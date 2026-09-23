"""XIRR against cases whose answer is known without running it.  # SPEC §8.3

The cases below are the ones arithmetic settles rather than a solver: one dollar out and one
back a year later is the rate itself; two years later it is the square root; an exact-year
annuity is a coupon. If ``xirr`` reproduces those it is solving the right equation, and the
ledger tests can then trust it on flows nobody could invert by hand.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.calc.irr import (
    NPV_TOLERANCE,
    _bisect,
    _bracket,
    has_a_rate,
    xirr,
    xnpv,
    xnpv_derivative,
)

D = Decimal
CLOSE = Decimal("1e-9")


def close(got: Decimal | None, want: str) -> bool:
    assert got is not None
    return abs(got - D(want)) < CLOSE


def test_one_year_out_and_back_is_the_rate_itself() -> None:
    """-100 today, +110 in exactly 365 days: 10%, by definition."""
    assert close(xirr([(date(2027, 1, 1), D("-100")), (date(2028, 1, 1), D("110"))]), "0.10")


def test_two_years_is_the_square_root() -> None:
    """-100 today, +110 in exactly 730 days: (1.1)^(1/2) - 1 = 0.0488088481701515...

    Hand-computable, and the case that proves the rate is compounded rather than simple.
    """
    got = xirr([(date(2026, 1, 1), D("-100")), (date(2028, 1, 1), D("110"))])
    assert close(got, "0.04880884817015154")


def test_half_a_year_is_the_square() -> None:
    """-100 today, +110 in 365/2 days would be (1.1)^2 - 1 = 21%; 2027 has 365 days."""
    got = xirr([(date(2027, 1, 1), D("-100")), (date(2027, 7, 2), D("110"))])
    # 1 Jan to 2 Jul 2027 is 182 days, a hair under half a year, so the rate is a hair over.
    assert got is not None
    assert D("0.2100") < got < D("0.2110")


def test_a_bullet_loan_at_a_flat_coupon_returns_the_coupon() -> None:
    """-1000 out, +1000 back in a year, +100 of interest with it: 10%, no compounding to do."""
    assert close(
        xirr([(date(2027, 1, 1), D("-1000")), (date(2028, 1, 1), D("1100"))]),
        "0.10",
    )


def test_a_loss_solves_to_a_negative_rate() -> None:
    """-100 out, +90 back a year later: -10%."""
    assert close(xirr([(date(2027, 1, 1), D("-100")), (date(2028, 1, 1), D("90"))]), "-0.10")


def test_the_solved_rate_zeroes_the_npv() -> None:
    """The property the whole thing is defined by, on a ledger-shaped set of flows."""
    flows = [
        (date(2027, 1, 1), D("-148500")),
        *[(date(2027, month, 1), D("1500")) for month in range(2, 13)],
        (date(2028, 1, 1), D("153000")),
    ]
    rate = xirr(flows)
    assert rate is not None
    assert abs(xnpv(rate, flows)) < NPV_TOLERANCE


def test_flows_out_of_order_are_sorted_before_solving() -> None:
    forwards = [(date(2027, 1, 1), D("-100")), (date(2028, 1, 1), D("110"))]
    assert xirr(forwards) == xirr(list(reversed(forwards)))


def test_no_sign_change_has_no_rate() -> None:
    """A ledger that only ever pays out, or only ever takes in, has no break-even rate.

    None rather than zero: a rate that does not exist is not a rate of zero (SPEC §8.3).
    """
    assert not has_a_rate([(date(2027, 1, 1), D("100")), (date(2028, 1, 1), D("110"))])
    assert xirr([(date(2027, 1, 1), D("100")), (date(2028, 1, 1), D("110"))]) is None
    assert xirr([(date(2027, 1, 1), D("-100")), (date(2028, 1, 1), D("-110"))]) is None
    assert xirr([(date(2027, 1, 1), D("-100"))]) is None


def test_xnpv_refuses_a_rate_outside_its_domain() -> None:
    flows = [(date(2027, 1, 1), D("-100")), (date(2028, 1, 1), D("110"))]
    assert xnpv(D(0), flows) == D(10)
    with pytest.raises(ValueError, match="above -1"):
        xnpv(D(-1), flows)
    with pytest.raises(ValueError, match="at least one cash flow"):
        xnpv(D(0), [])


def test_the_derivative_matches_a_numeric_slope() -> None:
    """Newton's step is only as good as its slope, so check it against a difference quotient."""
    flows = [
        (date(2027, 1, 1), D("-100")),
        (date(2028, 1, 1), D("60")),
        (date(2029, 1, 1), D("60")),
    ]
    rate, step = D("0.12"), D("0.000001")
    numeric = (xnpv(rate + step, flows) - xnpv(rate - step, flows)) / (2 * step)
    assert abs(xnpv_derivative(rate, flows) - numeric) < D("0.0001")


def test_bisection_alone_finds_the_same_rate_newton_does() -> None:
    """The fallback is not a different answer, only a slower route to the same one."""
    flows = [(date(2027, 1, 1), D("-148500")), (date(2028, 1, 1), D("171000"))]
    bracket = _bracket(flows)
    assert bracket is not None
    bisected = _bisect(flows, *bracket)
    newton = xirr(flows)
    assert newton is not None
    assert abs(bisected - newton) < D("1e-9")
