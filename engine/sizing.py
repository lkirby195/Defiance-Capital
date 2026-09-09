"""Implied leverage and the product-specific commitment split.  # SPEC §7.4, §8.2

Pure: ``Decimal`` in, ``Decimal`` out, every threshold from ``Config``, no I/O.
The screen (SPEC §7.4) and the underwrite (SPEC §8.2, with verified values) call the
same ``size_deal``; the only difference is what the caller puts in ``SizingInputs``.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config, LeverageCaps
from schema.models import (
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
SPLIT_PRODUCTS = frozenset({Product.SPLIT_DRAW, Product.SPLIT_PRINCIPAL})


def rehab_adjusted(rehab_budget: Decimal, config: Config) -> Decimal:
    """rehab_adj = rehab_budget x (1 + contingency_pct).  # SPEC §7.4"""
    return rehab_budget * (ONE + config.fees.contingency_pct)


def estimated_closing(purchase_price: Decimal, config: Config) -> Decimal:
    """est_closing = purchase_price x est_closing_pct_of_price.  # SPEC §7.4"""
    return purchase_price * config.fees.est_closing_pct_of_price


def total_cost(purchase_price: Decimal, rehab_budget: Decimal, config: Config) -> Decimal:
    """total_cost = purchase_price + rehab_adj + est_closing.  # SPEC §7.4"""
    return (
        purchase_price
        + rehab_adjusted(rehab_budget, config)
        + estimated_closing(purchase_price, config)
    )


def commitment_split(
    inputs: SizingInputs, rehab_adj: Decimal
) -> tuple[Decimal, CommitmentSplit | None]:
    """Commitment and purchase/rehab split per product.  # SPEC §8.2

    NO_DRAW, WHOLETAIL: commitment = loan_requested; no split.
    SPLIT_DRAW: commitment = loan_requested; purchase portion defaults to
        commitment - rehab_adj (floored at zero), team can override;
        holdback = min(rehab_adj, commitment - purchase_portion).
    SPLIT_PRINCIPAL: Principal Note = purchase portion (same default and override as
        SPLIT_DRAW); Tranche A = min(rehab_adj, loan_requested - purchase portion);
        commitment = Principal Note + Tranche A, which equals loan_requested unless the
        override leaves part of the request unallocated.
    """
    if inputs.product not in SPLIT_PRODUCTS:
        return inputs.loan_requested, None
    override = inputs.purchase_portion_override
    purchase = override if override is not None else max(ZERO, inputs.loan_requested - rehab_adj)
    rehab = min(rehab_adj, inputs.loan_requested - purchase)
    commitment = inputs.loan_requested if inputs.product is Product.SPLIT_DRAW else purchase + rehab
    split = CommitmentSplit(
        purchase_portion=purchase,
        rehab_portion=rehab,
        purchase_portion_overridden=override is not None,
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
    LTARV  = commitment / arv, or NOT_AVAILABLE when the ARV is unavailable

    ``commitment`` equals ``loan_requested`` for every product unless a SPLIT_PRINCIPAL
    override leaves part of the request unallocated (see ``commitment_split``). The screen
    turns the fallback and the missing ARV into flags (SPEC §7.4, §7.5).
    """
    rehab_adj = rehab_adjusted(inputs.rehab_budget, config)
    closing = estimated_closing(inputs.purchase_price, config)
    cost = inputs.purchase_price + rehab_adj + closing
    commitment, split = commitment_split(inputs, rehab_adj)
    caps = caps_for(config, inputs.product, tranche, tier)
    band = config.screen.tolerance_band

    if inputs.as_is_value is not None:
        ltv_basis, ltv_denominator = ValueBasis.AS_IS_VALUE, inputs.as_is_value
    else:
        ltv_basis, ltv_denominator = ValueBasis.PURCHASE_PRICE, inputs.purchase_price
    ltarv = ratio(commitment, inputs.arv) if inputs.arv is not None else None

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
        rehab_adj=rehab_adj,
        est_closing=closing,
        total_cost=cost,
        commitment=commitment,
        funded_at_close=funded_at_close(inputs.product, commitment, split),
        split=split,
        ltv_basis=ltv_basis,
        metrics=metrics,
        all_pass=all(check.passed for check in metrics.values()),
    )
