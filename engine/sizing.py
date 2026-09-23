"""Implied leverage and the product-specific commitment split.  # SPEC §7.4, §8.2

Pure: ``Decimal`` in, ``Decimal`` out, every threshold from ``Config``, no I/O.
The screen (SPEC §7.4) and the underwrite (SPEC §8.2, with verified values) call the
same ``size_deal``; the only difference is what the caller puts in ``SizingInputs``.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config, LeverageCaps
from schema.models import (
    SPLIT_PRODUCTS,
    CapStatus,
    CommitmentSplit,
    ExperienceTier,
    LeverageMetric,
    MetricCheck,
    Product,
    SizingInputs,
    SizingResult,
    Tranche,
    ValueBasis,
)

ZERO = Decimal(0)
ONE = Decimal(1)


def contingency_pct(inputs: SizingInputs, config: Config) -> Decimal:
    """The contingency in force: the team's own, else the config default.  # SPEC §8.1"""
    if inputs.contingency_pct is not None:
        return inputs.contingency_pct
    return config.fees.contingency_default_pct


def closing_costs(inputs: SizingInputs, config: Config) -> Decimal:
    """The lender's closing costs in force: the team's own, else the config default.

    # SPEC §8.1, §8.2. It is a dollar amount, not a percentage of the price: the 3%-of-price
    borrower closing assumption the LTC denominator used to carry is gone.
    """
    if inputs.closing_costs_usd is not None:
        return inputs.closing_costs_usd
    return config.fees.closing_costs_default_usd


def rehab_adjusted(rehab_costs: Decimal, pct: Decimal) -> Decimal:
    """rehab_adj = rehab_costs x (1 + contingency_pct).  # SPEC §7.4, §8.2"""
    return rehab_costs * (ONE + pct)


def total_cost(purchase_price: Decimal, rehab_adj: Decimal, closing: Decimal) -> Decimal:
    """total_cost = purchase_price + rehab_adj + closing_costs_usd.  # SPEC §7.4, §8.2"""
    return purchase_price + rehab_adj + closing


def commitment_split(
    inputs: SizingInputs, rehab_adj: Decimal
) -> tuple[Decimal, CommitmentSplit | None]:
    """Commitment and purchase/rehab split per product.  # SPEC §8.2

    NO_DRAW, WHOLETAIL: commitment = loan_requested; no split.
    SPLIT_DRAW: purchase portion and holdback as the team entered them (they add up to
        loan_requested), so commitment = loan_requested and the holdback is the rehab
        portion.
    SPLIT_PRINCIPAL: Principal Note = purchase portion, Tranche A = rehab portion;
        commitment = the two added back together.

    The rehab side is capped at ``rehab_adj`` either way: the lender does not hold back more
    than the contingency-adjusted rehab budget could ever draw. On SPLIT_PRINCIPAL the cap
    lowers the commitment, which is what COMMITMENT_BELOW_REQUEST reports (SPEC §7.5); on
    SPLIT_DRAW it does not, because there is one note - the money above the cap is advanced
    at close instead of held back, so only the timing moves.

    A split product with no split entered is sized on the loan requested with no split at
    all. That is a borrower-channel intake before anybody has divided it (SPEC §4.2): the
    screen still runs, and the underwrite refuses until the team enters it (SPEC §8.1).
    """
    entered = inputs.loan_split
    if inputs.product not in SPLIT_PRODUCTS or entered is None:
        return inputs.loan_requested, None
    purchase, requested_rehab = entered
    rehab = min(rehab_adj, requested_rehab)
    commitment = inputs.loan_requested if inputs.product is Product.SPLIT_DRAW else purchase + rehab
    split = CommitmentSplit(
        purchase_portion=purchase,
        rehab_portion=rehab,
        rehab_portion_requested=requested_rehab,
        rehab_portion_capped=rehab < requested_rehab,
    )
    return commitment, split


def funded_at_close(
    product: Product, commitment: Decimal, split: CommitmentSplit | None
) -> Decimal:
    """Dollars advanced at close: the commitment less the holdback / Tranche A.  # SPEC §3, §8.2"""
    if split is None:
        return commitment
    if product is Product.SPLIT_DRAW:
        return commitment - split.rehab_portion
    return split.purchase_portion  # SPLIT_PRINCIPAL: the Principal Note


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """numerator / denominator with a positive denominator; not rounded (output boundary rounds)."""
    if denominator <= ZERO:
        raise ValueError(f"leverage denominator must be positive, got {denominator}")
    return numerator / denominator


def cap_status(actual: Decimal | None, cap: Decimal, tolerance_band: Decimal) -> CapStatus:
    """PASS at or under the cap; WITHIN_TOLERANCE up to cap + band; FAIL beyond.  # SPEC §7.5"""
    if actual is None:
        return CapStatus.NOT_AVAILABLE
    if actual <= cap:
        return CapStatus.PASS
    if actual <= cap + tolerance_band:
        return CapStatus.WITHIN_TOLERANCE
    return CapStatus.FAIL


def check_metric(
    metric: LeverageMetric,
    actual: Decimal | None,
    cap: Decimal,
    tolerance_band: Decimal,
    basis: ValueBasis | None = None,
) -> MetricCheck:
    """One metric against its cap: actual, cap, pass/fail, tolerance-band status.  # SPEC §8.2"""
    status = cap_status(actual, cap, tolerance_band)
    return MetricCheck(
        metric=metric,
        actual=actual,
        cap=cap,
        tolerance_band=tolerance_band,
        status=status,
        passed=status is CapStatus.PASS,
        basis=basis,
    )


def caps_for(
    config: Config, product: Product, tranche: Tranche, tier: ExperienceTier
) -> LeverageCaps:
    """The caps cell for product x credit tranche x experience tier.  # SPEC §7.4"""
    return config.leverage_caps[product][tranche][tier]


def size_deal(
    inputs: SizingInputs, tranche: Tranche, tier: ExperienceTier, config: Config
) -> SizingResult:
    """Implied leverage against the caps cell, plus the commitment split.  # SPEC §7.4, §8.2

    LTC    = commitment / total_cost
    LTV    = commitment / as_is_value, or / purchase_price with basis PURCHASE_PRICE when the
             as-is value is unavailable
    LTARV  = commitment / estimated_sale_price, or NOT_AVAILABLE when there is no price

    Where each valuation came from (``as_is_value_source`` / ``estimated_sale_price_source``)
    travels through onto the result, so a stored screen says whether the numbers it sized on
    were pulled or entered by hand.

    ``commitment`` equals ``loan_requested`` for every product unless a SPLIT_PRINCIPAL rehab
    portion is capped at ``rehab_adj`` and leaves part of the request unallocated (see
    ``commitment_split``). The screen turns the fallback and the missing estimated sale
    price into flags (SPEC §7.4, §7.5).
    """
    pct = contingency_pct(inputs, config)
    rehab_adj = rehab_adjusted(inputs.rehab_costs, pct)
    closing = closing_costs(inputs, config)
    cost = total_cost(inputs.purchase_price, rehab_adj, closing)
    commitment, split = commitment_split(inputs, rehab_adj)
    caps = caps_for(config, inputs.product, tranche, tier)
    band = config.screen.tolerance_band

    if inputs.as_is_value is not None:
        ltv_basis, ltv_denominator = ValueBasis.AS_IS_VALUE, inputs.as_is_value
    else:
        ltv_basis, ltv_denominator = ValueBasis.PURCHASE_PRICE, inputs.purchase_price
    sale_price = inputs.estimated_sale_price
    ltarv = ratio(commitment, sale_price) if sale_price is not None else None

    metrics = {
        LeverageMetric.LTC: check_metric(
            LeverageMetric.LTC, ratio(commitment, cost), caps.ltc, band
        ),
        LeverageMetric.LTV_AS_IS: check_metric(
            LeverageMetric.LTV_AS_IS,
            ratio(commitment, ltv_denominator),
            caps.ltv_as_is,
            band,
            basis=ltv_basis,
        ),
        LeverageMetric.LTARV: check_metric(LeverageMetric.LTARV, ltarv, caps.ltarv, band),
    }
    return SizingResult(
        product=inputs.product,
        credit_tranche=tranche,
        experience_tier=tier,
        purchase_price=inputs.purchase_price,
        rehab_costs=inputs.rehab_costs,
        contingency_pct=pct,
        rehab_adj=rehab_adj,
        closing_costs=closing,
        total_cost=cost,
        loan_requested=inputs.loan_requested,
        commitment=commitment,
        funded_at_close=funded_at_close(inputs.product, commitment, split),
        split=split,
        ltv_basis=ltv_basis,
        as_is_value_source=inputs.as_is_value_source,
        estimated_sale_price_source=inputs.estimated_sale_price_source,
        metrics=metrics,
        all_pass=all(check.passed for check in metrics.values()),
    )
