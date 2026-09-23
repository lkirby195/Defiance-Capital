"""The lender's dated monthly cash flows, and their XIRR.  # SPEC §8.3

One row per month from closing (month 0) to payoff (month ``term_months``), every month in
between included even when nothing happens in it - so the table is dense, the dates are
contiguous, and the workbook's ``XIRR`` formula can run over one unbroken range.

Signs are the lender's. Money out is negative (``funding``, ``draws``); money in is positive
(``interest``, ``fees``, ``payoff``). ``net`` is their sum and is the column the IRR runs on.

    month 0                 -funded_at_close, +origination close half
    months 1..rehab_months  -draw
    months 1..term_months   +interest on the balance at the START of the month
    month term_months       +outstanding principal, +origination payoff half

The balance rule is where the products differ (SPEC §3). ``NO_DRAW``, ``WHOLETAIL`` and
``SPLIT_DRAW`` pay on the full commitment from close - on ``SPLIT_DRAW`` that is deliberately
more than has been advanced, which is the product. ``SPLIT_PRINCIPAL`` pays on the Principal
Note plus whatever of Tranche A has actually been drawn, so a draw taken in month *k* first
earns interest in month *k + 1*.

The draws are straight-line, in whole cents, and the last one is the remainder rather than
another copy of the quotient. A rehab portion that does not divide evenly - $55,000 over six
months - would otherwise leave the schedule a fraction of a cent away from the portion it is
meant to fund, the payoff row would stop being exactly the commitment, and the ``net`` column
would not sum to ``total_interest + total_fees``. Cents are the right unit here rather than a
display rounding: a draw is money that actually moves, and a lender wires a number of cents.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from engine.calc.irr import xirr
from engine.calc.terms import LoanTerms
from schema.dates import add_months
from schema.models import LedgerEntry, Product, ReturnOverview

ZERO = Decimal(0)
CENTS = Decimal("0.01")

# The products whose interest accrues on the full commitment from close, drawn or not.
FULL_COMMITMENT_INTEREST: frozenset[Product] = frozenset(
    {Product.NO_DRAW, Product.WHOLETAIL, Product.SPLIT_DRAW}
)


def draw_schedule(loan: LoanTerms) -> dict[int, Decimal]:
    """Month -> amount drawn in it, straight-line over the rehab period.  # SPEC §8.3

    Empty when there is no rehab money, or no rehab period to draw it over - in which case
    the rehab portion is advanced at close instead (``LoanTerms.funded_at_close``).
    """
    if not loan.drawn_over_time:
        return {}
    months = loan.rehab_months
    each = (loan.rehab_portion / Decimal(months)).quantize(CENTS, rounding=ROUND_HALF_UP)
    schedule = {month: each for month in range(1, months + 1)}
    schedule[months] = loan.rehab_portion - each * Decimal(months - 1)
    return schedule


def drawn_by(schedule: dict[int, Decimal], month: int) -> Decimal:
    """Everything drawn on or before ``month``; the balance a later month accrues on."""
    return sum((amount for at, amount in schedule.items() if at <= month), ZERO)


def interest_balance(loan: LoanTerms, schedule: dict[int, Decimal], month: int) -> Decimal:
    """The balance month ``month`` accrues interest on: the one standing at its start.

    # SPEC §8.3. A draw dated month *k* is money that went out at the end of month *k*, so
    it is in the balance from month *k + 1* onwards and not before.

    The SPLIT_PRINCIPAL balance starts from ``funded_at_close`` rather than from the
    Principal Note, and the two are the same number whenever there is a rehab period to draw
    over. Where there is not, the rehab money went out at close (SPEC §8.3) - and money that
    is out is money that accrues, so Tranche A earns from month 1 like the note beside it.
    """
    if loan.product in FULL_COMMITMENT_INTEREST:
        return loan.commitment
    return loan.funded_at_close + drawn_by(schedule, month - 1)


def build_ledger(loan: LoanTerms) -> list[LedgerEntry]:
    """One row per month from closing to payoff.  # SPEC §8.3"""
    schedule = draw_schedule(loan)
    entries: list[LedgerEntry] = []
    for month in range(0, loan.term_months + 1):
        funding = -loan.funded_at_close if month == 0 else ZERO
        draws = -schedule.get(month, ZERO)
        interest = loan.monthly_interest(interest_balance(loan, schedule, month)) if month else ZERO
        fees = ZERO
        if month == 0:
            fees += loan.origination_at_close
        if month == loan.term_months:
            fees += loan.origination_at_payoff
        payoff = (
            loan.funded_at_close + drawn_by(schedule, month) if month == loan.term_months else ZERO
        )
        entries.append(
            LedgerEntry(
                month=month,
                date=add_months(loan.closing_date, month),
                funding=funding,
                draws=draws,
                interest=interest,
                fees=fees,
                payoff=payoff,
                net=funding + draws + interest + fees + payoff,
            )
        )
    return entries


def return_overview(loan: LoanTerms) -> ReturnOverview:
    """The ledger, its totals, and the XIRR of its ``net`` column.  # SPEC §8.3"""
    entries = build_ledger(loan)
    total = {
        name: sum((getattr(entry, name) for entry in entries), ZERO)
        for name in ("funding", "draws", "interest", "fees", "payoff", "net")
    }
    return ReturnOverview(
        entries=entries,
        total_funding=total["funding"],
        total_draws=total["draws"],
        total_interest=total["interest"],
        total_fees=total["fees"],
        total_payoff=total["payoff"],
        total_profit=total["net"],
        irr=xirr([(entry.date, entry.net) for entry in entries]),
    )


def cash_flows(entries: list[LedgerEntry]) -> list[tuple[date, Decimal]]:
    """The ledger as the dated pairs ``engine.calc.irr`` takes."""
    return [(entry.date, entry.net) for entry in entries]
