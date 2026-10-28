"""Take-Back analysis: the lender's own downside, solved on its XIRR.  # SPEC §8.6

The question is no longer whether a rent carries what the loan cost (the DSCR test of engine
1.0 to 2.0) but what the property has to **sell** or **let** for, after a default, for the
lender to still earn ``pricing.target_lender_irr`` on the whole loan. Every figure is solved
by bisection on the lender's XIRR over one dated timeline, laid out from the ledger's own
anchors (``LoanTerms.anchor``):

    month 0                   -funded at close, +the origination fee's close half
    months 1 .. rehab_months  -the draws, as the ledger schedules them
    months 1 .. D             +interest on the commitment; D = max(1, term - listing months)
                              is the default month: the borrower stops paying when the rehab
                              period ends and the listing period would begin
    months D+1 .. D+L         nothing: L = take_back_lost_interest_months of lost interest
    month D+L                 -the legal bill (take_back_legal_costs_usd)
    months D+L+1 .. S         -the deal's monthly holding cost, one per foreclosure month;
                              S = D + L + F, F = take_back.foreclosure_months[state], or
                              the OTHER entry where the state has none (``foreclosure_months_for``)
    month S                   the lender owns the property

Then either the REO sale - ``+P x (1 - broker_selling_pct)`` at month S - or a hold: for each
``take_back.hold_months`` entry H, ``+rent x (1 - expenses_pct)`` in months S+1 .. S+H and
the sale net of the broker at month S+H. The payoff half of the origination fee is never
collected, and the stub days of a term that has them are not a period the lender gets to.
Whole months only: the draws always finish by the default month, because the rehab period
is the default month on every term longer than the listing period and there is no rehab
period on any other.

**Block A, the haircut to target** (``haircut_price``): the sale price P at which the XIRR of
the flows above equals the target, bisected over whole dollars in ``[0, 3 x projected sale
price]``. Reported as P, the haircut from the projected sale price in dollars and as a share
of it, P as a share of the purchase price, and P against the as-is value when the team gave
one. ``TAKE_BACK_PRICE_ABOVE_PURCHASE`` (severity from config, placeholder Hard) when P is
above the purchase price: the lender needs more than the house cost to come out whole.

**Block B, hold and rent** (``holds``), per hold period: (1) the monthly rent, at the projected
sale price, at which the XIRR reaches the target, bisected over ``[0, 10 x market rent]`` -
``[0, $20,000]`` with no market rent - and (2) the sale price at the market rent that does
the same, over the same price bounds as block A and ``NOT_EVALUATED`` without a market rent.

**No solution** is an answer: when the upper bound does not reach the target, or the lower
one already does, nothing inside the bounds is the answer and the solve says so rather than
reporting a bound as a price. Each solve carries its bounds. The lender's XIRR only rises
with the sale price and with the rent - both are money in, later - which is what makes the
bisection valid. The answer is defined on the XIRR itself (``engine/calc/irr.py``, the
function the Return Overview is solved with): the lowest whole dollar whose XIRR reaches the
target. ``solve`` bisects on that XIRR directly; ``solve_flows``, which the analysis uses,
asks each probe the same question more cheaply - whether the flows' NPV at the target rate
is at least zero, with the discount factors worked out once per solve because every probe
shares the dates - and then confirms the answer on the XIRR: the dollar it found clears the
target and the dollar below it does not. On a flow pattern where the two questions disagree
(the NPV crossing the target rate more than once) it discards the shortcut and bisects on the
XIRR alone, so the figure reported is the XIRR's in every case.

The analysis is ``NOT_EVALUATED`` without a projected sale price - the haircut and the
bounds are set against it - or without foreclosure months: config's entry for the deal's
state, else its ``OTHER`` entry standing in (``FORECLOSURE_MONTHS_MISSING`` says so either
way); the timeline and the costs are reported regardless.
Pure: ``UnderwriteInputs``, ``LoanTerms`` and a ``Config`` in, a ``TakeBackAnalysis`` out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from config.config import Config, TakeBackConfig
from engine.calc.irr import xirr
from engine.calc.ledger import draw_schedule
from engine.calc.terms import (
    LoanTerms,
    broker_selling_pct,
    rental_expenses_pct,
    take_back_legal_costs,
    take_back_lost_interest_months,
)
from schema.models import (
    AnalysisStatus,
    BoundHit,
    DatedFlow,
    HoldRow,
    Solve,
    SolveStatus,
    State,
    TakeBackAnalysis,
    UnderwriteInputs,
)

ZERO = Decimal(0)
ONE = Decimal(1)

# The solves' bounds (SPEC §8.6). Not a lender's tunables: they say how far the search looks
# before it reports that nothing in range reaches the target.
PRICE_BOUND_MULTIPLE = Decimal(3)  # a sale price is searched in [0, 3 x projected sale price]
RENT_BOUND_MULTIPLE = Decimal(10)  # a rent in [0, 10 x market rent] ...
RENT_BOUND_USD = Decimal(20000)  # ... or [0, $20,000] when there is no market rent
# The solves' resolution: the lowest whole dollar that clears the target, the way the pricing
# matrix names the lowest 0.01% (``engine/calc/pricing.py``).
DOLLAR = Decimal(1)

NOT_SOLVED = Solve(status=SolveStatus.NOT_EVALUATED, value=None, irr=None, lower=None, upper=None)


class Timeline(NamedTuple):
    """When the lender stops being paid and when it owns the property.  # SPEC §8.6"""

    default_month: int  # the last month of interest
    lost_months: int  # months of nothing after it
    legal_costs: Decimal  # paid when the lender starts foreclosing
    foreclosure_months: int  # carried at the monthly holding cost
    carry_monthly: Decimal
    broker_pct: Decimal  # off the sale, whenever it comes

    @property
    def legal_month(self) -> int:
        return self.default_month + self.lost_months

    @property
    def sale_month(self) -> int:
        """The month the lender owns the property and the REO sale lands in."""
        return self.legal_month + self.foreclosure_months


def default_month(term_months: int, config: Config) -> int:
    """max(1, term_months - listing_months): the borrower defaults as the listing period begins.

    # SPEC §8.6. Floored at one: a term inside the listing period still pays one month before
    it stops.
    """
    if term_months < 0:
        raise ValueError(f"term_months cannot be negative, got {term_months}")
    return max(1, term_months - config.draws.listing_months)


def lender_months(
    loan: LoanTerms,
    timeline: Timeline,
    sale_price: Decimal,
    hold_months: int = 0,
    net_rent: Decimal = ZERO,
) -> dict[int, Decimal]:
    """The lender's flows by ledger month over the whole loan, default to sale.  # SPEC §8.6

    ``net_rent`` is what each hold month collects after expenses; the sale, net of the
    broker, lands at the end of the hold - at ``timeline.sale_month`` when there is none.
    """
    by_month: dict[int, Decimal] = {}

    def add(month: int, amount: Decimal) -> None:
        by_month[month] = by_month.get(month, ZERO) + amount

    add(0, -loan.funded_at_close + loan.origination_at_close)
    schedule = draw_schedule(loan)
    if schedule and max(schedule) > timeline.default_month:  # pragma: no cover - see the module
        raise ValueError("the draw schedule runs past the default month")
    for month, amount in schedule.items():
        add(month, -amount)
    interest = loan.monthly_interest(loan.commitment)
    for month in range(1, timeline.default_month + 1):
        add(month, interest)
    add(timeline.legal_month, -timeline.legal_costs)
    for month in range(timeline.legal_month + 1, timeline.sale_month + 1):
        add(month, -timeline.carry_monthly)
    end = timeline.sale_month + hold_months
    for month in range(timeline.sale_month + 1, end + 1):
        add(month, net_rent)
    add(end, sale_price * (ONE - timeline.broker_pct))
    return dict(sorted(by_month.items()))


def lender_flows(
    loan: LoanTerms,
    timeline: Timeline,
    sale_price: Decimal,
    hold_months: int = 0,
    net_rent: Decimal = ZERO,
) -> list[tuple[date, Decimal]]:
    """``lender_months`` as the dated pairs ``engine.calc.irr`` takes."""
    months = lender_months(loan, timeline, sale_price, hold_months, net_rent)
    return [(loan.anchor(month), amount) for month, amount in months.items()]


def solve(
    irr_at: Callable[[Decimal], Decimal | None],
    lower: Decimal,
    upper: Decimal,
    target: Decimal,
) -> Solve:
    """The lowest whole dollar in ``[lower, upper]`` at which ``irr_at`` reaches ``target``.

    # SPEC §8.6. Bisection on whole dollars, holding the largest dollar known to fall short
    and the smallest known to clear until they are adjacent; ``irr_at`` has to rise with its
    argument. ``NO_SOLUTION`` when the upper bound falls short, or the lower bound already
    clears - either way the answer would be a bound, which is not an answer.
    """
    seen: dict[int, Decimal | None] = {}

    def clears(dollars: int) -> bool:
        if dollars not in seen:
            seen[dollars] = irr_at(Decimal(dollars))
        irr = seen[dollars]
        return irr is not None and irr >= target

    def no_solution(bound: BoundHit) -> Solve:
        return Solve(
            status=SolveStatus.NO_SOLUTION,
            value=None,
            irr=None,
            lower=lower,
            upper=upper,
            bound_hit=bound,
        )

    low, high = int(lower), int(upper)
    if high <= low or not clears(high):
        return no_solution(BoundHit.UPPER)
    short, clear = low - 1, high
    while clear - short > 1:
        middle = (short + clear) // 2
        if clears(middle):
            clear = middle
        else:
            short = middle
    if clear == low:
        return no_solution(BoundHit.LOWER)
    irr = seen[clear]
    assert irr is not None
    return Solve(status=SolveStatus.SOLVED, value=Decimal(clear), irr=irr, lower=lower, upper=upper)


Flows = list[tuple[date, Decimal]]


def solve_flows(
    flows_at: Callable[[Decimal], Flows],
    lower: Decimal,
    upper: Decimal,
    target: Decimal,
) -> Solve:
    """``solve`` for flows that share their dates: cheap probes, the answer confirmed on the XIRR.

    # SPEC §8.6. Every probe's flows fall on the same dates, so the discount factor of each
    at ``target`` is computed once and a probe is one dot product: the NPV at the target rate
    is at least zero exactly when the XIRR reaches the target, for flows whose NPV falls as
    the rate rises. The dollar the bisection lands on and the one below it are then put to
    the XIRR itself; if either answers differently, the shortcut was not sound for these
    flows and the solve is done again on the XIRR alone (``solve``).
    """

    def irr_at(value: Decimal) -> Decimal | None:
        return xirr(flows_at(value), target)

    sample = flows_at(lower)
    if len(sample) < 2:  # pragma: no cover - a loan always funds and sells
        return solve(irr_at, lower, upper, target)
    start = sample[0][0]
    base = ONE + target
    factors = [ONE / base ** (Decimal((when - start).days) / Decimal(365)) for when, _ in sample]

    def npv_at_target(value: Decimal) -> Decimal:
        flows = flows_at(value)
        assert [when for when, _ in flows] == [when for when, _ in sample]
        return sum(
            (amount * factor for (_, amount), factor in zip(flows, factors, strict=True)), ZERO
        )

    low, high = int(lower), int(upper)
    if high <= low:
        return solve(irr_at, lower, upper, target)
    if npv_at_target(Decimal(high)) < ZERO:
        top = irr_at(Decimal(high))
        if top is None or top < target:
            return Solve(
                status=SolveStatus.NO_SOLUTION,
                value=None,
                irr=None,
                lower=lower,
                upper=upper,
                bound_hit=BoundHit.UPPER,
            )
        return solve(irr_at, lower, upper, target)
    short, clear = low - 1, high
    while clear - short > 1:
        middle = (short + clear) // 2
        if npv_at_target(Decimal(middle)) >= ZERO:
            clear = middle
        else:
            short = middle
    at = irr_at(Decimal(clear))
    if at is None or at < target:
        return solve(irr_at, lower, upper, target)
    if clear == low:
        below = None
    else:
        below = irr_at(Decimal(clear - 1))
        if below is not None and below >= target:
            return solve(irr_at, lower, upper, target)
    if clear == low:
        return Solve(
            status=SolveStatus.NO_SOLUTION,
            value=None,
            irr=None,
            lower=lower,
            upper=upper,
            bound_hit=BoundHit.LOWER,
        )
    assert below is None or below < target
    return Solve(status=SolveStatus.SOLVED, value=Decimal(clear), irr=at, lower=lower, upper=upper)


def foreclosure_months_for(
    state: State, settings: TakeBackConfig
) -> tuple[int | None, State | None]:
    """The foreclosure months config names for ``state``, and under which key.  # SPEC §8.6

    The state's own entry when there is one; else the ``OTHER`` entry, the placeholder that
    stands in for a state outside the footprint (``FORECLOSURE_MONTHS_MISSING`` names it);
    else nothing, and the take-back is NOT_EVALUATED.
    """
    months = settings.foreclosure_months
    if state in months:
        return months[state], state
    if State.OTHER in months:
        return months[State.OTHER], State.OTHER
    return None, None


def take_back_analysis(
    inputs: UnderwriteInputs, loan: LoanTerms, config: Config
) -> TakeBackAnalysis:
    """What the property has to sell or let for after a default.  # SPEC §8.6"""
    settings = config.take_back
    lost_months = take_back_lost_interest_months(inputs, config)
    legal = take_back_legal_costs(inputs, config)
    foreclosure, foreclosure_from = foreclosure_months_for(inputs.state, settings)
    carry = loan.holding_costs_monthly
    broker = broker_selling_pct(inputs, config)
    expenses_pct = rental_expenses_pct(inputs, config)
    target = config.pricing.target_lender_irr
    interest = loan.monthly_interest(loan.commitment)
    default = default_month(loan.term_months, config)
    sale_price = inputs.deal.estimated_sale_price
    market_rent = inputs.monthly_rent
    as_is = inputs.as_is_value
    common = {
        "loan_amount": loan.commitment,
        "interest_rate": loan.interest_rate,
        "term_months": loan.term_months,
        "default_month": default,
        "interest_received": interest * Decimal(default),
        "lost_interest_months": lost_months,
        "lost_interest": interest * Decimal(lost_months),
        "legal_costs": legal,
        "state": inputs.state,
        "foreclosure_months": foreclosure,
        "foreclosure_months_from": foreclosure_from,
        "holding_costs_monthly": carry,
        "foreclosure_carry": None if foreclosure is None else carry * Decimal(foreclosure),
        "broker_selling_pct": broker,
        "expenses_pct": expenses_pct,
        "target_irr": target,
        "hold_months": list(settings.hold_months),
        "purchase_price": loan.sizing.purchase_price,
        "as_is_value": as_is,
        "projected_sale_price": sale_price,
    }
    if foreclosure is None or sale_price is None:
        sale_month = None if foreclosure is None else default + lost_months + foreclosure
        return TakeBackAnalysis(
            status=AnalysisStatus.NOT_EVALUATED,
            market_rent_status=AnalysisStatus.NOT_EVALUATED,
            sale_month=sale_month,
            haircut_price=None,
            haircut_amount=None,
            haircut_pct=None,
            price_pct_of_purchase=None,
            price_above_purchase=None,
            price_vs_as_is=None,
            price_pct_of_as_is=None,
            holds=[
                HoldRow(
                    hold_months=hold,
                    sale_month=(sale_month or 0) + hold,
                    required_rent=NOT_SOLVED,
                    required_sale_price=NOT_SOLVED,
                )
                for hold in settings.hold_months
            ],
            **common,
        )
    timeline = Timeline(default, lost_months, legal, foreclosure, carry, broker)
    price_upper = sale_price * PRICE_BOUND_MULTIPLE
    rent_upper = RENT_BOUND_USD if market_rent is None else market_rent * RENT_BOUND_MULTIPLE
    net_of_expenses = ONE - expenses_pct

    def flows_at_price(price: Decimal, hold: int = 0, net_rent: Decimal = ZERO) -> Flows:
        return lender_flows(loan, timeline, price, hold, net_rent)

    haircut = solve_flows(flows_at_price, ZERO, price_upper, target)
    holds: list[HoldRow] = []
    for hold in settings.hold_months:

        def flows_at_rent(monthly: Decimal, hold: int = hold) -> Flows:
            return flows_at_price(sale_price, hold, monthly * net_of_expenses)

        rent = solve_flows(flows_at_rent, ZERO, rent_upper, target)
        if market_rent is None:
            price = NOT_SOLVED
        else:
            net_rent = market_rent * net_of_expenses

            def flows_at_sale(
                value: Decimal, hold: int = hold, net_rent: Decimal = net_rent
            ) -> Flows:
                return flows_at_price(value, hold, net_rent)

            price = solve_flows(flows_at_sale, ZERO, price_upper, target)
        holds.append(
            HoldRow(
                hold_months=hold,
                sale_month=timeline.sale_month + hold,
                required_rent=rent,
                required_sale_price=price,
            )
        )
    solved = haircut.value
    purchase = loan.sizing.purchase_price
    flows = (
        []
        if solved is None
        else [
            DatedFlow(month=month, date=loan.anchor(month), amount=amount)
            for month, amount in lender_months(loan, timeline, solved).items()
        ]
    )
    return TakeBackAnalysis(
        status=AnalysisStatus.EVALUATED,
        market_rent_status=(
            AnalysisStatus.NOT_EVALUATED if market_rent is None else AnalysisStatus.EVALUATED
        ),
        sale_month=timeline.sale_month,
        haircut_price=haircut,
        haircut_amount=None if solved is None else sale_price - solved,
        haircut_pct=None if solved is None else (sale_price - solved) / sale_price,
        price_pct_of_purchase=None if solved is None else solved / purchase,
        price_above_purchase=None if solved is None else solved > purchase,
        price_vs_as_is=None if solved is None or as_is is None else solved - as_is,
        price_pct_of_as_is=None if solved is None or as_is is None else solved / as_is,
        haircut_flows=flows,
        holds=holds,
        **common,
    )
