"""Lender Sensitivity - IRR by loan amount and rate: every cell is a full ledger re-run.

# SPEC §8.9

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

The columns are the config grid and nothing else, the same on every deal, so two deals'
tables read side by side. The deal's own cell - the request, at its actual rate - is marked
when that rate is one of the columns; a deal priced off the grid has no marked cell, and its
own IRR is the Return Overview's. The default rate (``interest.default_annual_rate``) sits
on the grid, so a deal nobody has re-priced is marked.

Pure: ``UnderwriteInputs``, the caps cell and a ``Config`` in, a ``SensitivityTable`` out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from engine.calc.ledger import return_overview
from engine.calc.terms import LoanTerms, loan_terms
from engine.sizing import size_deal
from schema.models import (
    ExperienceTier,
    ReturnOverview,
    SensitivityCell,
    SensitivityRow,
    SensitivityTable,
    SizingInputs,
    SizingResult,
    Tranche,
    UnderwriteInputs,
)

ZERO = Decimal(0)


def rate_columns(config: Config) -> list[Decimal]:
    """The grid's rates, lowest first.  # SPEC §8.9

    Each column is ``rate_min + n x rate_step`` rather than a running sum, so the last column
    is exactly the number the yaml names and not that number plus a rounding remainder.
    """
    settings = config.sensitivity
    columns: list[Decimal] = []
    step = 0
    while (rate := settings.rate_min + settings.rate_step * step) <= settings.rate_max:
        columns.append(rate)
        step += 1
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


def sized(
    inputs: UnderwriteInputs,
    reduction: Decimal,
    tranche: Tranche,
    tier: ExperienceTier,
    config: Config,
) -> tuple[SizingInputs, SizingResult] | None:
    """The deal lending ``reduction`` less, sized in the caps cell; None when nothing is lent.

    # SPEC §8.9. The sizing does not depend on the rate, so a row is sized once and every
    rate column on it re-ledgers that one sizing.
    """
    deal = reduced_deal(inputs.deal, reduction)
    if deal is None:
        return None
    sizing = size_deal(deal, tranche, tier, config)
    if sizing.commitment <= ZERO:
        return None
    return deal, sizing


class Point(NamedTuple):
    """One loan amount at one rate: the inputs as re-run, the loan, and its ledger."""

    inputs: UnderwriteInputs
    loan: LoanTerms
    overview: ReturnOverview


def run_point(
    inputs: UnderwriteInputs,
    deal: SizingInputs,
    sizing: SizingResult,
    rate: Decimal,
    config: Config,
) -> Point:
    """The whole ledger laid out again at ``deal``'s loan amount and ``rate``.  # SPEC §8.9"""
    variant = inputs.model_copy(update={"deal": deal, "interest_rate": rate})
    loan = loan_terms(sizing, variant, config)
    return Point(variant, loan, return_overview(loan))


class Grid:
    """Every (loan amount, rate) point the three tables read, each laid out once.

    # SPEC §8.9, §8.10. The Lender Sensitivity, the Borrower Sensitivity and the pricing
    matrix all read the same ledgers - the borrower's cell is the lender's cell seen from the
    other side - so one underwrite builds one ``Grid`` and hands it to all three. A memo
    local to one call, not state: the same inputs always fill it with the same points, and a
    table built without one builds its own.
    """

    def __init__(
        self, inputs: UnderwriteInputs, tranche: Tranche, tier: ExperienceTier, config: Config
    ) -> None:
        self.inputs = inputs
        self.tranche = tranche
        self.tier = tier
        self.config = config
        self._rows: dict[Decimal, tuple[SizingInputs, SizingResult] | None] = {}
        self._points: dict[tuple[Decimal, Decimal], Point] = {}

    def row(self, reduction: Decimal) -> tuple[SizingInputs, SizingResult] | None:
        """The deal ``reduction`` less, sized; None when nothing is lent (``sized``)."""
        if reduction not in self._rows:
            self._rows[reduction] = sized(
                self.inputs, reduction, self.tranche, self.tier, self.config
            )
        return self._rows[reduction]

    def point(self, reduction: Decimal, rate: Decimal) -> Point | None:
        """The ledger at ``reduction`` less and ``rate``; None when the row is left out."""
        key = (reduction, rate)
        if key not in self._points:
            row = self.row(reduction)
            if row is None:
                return None
            self._points[key] = run_point(self.inputs, *row, rate, self.config)
        return self._points[key]


def sensitivity_table(
    inputs: UnderwriteInputs,
    tranche: Tranche,
    tier: ExperienceTier,
    config: Config,
    grid: Grid | None = None,
) -> SensitivityTable:
    """The whole grid: one row per loan amount, one cell per rate, each a ledger re-run.

    # SPEC §8.9. The caps cell is the one the underwrite scored the deal in, so every cell
    sizes against the same caps the deal did; the sizing is done once per row, because it
    does not depend on the rate, and the ledger once per cell, because it does.
    """
    grid = grid or Grid(inputs, tranche, tier, config)
    rates = rate_columns(config)
    rows: list[SensitivityRow] = []
    for reduction in reductions(config):
        row = grid.row(reduction)
        if row is None:
            continue
        deal, _ = row
        cells: list[SensitivityCell] = []
        for rate in rates:
            point = grid.point(reduction, rate)
            assert point is not None  # the row was sized, so every rate on it is priced
            cells.append(
                SensitivityCell(
                    interest_rate=rate,
                    irr=point.overview.irr,
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
