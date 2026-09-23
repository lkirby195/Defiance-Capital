"""XIRR on dated cash flows: Newton, with bisection when Newton will not do.  # SPEC §8.3

The lender's ledger (``engine.calc.ledger``) is a list of dated flows, and its IRR is the
rate that discounts them to zero on the actual dates:

    xnpv(r) = SUM  amount_i / (1 + r) ^ ((date_i - date_0) / 365)

That is Excel's ``XIRR`` convention exactly - actual days over a 365-day year, annualized
and compounded - and it is the convention on purpose: the workbook the export writes carries
an ``XIRR`` formula over the same dates and the same ``net`` column, so a reader can see the
engine's number and the spreadsheet's agree rather than taking the engine's word for it.

Everything here is ``Decimal``. ``Decimal.__pow__`` handles the fractional exponent, which is
what makes an exact-to-the-cent ledger produce a rate that does not drift with float noise.

**Newton, then bisection.** Newton converges in a handful of steps on a well-behaved ledger,
which every real one is: money out at close, money in every month after. It is not
guaranteed to converge on every input, so a step that lands outside the domain (``r <= -1``,
where the discount factors are undefined), a derivative at zero, or a run that uses up its
iterations all fall back to bisection over a bracket found by scanning. Bisection is slower
and always converges once a sign change is bracketed, which makes it the right thing to fail
into rather than the right thing to start with.

``None`` comes back when there is no rate at all: a ledger with no negative flow, or one with
no positive flow, has no break-even discount rate, and reporting one of zero would be
inventing a number. A positive amount funded at close makes that unreachable in practice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)

# Excel's XIRR day-count: actual days over a 365-day year, leap years included.
DAYS_PER_YEAR = Decimal(365)

# How close to zero xnpv has to land, in dollars. The ledger's flows are dollars, so this is
# a ten-thousandth of a cent on a deal of any size GLENWOOD writes - far below anything a
# lender would notice, and far above the last place of a Decimal carrying 28 digits.
NPV_TOLERANCE = Decimal("1e-7")

# How close two rates have to be for bisection to stop. A hundred-millionth of a percentage
# point; the workbook is compared against the engine at 1e-6, so this is two orders of
# magnitude tighter than the claim it supports.
RATE_TOLERANCE = Decimal("1e-10")

NEWTON_ITERATIONS = 60
BISECTION_ITERATIONS = 200

# The domain is r > -1: at -1 the discount factor is a division by zero, and below it the
# base is negative and a fractional power is not real. The scan starts just inside it.
RATE_FLOOR = Decimal("-0.9999")
RATE_CEILING = Decimal(1000)
INITIAL_GUESS = Decimal("0.1")


def _years(start: date, when: date) -> Decimal:
    """Actual days from ``start``, over 365. Excel's XIRR day-count."""
    return Decimal((when - start).days) / DAYS_PER_YEAR


def xnpv(rate: Decimal, flows: list[tuple[date, Decimal]]) -> Decimal:
    """The net present value of ``flows`` at ``rate``, discounted from the first date."""
    if not flows:
        raise ValueError("xnpv needs at least one cash flow")
    if rate <= -ONE:
        raise ValueError(f"rate must be above -1 to discount anything, got {rate}")
    start = flows[0][0]
    base = ONE + rate
    return sum(
        (amount / base ** _years(start, when) for when, amount in flows),
        ZERO,
    )


def xnpv_derivative(rate: Decimal, flows: list[tuple[date, Decimal]]) -> Decimal:
    """d(xnpv)/d(rate) = SUM -t_i x amount_i x (1 + r) ^ (-t_i - 1); Newton's slope."""
    start = flows[0][0]
    base = ONE + rate
    return sum(
        (
            -_years(start, when) * amount / base ** (_years(start, when) + ONE)
            for when, amount in flows
        ),
        ZERO,
    )


def has_a_rate(flows: list[tuple[date, Decimal]]) -> bool:
    """True when the flows change sign, which is what makes a break-even rate exist."""
    return any(amount > ZERO for _, amount in flows) and any(amount < ZERO for _, amount in flows)


def _bracket(flows: list[tuple[date, Decimal]]) -> tuple[Decimal, Decimal] | None:
    """A (low, high) pair whose xnpv signs differ, by scanning outward from the floor.

    The scan is coarse on purpose: bisection narrows it, and a bracket found cheaply beats a
    bracket found precisely. ``None`` means no sign change was found anywhere in the domain,
    which ``has_a_rate`` has already ruled out for every ledger with flows both ways.
    """
    probes = [RATE_FLOOR, Decimal("-0.9"), Decimal("-0.5"), ZERO, ONE, Decimal(10), RATE_CEILING]
    previous: tuple[Decimal, Decimal] | None = None
    for rate in probes:
        value = xnpv(rate, flows)
        if value == ZERO:
            return rate, rate
        if previous is not None and (previous[1] > ZERO) != (value > ZERO):
            return previous[0], rate
        previous = (rate, value)
    return None


def _bisect(flows: list[tuple[date, Decimal]], low: Decimal, high: Decimal) -> Decimal:
    """The rate in [low, high] where xnpv crosses zero. The bracket is checked by the caller."""
    low_value = xnpv(low, flows)
    for _ in range(BISECTION_ITERATIONS):
        middle = (low + high) / 2
        value = xnpv(middle, flows)
        if value == ZERO or high - low < RATE_TOLERANCE:
            return middle
        if (value > ZERO) == (low_value > ZERO):
            low, low_value = middle, value
        else:
            high = middle
    return (low + high) / 2


def xirr(flows: list[tuple[date, Decimal]]) -> Decimal | None:
    """The annualized, compounded rate that discounts ``flows`` to zero on their own dates.

    ``None`` when no such rate exists: the flows have to go both ways for there to be one.
    """
    if len(flows) < 2 or not has_a_rate(flows):
        return None
    ordered = sorted(flows, key=lambda flow: flow[0])
    rate = INITIAL_GUESS
    for _ in range(NEWTON_ITERATIONS):
        value = xnpv(rate, ordered)
        if abs(value) < NPV_TOLERANCE:
            return rate
        slope = xnpv_derivative(rate, ordered)
        if slope == ZERO:
            break
        step = rate - value / slope
        if step <= RATE_FLOOR or step > RATE_CEILING:
            break
        if abs(step - rate) < RATE_TOLERANCE:
            return step
        rate = step
    bracket = _bracket(ordered)
    if bracket is None:  # pragma: no cover - has_a_rate has already ruled this out
        return None
    return _bisect(ordered, *bracket)
