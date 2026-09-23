"""engine/sizing.py: implied leverage, caps with tolerance band, commitment split.  # SPEC §7.4"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from config.config import DEFAULT_PATH, Config, ConfigError, load_yaml
from engine.sizing import (
    cap_status,
    caps_for,
    check_metric,
    closing_costs,
    commitment_split,
    contingency_pct,
    funded_at_close,
    ratio,
    rehab_adjusted,
    size_deal,
    total_cost,
)
from schema.models import (
    SPLIT_PRODUCTS,
    CapStatus,
    ExperienceTier,
    LeverageMetric,
    Product,
    SizingInputs,
    Tranche,
    ValueBasis,
)

CONFIG = Config.load()
D = Decimal


def inputs(product: Product = Product.SPLIT_DRAW, **overrides: Any) -> SizingInputs:
    """A deal, with the loan split already entered on a split product.

    A split product with no split is a real state - a borrower-channel intake nobody has
    divided (SPEC §4.2) - and it has a test of its own below. It is not the default here,
    because almost every test in this file is about a deal somebody finished entering.
    """
    base: dict[str, Any] = {
        "product": product,
        "purchase_price": D("100000.00"),
        "rehab_costs": D("40000.00"),
        "loan_requested": D("115000.00"),
        "as_is_value": D("150000.00"),
        "estimated_sale_price": D("200000.00"),
    }
    base.update(overrides)
    if product in SPLIT_PRODUCTS and "loan_purchase_portion" not in base:
        loan = D(base["loan_requested"])
        # two places, like a real entry; hypothesis pushes cents through here
        rehab = min(loan, D(base["rehab_costs"]))
        base["loan_purchase_portion"] = loan - rehab
        base["loan_rehab_portion"] = rehab
    return SizingInputs(**base)


def split_inputs(product: Product, purchase: str, rehab: str) -> SizingInputs:
    """A deal whose loan split is the pair given, whatever that pair says."""
    return inputs(product, loan_purchase_portion=D(purchase), loan_rehab_portion=D(rehab))


# --- §7.4 building blocks ----------------------------------------------------------------------


def test_rehab_adjusted_applies_the_contingency_in_force() -> None:
    """The contingency is a SPEC §8.1 input now, and its config default is 0.00."""
    assert CONFIG.fees.contingency_default_pct == D("0.00")
    assert contingency_pct(inputs(), CONFIG) == D("0.00")
    assert contingency_pct(inputs(contingency_pct=D("0.10")), CONFIG) == D("0.10")
    assert rehab_adjusted(D("40000.00"), D("0.10")) == D("44000.0000")
    assert rehab_adjusted(D("40000.00"), D("0.00")) == D("40000.00")
    assert rehab_adjusted(D("0"), D("0.10")) == 0


def test_closing_costs_is_a_dollar_amount_from_config_or_the_deal() -> None:
    # SPEC §8.1, §8.2: the lender's own closing costs, not a percentage of the price
    assert CONFIG.fees.closing_costs_default_usd == D("1000.00")
    assert closing_costs(inputs(), CONFIG) == D("1000.00")
    assert closing_costs(inputs(closing_costs_usd=D("1500.00")), CONFIG) == D("1500.00")


def test_total_cost_is_price_plus_rehab_adj_plus_closing() -> None:
    assert total_cost(D("100000.00"), D("44000.00"), D("1000.00")) == D("145000.00")


def test_thresholds_come_from_config_not_code() -> None:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    data["fees"]["contingency_default_pct"] = D("0.20")
    data["fees"]["closing_costs_default_usd"] = D("2500.00")
    cfg = Config.from_dict(data)
    assert contingency_pct(inputs(), cfg) == D("0.20")
    assert closing_costs(inputs(), cfg) == D("2500.00")


def test_config_rejects_the_retired_buy_side_closing_key() -> None:
    """The 3%-of-price borrower closing assumption is gone: the lender's own dollar amount
    is the LTC denominator now, and a stale yaml is refused rather than half-read
    (SPEC §8.1, §10)."""
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    data["fees"]["borrower_closing_pct_of_price"] = D("0.03")
    with pytest.raises(ConfigError, match="borrower_closing_pct_of_price"):
        Config.from_dict(data)


def test_ratio_is_exact_decimal_and_rejects_nonpositive_denominator() -> None:
    assert ratio(D("3"), D("4")) == D("0.75")
    assert isinstance(ratio(D("1"), D("3")), Decimal)
    with pytest.raises(ValueError, match="positive"):
        ratio(D("1"), D("0"))


# --- §7.5 tolerance band -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "actual, expected",
    [
        (D("0.60"), CapStatus.PASS),
        (D("0.75"), CapStatus.PASS),  # at the cap
        (D("0.7500001"), CapStatus.WITHIN_TOLERANCE),
        (D("0.80"), CapStatus.WITHIN_TOLERANCE),  # exactly cap + band
        (D("0.8000001"), CapStatus.FAIL),
        (D("1.20"), CapStatus.FAIL),
        (None, CapStatus.NOT_AVAILABLE),
    ],
)
def test_cap_status_boundaries(actual: Decimal | None, expected: CapStatus) -> None:
    assert cap_status(actual, D("0.75"), D("0.05")) is expected


def test_check_metric_carries_cap_actual_band_and_pass_flag() -> None:
    check = check_metric(LeverageMetric.LTC, D("0.82"), D("0.80"), D("0.05"))
    assert check.metric is LeverageMetric.LTC
    assert check.actual == D("0.82") and check.cap == D("0.80")
    assert check.tolerance_band == D("0.05")
    assert check.status is CapStatus.WITHIN_TOLERANCE
    assert check.passed is False
    assert check.basis is None
    assert check_metric(LeverageMetric.LTC, D("0.80"), D("0.80"), D("0.05")).passed is True


def test_caps_for_reads_the_grid_cell() -> None:
    cell = caps_for(CONFIG, Product.WHOLETAIL, Tranche.T3, ExperienceTier.E1)
    assert cell == CONFIG.leverage_caps[Product.WHOLETAIL][Tranche.T3][ExperienceTier.E1]


# --- §8.2 commitment split ---------------------------------------------------------------------


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL])
def test_single_note_products_have_no_split(product: Product) -> None:
    commitment, split = commitment_split(inputs(product), D("40000"))
    assert commitment == D("115000.00")
    assert split is None
    assert funded_at_close(product, commitment, split) == commitment


def test_split_draw_holds_back_the_entered_rehab_portion() -> None:
    commitment, split = commitment_split(inputs(Product.SPLIT_DRAW), D("40000"))
    assert commitment == D("115000.00")  # SPLIT_DRAW commitment is the loan requested
    assert split is not None
    assert split.purchase_portion == D("75000.00")
    assert split.rehab_portion == D("40000")  # the holdback, as entered
    assert split.rehab_portion_capped is False
    assert funded_at_close(Product.SPLIT_DRAW, commitment, split) == D("75000.00")


def test_a_split_draw_rehab_portion_above_rehab_adj_is_advanced_at_close() -> None:
    """One note, so the cap moves money from the holdback to close, not off the loan."""
    commitment, split = commitment_split(
        split_inputs(Product.SPLIT_DRAW, "55000.00", "60000.00"), D("40000")
    )
    assert commitment == D("115000.00")
    assert split is not None
    assert split.rehab_portion == D("40000")  # capped at rehab_adj
    assert split.rehab_portion_requested == D("60000.00")
    assert split.rehab_portion_capped is True
    assert funded_at_close(Product.SPLIT_DRAW, commitment, split) == D("75000.00")


def test_split_principal_notes_are_the_two_entered_portions() -> None:
    commitment, split = commitment_split(inputs(Product.SPLIT_PRINCIPAL), D("40000"))
    assert split is not None
    assert split.purchase_portion == D("75000.00")  # Principal Note
    assert split.rehab_portion == D("40000")  # Tranche A
    assert commitment == split.purchase_portion + split.rehab_portion == D("115000.00")
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("75000.00")


def test_split_principal_commitment_falls_when_tranche_a_is_capped() -> None:
    commitment, split = commitment_split(
        split_inputs(Product.SPLIT_PRINCIPAL, "45000.00", "70000.00"), D("44000")
    )
    assert split is not None
    assert split.purchase_portion == D("45000.00")
    assert split.rehab_portion == D("44000")  # Tranche A, capped at rehab_adj
    assert commitment == D("89000.00")  # Principal Note + Tranche A, SPEC §8.2
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("45000.00")


@pytest.mark.parametrize("product", sorted(SPLIT_PRODUCTS))
def test_a_split_product_with_no_split_entered_is_sized_on_the_request(product: Product) -> None:
    """A borrower-channel intake nobody has divided: the screen runs, with no split."""
    data = inputs(product, loan_purchase_portion=None, loan_rehab_portion=None)
    commitment, split = commitment_split(data, D("44000"))
    assert commitment == D("115000.00")
    assert split is None
    assert funded_at_close(product, commitment, split) == commitment


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL])
def test_a_split_is_rejected_on_a_single_note_product(product: Product) -> None:
    with pytest.raises(ValidationError, match="applies only to"):
        split_inputs(product, "1000.00", "119000.00")


def test_a_split_that_does_not_add_up_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must add up to the loan requested"):
        split_inputs(Product.SPLIT_DRAW, "76000.00", "44000.01")


def test_half_a_split_is_rejected() -> None:
    with pytest.raises(ValidationError, match="set together or not at all"):
        inputs(Product.SPLIT_DRAW, loan_purchase_portion=D("76000.00"), loan_rehab_portion=None)


@pytest.mark.parametrize(
    "field, value",
    [
        ("purchase_price", "0"),
        ("loan_requested", "0"),
        ("rehab_costs", "-1"),
        ("estimated_sale_price", "0"),
    ],
)
def test_money_inputs_validated_at_the_boundary(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        inputs(**{field: D(value)})


# --- size_deal -----------------------------------------------------------------------------------


def test_size_deal_metrics_caps_and_pass() -> None:
    result = size_deal(inputs(Product.SPLIT_DRAW), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.product is Product.SPLIT_DRAW
    assert result.credit_tranche is Tranche.T2 and result.experience_tier is ExperienceTier.E2
    assert result.rehab_adj == D("40000.00")  # the config contingency default is 0.00
    assert result.closing_costs == D("1000.00")  # the config default, not a % of price
    assert result.total_cost == D("141000.00")
    assert result.commitment == D("115000.00")
    assert result.ltv_basis is ValueBasis.AS_IS_VALUE

    ltc = result.metrics[LeverageMetric.LTC]
    ltv = result.metrics[LeverageMetric.LTV_AS_IS]
    ltarv = result.metrics[LeverageMetric.LTARV]
    assert ltc.actual == D("115000.00") / D("141000.00")
    assert ltc.status is CapStatus.WITHIN_TOLERANCE  # 81.56% is over 80% but under 85%
    assert ltv.actual == D("115000.00") / D("150000.00")
    assert ltv.basis is ValueBasis.AS_IS_VALUE
    assert ltarv.actual == D("0.575")
    assert ltc.cap == D("0.80") and ltv.cap == D("0.75") and ltarv.cap == D("0.70")
    assert result.all_pass is False


def test_size_deal_status_per_metric() -> None:
    # LTC 115000/141000 = 81.56% -> within band; LTV 76.7% -> within band; LTARV 57.5% -> pass
    result = size_deal(inputs(Product.SPLIT_DRAW), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.metrics[LeverageMetric.LTC].status is CapStatus.WITHIN_TOLERANCE
    assert result.metrics[LeverageMetric.LTV_AS_IS].status is CapStatus.WITHIN_TOLERANCE
    assert result.metrics[LeverageMetric.LTARV].status is CapStatus.PASS


def test_ltv_falls_back_to_purchase_price_when_as_is_missing() -> None:
    result = size_deal(inputs(as_is_value=None), Tranche.T2, ExperienceTier.E2, CONFIG)
    ltv = result.metrics[LeverageMetric.LTV_AS_IS]
    assert result.ltv_basis is ValueBasis.PURCHASE_PRICE
    assert ltv.basis is ValueBasis.PURCHASE_PRICE
    assert ltv.actual == D("1.15")  # 115000 / 100000
    assert ltv.status is CapStatus.FAIL


def test_ltarv_not_available_when_the_sale_price_is_missing() -> None:
    result = size_deal(inputs(estimated_sale_price=None), Tranche.T2, ExperienceTier.E2, CONFIG)
    ltarv = result.metrics[LeverageMetric.LTARV]
    assert ltarv.actual is None
    assert ltarv.status is CapStatus.NOT_AVAILABLE
    assert ltarv.passed is False
    assert result.all_pass is False


def test_a_capped_tranche_a_changes_the_leverage_numerator() -> None:
    result = size_deal(
        split_inputs(Product.SPLIT_PRINCIPAL, "45000.00", "70000.00"),
        Tranche.T1,
        ExperienceTier.E3,
        CONFIG,
    )
    assert result.commitment == D("85000.00")  # 45,000 note + Tranche A capped at 40,000
    assert result.metrics[LeverageMetric.LTV_AS_IS].actual == D("85000.00") / D("150000.00")


def test_every_number_in_the_result_is_decimal() -> None:
    result = size_deal(inputs(), Tranche.T2, ExperienceTier.E2, CONFIG)
    for name in ("rehab_adj", "closing_costs", "total_cost", "commitment", "funded_at_close"):
        assert isinstance(getattr(result, name), Decimal), name
    assert result.split is not None
    assert isinstance(result.split.purchase_portion, Decimal)
    for check in result.metrics.values():
        assert isinstance(check.cap, Decimal) and isinstance(check.tolerance_band, Decimal)
        assert check.actual is None or isinstance(check.actual, Decimal)


money = st.decimals(min_value=D("0.01"), max_value=D("5000000"), places=2)
budget = st.decimals(min_value=D("0"), max_value=D("2000000"), places=2)


@settings(max_examples=200, deadline=None)
@given(product=st.sampled_from(list(Product)), price=money, rehab=budget, loan=money)
def test_split_invariants_hold_for_any_deal(
    product: Product, price: Decimal, rehab: Decimal, loan: Decimal
) -> None:
    data = inputs(product, purchase_price=price, rehab_costs=rehab, loan_requested=loan)
    result = size_deal(data, Tranche.T3, ExperienceTier.E1, CONFIG)
    assert 0 <= result.funded_at_close <= result.commitment
    if result.split is None:
        assert product in (Product.NO_DRAW, Product.WHOLETAIL)
        assert result.commitment == loan
    else:
        assert result.split.purchase_portion >= 0 and result.split.rehab_portion >= 0
        assert result.split.rehab_portion <= result.rehab_adj
        # the helper never caps, so a commitment below the request means the cap bit
        assert result.commitment <= loan
        if product is Product.SPLIT_DRAW:
            assert result.commitment == loan
    for check in result.metrics.values():
        assert check.actual is None or check.actual >= 0
        assert check.passed is (check.status is CapStatus.PASS)


# --- review: loan_requested on the result; Tranche A capped at rehab_adj -----------------------


def test_result_carries_loan_requested() -> None:
    result = size_deal(inputs(), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.loan_requested == D("115000.00")
    assert isinstance(result.loan_requested, Decimal)


def test_split_principal_tranche_a_never_exceeds_rehab_adj() -> None:
    # the team put 110,000 on the rehab side; Tranche A stays capped at rehab_adj = 44,000
    commitment, split = commitment_split(
        split_inputs(Product.SPLIT_PRINCIPAL, "5000.00", "110000.00"), D("44000")
    )
    assert split is not None
    assert split.rehab_portion == D("44000")
    assert commitment == D("49000.00")  # below the 120,000 requested; the screen reports it
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("5000.00")
