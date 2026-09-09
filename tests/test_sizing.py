"""engine/sizing.py: implied leverage, caps with tolerance band, commitment split.  # SPEC §7.4"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from config.config import DEFAULT_PATH, Config, load_yaml
from engine.sizing import (
    cap_status,
    caps_for,
    check_metric,
    commitment_split,
    estimated_closing,
    funded_at_close,
    ratio,
    rehab_adjusted,
    size_deal,
    total_cost,
)
from schema.models import (
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
    base: dict[str, Any] = {
        "product": product,
        "purchase_price": D("100000.00"),
        "rehab_budget": D("40000.00"),
        "loan_requested": D("120000.00"),
        "as_is_value": D("150000.00"),
        "arv": D("200000.00"),
    }
    base.update(overrides)
    return SizingInputs(**base)


# --- §7.4 building blocks ----------------------------------------------------------------------


def test_rehab_adjusted_applies_contingency() -> None:
    assert CONFIG.fees.contingency_pct == D("0.10")
    assert rehab_adjusted(D("40000.00"), CONFIG) == D("44000.0000")
    assert rehab_adjusted(D("0"), CONFIG) == 0


def test_estimated_closing_is_pct_of_price() -> None:
    assert CONFIG.fees.est_closing_pct_of_price == D("0.02")
    assert estimated_closing(D("100000.00"), CONFIG) == D("2000.0000")


def test_total_cost_is_price_plus_rehab_adj_plus_closing() -> None:
    assert total_cost(D("100000.00"), D("40000.00"), CONFIG) == D("146000.0000")


def test_thresholds_come_from_config_not_code() -> None:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    data["fees"]["contingency_pct"] = D("0.20")
    data["fees"]["est_closing_pct_of_price"] = D("0.03")
    cfg = Config.from_dict(data)
    assert rehab_adjusted(D("1000"), cfg) == D("1200.00")
    assert estimated_closing(D("1000"), cfg) == D("30.00")


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
    commitment, split = commitment_split(inputs(product), D("44000"))
    assert commitment == D("120000.00")
    assert split is None
    assert funded_at_close(product, commitment, split) == commitment


def test_split_draw_default_split_and_holdback() -> None:
    commitment, split = commitment_split(inputs(Product.SPLIT_DRAW), D("44000"))
    assert commitment == D("120000.00")
    assert split is not None
    assert split.purchase_portion == D("76000.00")  # commitment - rehab_adj
    assert split.rehab_portion == D("44000")  # holdback = min(rehab_adj, commitment - purchase)
    assert split.purchase_portion_overridden is False
    assert funded_at_close(Product.SPLIT_DRAW, commitment, split) == D("76000.00")


def test_split_draw_override_caps_holdback_at_remaining_commitment() -> None:
    commitment, split = commitment_split(
        inputs(Product.SPLIT_DRAW, purchase_portion_override=D("90000.00")), D("44000")
    )
    assert commitment == D("120000.00")  # SPLIT_DRAW commitment is always loan_requested
    assert split is not None
    assert split.purchase_portion == D("90000.00")
    assert split.rehab_portion == D("30000.00")  # min(44000, 120000 - 90000)
    assert split.purchase_portion_overridden is True
    assert funded_at_close(Product.SPLIT_DRAW, commitment, split) == D("90000.00")


def test_split_draw_rehab_larger_than_loan_holds_everything_back() -> None:
    commitment, split = commitment_split(
        inputs(Product.SPLIT_DRAW, loan_requested=D("30000.00")), D("44000")
    )
    assert split is not None
    assert split.purchase_portion == 0  # floored, never negative
    assert split.rehab_portion == D("30000.00")
    assert funded_at_close(Product.SPLIT_DRAW, commitment, split) == 0


def test_split_principal_default_equals_loan_requested() -> None:
    commitment, split = commitment_split(inputs(Product.SPLIT_PRINCIPAL), D("44000"))
    assert split is not None
    assert split.purchase_portion == D("76000.00")  # Principal Note
    assert split.rehab_portion == D("44000")  # Tranche A
    assert commitment == split.purchase_portion + split.rehab_portion == D("120000.00")
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("76000.00")


def test_split_principal_override_can_shrink_the_commitment() -> None:
    commitment, split = commitment_split(
        inputs(Product.SPLIT_PRINCIPAL, purchase_portion_override=D("50000.00")), D("44000")
    )
    assert split is not None
    assert split.purchase_portion == D("50000.00")
    assert split.rehab_portion == D("44000")  # min(44000, 120000 - 50000)
    assert commitment == D("94000.00")  # Principal Note + Tranche A, SPEC §8.2
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("50000.00")


@pytest.mark.parametrize("product", [Product.NO_DRAW, Product.WHOLETAIL])
def test_override_rejected_for_single_note_products(product: Product) -> None:
    with pytest.raises(ValidationError, match="SPLIT_DRAW / SPLIT_PRINCIPAL"):
        inputs(product, purchase_portion_override=D("1000.00"))


def test_override_above_loan_requested_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot exceed loan_requested"):
        inputs(Product.SPLIT_DRAW, purchase_portion_override=D("120000.01"))


@pytest.mark.parametrize(
    "field, value",
    [("purchase_price", "0"), ("loan_requested", "0"), ("rehab_budget", "-1"), ("arv", "0")],
)
def test_money_inputs_validated_at_the_boundary(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        inputs(**{field: D(value)})


# --- size_deal -----------------------------------------------------------------------------------


def test_size_deal_metrics_caps_and_pass() -> None:
    result = size_deal(inputs(Product.SPLIT_DRAW), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.product is Product.SPLIT_DRAW
    assert result.credit_tranche is Tranche.T2 and result.experience_tier is ExperienceTier.E2
    assert result.rehab_adj == D("44000.0000")
    assert result.est_closing == D("2000.0000")
    assert result.total_cost == D("146000.0000")
    assert result.commitment == D("120000.00")
    assert result.ltv_basis is ValueBasis.AS_IS_VALUE

    ltc = result.metrics[LeverageMetric.LTC]
    ltv = result.metrics[LeverageMetric.LTV_AS_IS]
    ltarv = result.metrics[LeverageMetric.LTARV]
    assert ltc.actual == D("120000.00") / D("146000.0000")
    assert ltc.status is CapStatus.WITHIN_TOLERANCE  # 82.19% is over 80% but under 85%
    assert ltv.actual == D("0.8") and ltv.basis is ValueBasis.AS_IS_VALUE
    assert ltarv.actual == D("0.6")
    assert ltc.cap == D("0.80") and ltv.cap == D("0.75") and ltarv.cap == D("0.70")
    assert result.all_pass is False


def test_size_deal_status_per_metric() -> None:
    # LTC 120000/146000 = 82.19% -> within band; LTV 80% -> within band; LTARV 60% -> pass
    result = size_deal(inputs(Product.SPLIT_DRAW), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.metrics[LeverageMetric.LTC].status is CapStatus.WITHIN_TOLERANCE
    assert result.metrics[LeverageMetric.LTV_AS_IS].status is CapStatus.WITHIN_TOLERANCE
    assert result.metrics[LeverageMetric.LTARV].status is CapStatus.PASS


def test_ltv_falls_back_to_purchase_price_when_as_is_missing() -> None:
    result = size_deal(inputs(as_is_value=None), Tranche.T2, ExperienceTier.E2, CONFIG)
    ltv = result.metrics[LeverageMetric.LTV_AS_IS]
    assert result.ltv_basis is ValueBasis.PURCHASE_PRICE
    assert ltv.basis is ValueBasis.PURCHASE_PRICE
    assert ltv.actual == D("1.2")  # 120000 / 100000
    assert ltv.status is CapStatus.FAIL


def test_ltarv_not_available_when_arv_missing() -> None:
    result = size_deal(inputs(arv=None), Tranche.T2, ExperienceTier.E2, CONFIG)
    ltarv = result.metrics[LeverageMetric.LTARV]
    assert ltarv.actual is None
    assert ltarv.status is CapStatus.NOT_AVAILABLE
    assert ltarv.passed is False
    assert result.all_pass is False


def test_split_principal_override_changes_the_leverage_numerator() -> None:
    result = size_deal(
        inputs(Product.SPLIT_PRINCIPAL, purchase_portion_override=D("50000.00")),
        Tranche.T1,
        ExperienceTier.E3,
        CONFIG,
    )
    assert result.commitment == D("94000.00")
    assert result.metrics[LeverageMetric.LTV_AS_IS].actual == D("94000.00") / D("150000.00")


def test_every_number_in_the_result_is_decimal() -> None:
    result = size_deal(inputs(), Tranche.T2, ExperienceTier.E2, CONFIG)
    for name in ("rehab_adj", "est_closing", "total_cost", "commitment", "funded_at_close"):
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
    data = inputs(product, purchase_price=price, rehab_budget=rehab, loan_requested=loan)
    result = size_deal(data, Tranche.T3, ExperienceTier.E1, CONFIG)
    assert result.commitment == loan  # no override: commitment is what was asked for
    assert 0 <= result.funded_at_close <= result.commitment
    if result.split is None:
        assert product in (Product.NO_DRAW, Product.WHOLETAIL)
    else:
        assert result.split.purchase_portion >= 0 and result.split.rehab_portion >= 0
        assert result.split.purchase_portion + result.split.rehab_portion == result.commitment
        assert result.split.rehab_portion <= result.rehab_adj
    for check in result.metrics.values():
        assert check.actual is None or check.actual >= 0
        assert check.passed is (check.status is CapStatus.PASS)


# --- review: loan_requested on the result; Tranche A capped at rehab_adj -----------------------


def test_result_carries_loan_requested() -> None:
    result = size_deal(inputs(), Tranche.T2, ExperienceTier.E2, CONFIG)
    assert result.loan_requested == D("120000.00")
    assert isinstance(result.loan_requested, Decimal)


def test_split_principal_tranche_a_never_exceeds_rehab_adj() -> None:
    # the override leaves 110,000 of room, but Tranche A stays capped at rehab_adj = 44,000
    commitment, split = commitment_split(
        inputs(Product.SPLIT_PRINCIPAL, purchase_portion_override=D("10000.00")), D("44000")
    )
    assert split is not None
    assert split.rehab_portion == D("44000")
    assert commitment == D("54000.00")  # below the 120,000 requested; the screen reports it
    assert funded_at_close(Product.SPLIT_PRINCIPAL, commitment, split) == D("10000.00")
