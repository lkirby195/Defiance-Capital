"""IRR by loan amount and rate: every cell is a full ledger re-run.  # SPEC §8.9

The table answers the two questions a lender asks of a priced deal before quoting it - what
if we lend less, and what if we charge more - without anyone editing the deal and running
it again by hand. Rows are the loan amount, the request on the bottom row and
``sensitivity.loan_steps`` rows above it each ``sensitivity.loan_step_usd`` less; columns
are the annual rate from ``sensitivity.rate_min`` to ``sensitivity.rate_max`` in steps of
``sensitivity.rate_step``. Every cell re-sizes the deal and lays the whole ledger out again
(``engine/calc/ledger.py``) at that amount and that rate, and reports the XIRR; nothing is
interpolated, so a cell is exactly what the deal page would show had the deal been entered
that way.

**A reduction comes off the advance at closing.** The rehab portion is what the work costs
and lending less does not make the work cheaper, so on a split product the smaller loan
advances less at close and holds back the same; only once the advance is gone does the
rehab portion give, dollar for dollar. A single-note product has no split and simply
lends less. A row whose reduced amount would be nothing at all - or whose commitment sizes
to nothing - is left out rather than priced.

The deal's own rate is always a column, inserted in rate order when it falls between two
grid rates and not duplicated when it lands on one, so the deal's own cell - the request, at
its actual rate - is always on the grid and marked: a reader finds the number they already
know and reads outward from it.

Pure: ``UnderwriteInputs``, the caps cell and a ``Config`` in, a ``SensitivityTable`` out.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.ledger import return_overview
from engine.calc.terms import loan_terms
from engine.sizing import size_deal
from schema.models import (
    ExperienceTier,
    SensitivityCell,
    SensitivityRow,
    SensitivityTable,
    SizingInputs,
    Tranche,
    UnderwriteInputs,
)

ZERO = Decimal(0)


def rate_columns(config: Config, deal_rate: Decimal | None = None) -> list[Decimal]:
    """The grid's rates, lowest first, with the deal's own rate among them.  # SPEC §8.9

    Each grid column is ``rate_min + n x rate_step`` rather than a running sum, so the last
    column is exactly the number the yaml names and not that number plus a rounding remainder.
    ``deal_rate`` is inserted in rate order when it is not already a column; a rate that lands
    on a grid rate is not written twice.
    """
    settings = config.sensitivity
    columns: list[Decimal] = []
    step = 0
    while (rate := settings.rate_min + settings.rate_step * step) <= settings.rate_max:
        columns.append(rate)
        step += 1
    if deal_rate is not None and deal_rate not in columns:
        columns.append(deal_rate)
        columns.sort()
    return columns


def reductions(config: Config) -> list[Decimal]:
    """How far each row sits below the request, largest first and ending at zero.  # SPEC §8.9"""
    settings = config.sensitivity
    return [settings.loan_step_usd * Decimal(n) for n in range(settings.loan_steps, -1, -1)]


def reduced_split(
    purchase_portion: Decimal, rehab_portion: Decimal, reduction: Decimal
) -> tuple[Decimal, Decimal]:
    """Take ``reduction`` off the advance first; the rehab portion gives only once it is gone.

    # SPEC §8.9. Lending $20,000 less on a loan that advances $15,000 at close advances
    nothing at close and holds back $5,000 less for the rehab.
    """
    off_advance = min(purchase_portion, reduction)
    off_rehab = reduction - off_advance
    return purchase_portion - off_advance, rehab_portion - off_rehab


def reduced_deal(deal: SizingInputs, reduction: Decimal) -> SizingInputs | None:
    """The deal lending ``reduction`` less, or None when that leaves no loan at all."""
    amount = deal.loan_requested - reduction
    if amount <= ZERO:
        return None
    changes: dict[str, Decimal] = {"loan_requested": amount}
    split = deal.loan_split
    if split is not None:
        purchase, rehab = reduced_split(*split, reduction)
        if rehab < ZERO:  # pragma: no cover - the amount check above already rules this out
            return None
        changes["loan_purchase_portion"] = purchase
        changes["loan_rehab_portion"] = rehab
    return deal.model_copy(update=changes)


def sensitivity_table(
    inputs: UnderwriteInputs, tranche: Tranche, tier: ExperienceTier, config: Config
) -> SensitivityTable:
    """The whole grid: one row per loan amount, one cell per rate, each a ledger re-run.

    # SPEC §8.9. The caps cell is the one the underwrite scored the deal in, so every cell
    sizes against the same caps the deal did; the sizing is done once per row, because it
    does not depend on the rate, and the ledger once per cell, because it does.
    """
    rates = rate_columns(config, inputs.interest_rate)
    rows: list[SensitivityRow] = []
    for reduction in reductions(config):
        deal = reduced_deal(inputs.deal, reduction)
        if deal is None:
            continue
        sizing = size_deal(deal, tranche, tier, config)
        if sizing.commitment <= ZERO:
            continue
        cells: list[SensitivityCell] = []
        for rate in rates:
            variant = inputs.model_copy(update={"deal": deal, "interest_rate": rate})
            overview = return_overview(loan_terms(sizing, variant, config))
            cells.append(
                SensitivityCell(
                    interest_rate=rate,
                    irr=overview.irr,
                    is_deal=reduction == ZERO and rate == inputs.interest_rate,
                )
            )
        rows.append(
            SensitivityRow(
                loan_amount=deal.loan_requested,
                reduction=reduction,
                loan_purchase_portion=deal.loan_purchase_portion,
                loan_rehab_portion=deal.loan_rehab_portion,
                cells=cells,
            )
        )
    return SensitivityTable(
        loan_requested=inputs.deal.loan_requested,
        interest_rate=inputs.interest_rate,
        rates=rates,
        rows=rows,
    )
