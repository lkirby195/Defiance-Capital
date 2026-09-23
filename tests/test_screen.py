"""engine/screen.py: tranches, tiers, court flags, verdict, reasons, reply.  # SPEC §7"""

from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.config import DEFAULT_PATH, Config, load_yaml
from engine.screen import (
    court_flags,
    credit_check,
    experience_check,
    is_below_floor,
    leverage_flags,
    order_flags,
    reasons_from_flags,
    screen,
    state_flags,
    suggested_reply,
    team_sourced_flags,
    tier_from_bucket,
    tier_from_verified_deals,
    tranche_from_score,
    tranche_range_text,
    verdict_from_flags,
    within_lookback,
    years_before,
)
from engine.sizing import size_deal
from engine.version import ENGINE_VERSION
from schema.models import (
    BorrowerInputs,
    CourtFlag,
    CourtRecordInputs,
    ExperienceBucket,
    ExperienceTier,
    Flag,
    LienKind,
    Product,
    RepeatBorrowerStatus,
    ScreenFlag,
    ScreenInputs,
    ScreenResult,
    Severity,
    SizingInputs,
    SizingResult,
    State,
    SubjectPropertyLien,
    Tranche,
    ValueSource,
    Verdict,
)

CONFIG = Config.load()
D = Decimal
AS_OF = date(2026, 9, 9)


def config_with(**sections: dict[str, Any]) -> Config:
    data = copy.deepcopy(load_yaml(DEFAULT_PATH.read_text(encoding="utf-8")))
    for section, values in sections.items():
        for key, value in values.items():
            data[section][key] = value
    return Config.from_dict(data)


def borrower(**overrides: Any) -> BorrowerInputs:
    base: dict[str, Any] = {
        "credit_range_self_reported": Tranche.T2,
        "experience_bucket_self_reported": ExperienceBucket.THREE_TO_FIVE,
        "repeat_borrower_self_reported": False,
    }
    base.update(overrides)
    return BorrowerInputs(**base)


def deal(**overrides: Any) -> SizingInputs:
    base: dict[str, Any] = {
        "product": Product.NO_DRAW,
        "purchase_price": D("150000.00"),
        "rehab_costs": D("0.00"),
        "loan_requested": D("105000.00"),
        "as_is_value": D("160000.00"),
        "estimated_sale_price": D("165000.00"),
    }
    base.update(overrides)
    return SizingInputs(**base)


def records(**overrides: Any) -> CourtRecordInputs:
    return CourtRecordInputs(as_of=AS_OF, **overrides)


def codes(flags: list[Flag]) -> list[str]:
    return [f.code.value for f in flags]


# --- credit (SPEC §7.1) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score, tranche",
    [
        (850, Tranche.T1),
        (740, Tranche.T1),
        (739, Tranche.T2),
        (700, Tranche.T2),
        (699, Tranche.T3),
        (660, Tranche.T3),
        (659, Tranche.T4),
        (620, Tranche.T4),
        (619, Tranche.T5),
        (300, Tranche.T5),
    ],
)
def test_tranche_from_score_uses_config_cutoffs(score: int, tranche: Tranche) -> None:
    assert tranche_from_score(score, CONFIG) is tranche


def test_tranche_range_text() -> None:
    """The label a person reads; the T-code stays the stored value (SPEC §7.1)."""
    assert tranche_range_text(Tranche.T1, CONFIG) == "740+"
    assert tranche_range_text(Tranche.T2, CONFIG) == "700–739"
    assert tranche_range_text(Tranche.T4, CONFIG) == "620–659"
    assert tranche_range_text(Tranche.T5, CONFIG) == "Under 620"


def test_tranche_range_text_follows_the_config_cutoffs() -> None:
    """A lender who moves a cutoff moves the label with it; nothing here is written out."""
    moved = config_with(credit={"tranche_cutoffs": {"T1": 760, "T2": 720, "T3": 680, "T4": 640}})
    assert tranche_range_text(Tranche.T1, moved) == "760+"
    assert tranche_range_text(Tranche.T2, moved) == "720–759"
    assert tranche_range_text(Tranche.T5, moved) == "Under 640"


def test_floor_tranche_is_config() -> None:
    assert is_below_floor(Tranche.T4, CONFIG) is False
    assert is_below_floor(Tranche.T5, CONFIG) is True
    stricter = config_with(credit={"floor_tranche": "T3"})
    assert is_below_floor(Tranche.T4, stricter) is True
    assert is_below_floor(Tranche.T3, stricter) is False


def test_credit_below_floor_is_hard_and_names_the_floor() -> None:
    outcome = credit_check(borrower(credit_range_self_reported=Tranche.T5), CONFIG)
    assert outcome.tranche is Tranche.T5 and outcome.verified is False
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.CREDIT_BELOW_FLOOR and flag.severity is Severity.HARD
    assert flag.message == "Self-reported credit Under 620 is below the floor of 620–659."


def test_verified_score_replaces_self_report_and_flags_mismatch() -> None:
    outcome = credit_check(borrower(verified_credit_score=650), CONFIG)
    assert outcome.tranche is Tranche.T4 and outcome.verified is True
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.CREDIT_MISMATCH and flag.severity is Severity.SOFT
    assert "650 (620–659)" in flag.message and "self-reported 700–739" in flag.message
    assert "T4" not in flag.message and "T2" not in flag.message


def test_verified_score_below_floor_declines_even_if_self_report_was_fine() -> None:
    outcome = credit_check(borrower(verified_credit_score=600), CONFIG)
    assert codes(outcome.flags) == ["CREDIT_MISMATCH", "CREDIT_BELOW_FLOOR"]
    assert outcome.flags[1].message.startswith("Verified credit Under 620")


def test_matching_verified_score_raises_no_flag() -> None:
    assert credit_check(borrower(verified_credit_score=720), CONFIG).flags == []


# --- experience (SPEC §7.3) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bucket, tier",
    [
        (ExperienceBucket.ZERO, ExperienceTier.E0),
        (ExperienceBucket.ONE_TO_TWO, ExperienceTier.E1),
        (ExperienceBucket.THREE_TO_FIVE, ExperienceTier.E2),
        (ExperienceBucket.SIX_PLUS, ExperienceTier.E3),
    ],
)
def test_tier_from_bucket(bucket: ExperienceBucket, tier: ExperienceTier) -> None:
    assert tier_from_bucket(bucket) is tier


@pytest.mark.parametrize(
    "count, tier",
    [
        (0, ExperienceTier.E0),
        (1, ExperienceTier.E1),
        (2, ExperienceTier.E1),
        (3, ExperienceTier.E2),
        (5, ExperienceTier.E2),
        (6, ExperienceTier.E3),
        (40, ExperienceTier.E3),
    ],
)
def test_tier_from_verified_deals(count: int, tier: ExperienceTier) -> None:
    assert tier_from_verified_deals(count) is tier


def test_negative_verified_count_rejected() -> None:
    with pytest.raises(ValueError):
        tier_from_verified_deals(-1)


def test_verified_deals_drive_the_tier_and_flag_a_mismatch() -> None:
    outcome = experience_check(borrower(verified_deals_36mo=7), CONFIG)
    assert outcome.tier_self_reported is ExperienceTier.E2
    assert outcome.tier_verified is ExperienceTier.E3
    assert outcome.tier is ExperienceTier.E3
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.EXPERIENCE_MISMATCH and flag.severity is Severity.SOFT
    assert "Verified 7 deal(s)" in flag.message and "3_5 (E2)" in flag.message


def test_verified_deals_matching_bucket_raise_nothing() -> None:
    outcome = experience_check(borrower(verified_deals_36mo=4), CONFIG)
    assert outcome.flags == [] and outcome.tier is ExperienceTier.E2


def test_clean_repeat_borrower_lifts_tier_to_config_minimum() -> None:
    outcome = experience_check(
        borrower(
            experience_bucket_self_reported=ExperienceBucket.ZERO,
            repeat_borrower_self_reported=True,
            repeat_borrower_verified=RepeatBorrowerStatus.CLEAN,
        ),
        CONFIG,
    )
    assert outcome.tier is ExperienceTier.E2 and outcome.override_applied is True
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.REPEAT_BORROWER_OVERRIDE_APPLIED
    assert flag.severity is Severity.INFO
    assert "raised from E0 to E2 (config minimum E2)" in flag.message


def test_override_minimum_is_config() -> None:
    cfg = config_with(experience={"repeat_borrower_min_tier": "E3"})
    outcome = experience_check(borrower(repeat_borrower_verified=RepeatBorrowerStatus.CLEAN), cfg)
    assert outcome.tier is ExperienceTier.E3 and outcome.override_applied is True


def test_clean_repeat_borrower_never_lowers_the_tier() -> None:
    outcome = experience_check(
        borrower(
            experience_bucket_self_reported=ExperienceBucket.SIX_PLUS,
            repeat_borrower_verified=RepeatBorrowerStatus.CLEAN,
        ),
        CONFIG,
    )
    assert outcome.tier is ExperienceTier.E3 and outcome.override_applied is False
    assert codes(outcome.flags) == ["REPEAT_BORROWER_OVERRIDE_APPLIED"]
    assert "already at or above" in outcome.flags[0].message


def test_not_clean_match_is_info_and_no_override() -> None:
    outcome = experience_check(
        borrower(
            experience_bucket_self_reported=ExperienceBucket.ZERO,
            repeat_borrower_self_reported=True,
            repeat_borrower_verified=RepeatBorrowerStatus.NOT_CLEAN,
        ),
        CONFIG,
    )
    assert outcome.tier is ExperienceTier.E0 and outcome.override_applied is False
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.REPEAT_BORROWER_PAYOFF_NOT_CLEAN
    assert flag.severity is Severity.INFO


def test_self_reported_repeat_with_no_match_is_a_soft_mismatch() -> None:
    outcome = experience_check(
        borrower(
            repeat_borrower_self_reported=True,
            repeat_borrower_verified=RepeatBorrowerStatus.NO_MATCH,
        ),
        CONFIG,
    )
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.REPEAT_BORROWER_MISMATCH and flag.severity is Severity.SOFT


def test_no_match_without_a_claim_raises_nothing() -> None:
    outcome = experience_check(
        borrower(repeat_borrower_verified=RepeatBorrowerStatus.NO_MATCH), CONFIG
    )
    assert outcome.flags == []


def test_unverified_self_reported_repeat_is_info() -> None:
    outcome = experience_check(borrower(repeat_borrower_self_reported=True), CONFIG)
    [flag] = outcome.flags
    assert flag.code is ScreenFlag.REPEAT_BORROWER_UNVERIFIED and flag.severity is Severity.INFO
    assert outcome.override_applied is False


# --- court and filing flags (SPEC §7.2) ---------------------------------------------------------


def test_years_before_handles_leap_day() -> None:
    assert years_before(date(2028, 2, 29), 1) == date(2027, 2, 28)
    assert years_before(date(2026, 9, 9), 4) == date(2022, 9, 9)


def test_within_lookback_is_inclusive_at_the_cutoff_and_counts_future_dates() -> None:
    assert within_lookback(date(2022, 9, 9), AS_OF, 4) is True
    assert within_lookback(date(2022, 9, 8), AS_OF, 4) is False
    assert within_lookback(date(2027, 1, 1), AS_OF, 4) is True


def test_no_records_is_an_info_flag_not_a_clean_record() -> None:
    [flag] = court_flags(None, CONFIG)
    assert flag.code is ScreenFlag.COURT_RECORDS_NOT_CHECKED and flag.severity is Severity.INFO


def test_clean_records_raise_nothing() -> None:
    assert court_flags(records(), CONFIG) == []


def test_bankruptcy_inside_lookback_is_hard_and_names_the_window() -> None:
    flags = court_flags(
        records(bankruptcy_filing_dates=[date(2023, 5, 1), date(2019, 5, 1)]), CONFIG
    )
    [flag] = flags
    assert flag.code is CourtFlag.BANKRUPTCY_IN_LOOKBACK and flag.severity is Severity.HARD
    assert "2023-05-01" in flag.message
    assert "4-year lookback (on or after 2022-09-09)" in flag.message


def test_lookback_years_come_from_config() -> None:
    cfg = config_with(flags={"lookbacks": {"bankruptcy_years": 10, "satisfied_judgment_years": 3}})
    flags = court_flags(records(bankruptcy_filing_dates=[date(2019, 5, 1)]), cfg)
    assert codes(flags) == ["BANKRUPTCY_IN_LOOKBACK"]
    assert "10-year lookback" in flags[0].message


def test_active_foreclosure_and_open_tax_lien_are_hard() -> None:
    flags = court_flags(
        records(active_foreclosure_as_owner=True, open_tax_liens_usd=[D("1500.00")]), CONFIG
    )
    assert codes(flags) == ["ACTIVE_FORECLOSURE_AS_OWNER", "OPEN_TAX_LIEN"]
    assert all(f.severity is Severity.HARD for f in flags)


def test_unsatisfied_judgments_aggregate_across_matters() -> None:
    # each judgment is under the $10,000 aggregate threshold; together they exceed it
    flags = court_flags(
        records(unsatisfied_judgments_usd=[D("6000.00"), D("4000.00"), D("0.01")]), CONFIG
    )
    [flag] = flags
    assert flag.code is CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD
    assert flag.severity is Severity.HARD
    assert "total $10,000.01 across 3 matter(s)" in flag.message
    assert "$10,000.00 aggregate threshold" in flag.message


def test_unsatisfied_judgments_at_or_under_the_aggregate_threshold_raise_nothing() -> None:
    assert court_flags(records(unsatisfied_judgments_usd=[D("6000"), D("4000")]), CONFIG) == []
    assert court_flags(records(unsatisfied_judgments_usd=[D("10000.00")]), CONFIG) == []


def test_any_open_tax_lien_is_flagged_regardless_of_amount() -> None:
    [flag] = court_flags(records(open_tax_liens_usd=[D("0.01")]), CONFIG)
    assert flag.code is CourtFlag.OPEN_TAX_LIEN and flag.severity is Severity.HARD
    assert "total $0.01 across 1 matter(s)" in flag.message
    assert "regardless of amount" in flag.message
    # an open lien of unknown / zero amount still flags
    [flag] = court_flags(records(open_tax_liens_usd=[D("0")]), CONFIG)
    assert flag.code is CourtFlag.OPEN_TAX_LIEN
    [flag] = court_flags(records(open_tax_liens_usd=[D("2000"), D("3000.01")]), CONFIG)
    assert "total $5,000.01 across 2 matter(s)" in flag.message
    assert court_flags(records(open_tax_liens_usd=[]), CONFIG) == []
    assert not hasattr(CONFIG.flags.thresholds, "open_tax_liens_aggregate_usd")


def test_civil_litigation_is_soft_and_names_the_threshold() -> None:
    flags = court_flags(
        records(active_civil_litigation_as_defendant_usd=[D("25000"), D("26000")]), CONFIG
    )
    [flag] = flags
    assert flag.code is CourtFlag.ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT
    assert flag.severity is Severity.SOFT
    assert "$26,000.00 exceeds the $25,000.00 threshold" in flag.message


def test_satisfied_judgment_inside_lookback_is_soft() -> None:
    flags = court_flags(
        records(satisfied_judgment_or_released_lien_dates=[date(2024, 1, 1), date(2020, 1, 1)]),
        CONFIG,
    )
    [flag] = flags
    assert flag.code is CourtFlag.SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK
    assert flag.severity is Severity.SOFT
    assert "3-year lookback (on or after 2023-09-09)" in flag.message


def test_landlord_tenant_is_info_with_a_count() -> None:
    [flag] = court_flags(records(landlord_tenant_matters_as_landlord=2), CONFIG)
    assert flag.code is CourtFlag.LANDLORD_TENANT_AS_LANDLORD and flag.severity is Severity.INFO
    assert flag.message.startswith("2 landlord-tenant matter(s)")


def test_subject_property_lien_flags_only_when_senior_and_unresolved() -> None:
    liens = [
        SubjectPropertyLien(kind=LienKind.LIEN, senior=True, resolved_at_close=True),
        SubjectPropertyLien(kind=LienKind.LIEN, senior=False, resolved_at_close=False),
        SubjectPropertyLien(
            kind=LienKind.LIS_PENDENS, senior=True, resolved_at_close=False, amount_usd=D("12000")
        ),
    ]
    [flag] = court_flags(records(subject_property_liens=liens), CONFIG)
    assert flag.code is CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS
    assert flag.severity is Severity.HARD
    assert "Senior lis pendens of $12,000.00" in flag.message


def test_court_flag_severity_comes_from_config() -> None:
    severities = dict(CONFIG.flags.severities)
    severities[CourtFlag.OPEN_TAX_LIEN] = Severity.SOFT
    cfg = config_with(flags={"severities": {k.value: v.value for k, v in severities.items()}})
    [flag] = court_flags(records(open_tax_liens_usd=[D("100")]), cfg)
    assert flag.severity is Severity.SOFT


# --- state and leverage flags (SPEC §7.4, §7.5) -------------------------------------------------


def test_state_outside_served_list_is_soft() -> None:
    assert state_flags(State.OK, CONFIG) == []
    [flag] = state_flags(State.OTHER, CONFIG)
    assert flag.code is ScreenFlag.STATE_NOT_SERVED and flag.severity is Severity.SOFT
    assert "outside the served states (OK, CO)" in flag.message
    only_ok = config_with(states={"served": ["OK"], "court_adapter": {"OK": "oscn"}})
    assert codes(state_flags(State.CO, only_ok)) == ["STATE_NOT_SERVED"]


def test_leverage_flags_name_cap_actual_band_and_cell() -> None:
    sizing = size_deal(
        deal(
            product=Product.SPLIT_PRINCIPAL,
            purchase_price=D("100000.00"),
            rehab_costs=D("40000.00"),
            loan_requested=D("135000.00"),
            as_is_value=D("110000.00"),
            estimated_sale_price=D("180000.00"),
        ),
        Tranche.T3,
        ExperienceTier.E1,
        CONFIG,
    )
    flags = leverage_flags(sizing)
    by_code = {f.code: f for f in flags}
    ltc = by_code[ScreenFlag.LTC_OVER_CAP]
    assert ltc.severity is Severity.HARD
    assert re.search(r"LTC 95\.7% exceeds the 80\.0% cap for SPLIT_PRINCIPAL/T3/E1", ltc.message)
    assert "5.0 pt tolerance band (limit 85.0%)" in ltc.message
    ltarv = by_code[ScreenFlag.LTARV_OVER_CAP]
    assert ltarv.severity is Severity.SOFT
    assert "LTARV 75.0% is over the 70.0% cap" in ltarv.message


def test_missing_values_are_soft_flags() -> None:
    sizing = size_deal(
        deal(as_is_value=None, estimated_sale_price=None), Tranche.T2, ExperienceTier.E2, CONFIG
    )
    flags = leverage_flags(sizing)
    assert codes(flags) == ["AS_IS_VALUE_MISSING", "ESTIMATED_SALE_PRICE_MISSING"]
    assert all(f.severity is Severity.SOFT for f in flags)
    assert "LTV computed on the purchase price" in flags[0].message
    assert "cap 70.0%" in flags[1].message


def test_ltv_breach_on_purchase_price_says_so() -> None:
    sizing = size_deal(
        deal(as_is_value=None, loan_requested=D("125000.00")), Tranche.T2, ExperienceTier.E2, CONFIG
    )
    by_code = {f.code: f for f in leverage_flags(sizing)}
    assert by_code[ScreenFlag.LTV_AS_IS_OVER_CAP].message.startswith(
        "LTV (on purchase price) 83.3%"
    )


def test_split_principal_capped_tranche_a_below_request_is_an_info_flag() -> None:
    sizing = size_deal(
        deal(
            product=Product.SPLIT_PRINCIPAL,
            purchase_price=D("150000.00"),
            rehab_costs=D("60000.00"),
            loan_requested=D("170000.00"),
            as_is_value=D("230000.00"),
            estimated_sale_price=D("290000.00"),
            loan_purchase_portion=D("80000.00"),
            loan_rehab_portion=D("90000.00"),  # capped at rehab_adj = 66,000
        ),
        Tranche.T2,
        ExperienceTier.E2,
        CONFIG,
    )
    assert sizing.commitment == D("140000.00") and sizing.loan_requested == D("170000.00")
    # two Info flags, and they say different things: one that the entered rehab portion is
    # over the budget, one that the commitment fell as a result
    flags = leverage_flags(sizing)
    assert [f.code for f in flags] == [
        ScreenFlag.REHAB_PORTION_EXCEEDS_BUDGET,
        ScreenFlag.COMMITMENT_BELOW_REQUEST,
    ]
    flag = flags[1]
    assert flag.code is ScreenFlag.COMMITMENT_BELOW_REQUEST and flag.severity is Severity.INFO
    assert "commitment $140,000.00" in flag.message
    assert "$170,000.00 requested" in flag.message
    assert "Tranche A $60,000.00" in flag.message
    assert "entered rehab portion is capped" in flag.message
    assert "rehab cost $60,000.00" in flag.message


def test_a_rehab_portion_over_the_budget_is_an_info_flag_on_both_split_products() -> None:
    """Naming both amounts: what the team entered, and what the budget allows.  # SPEC §8.2"""
    for product, consequence in (
        (Product.SPLIT_PRINCIPAL, "Tranche A is capped at the budget"),
        (Product.SPLIT_DRAW, "the difference is advanced at close"),
    ):
        sizing = size_deal(
            deal(
                product=product,
                purchase_price=D("150000.00"),
                rehab_costs=D("60000.00"),
                loan_requested=D("170000.00"),
                as_is_value=D("230000.00"),
                estimated_sale_price=D("290000.00"),
                loan_purchase_portion=D("80000.00"),
                loan_rehab_portion=D("90000.00"),  # rehab_adj is 60,000
            ),
            Tranche.T2,
            ExperienceTier.E2,
            CONFIG,
        )
        flag = next(
            f for f in leverage_flags(sizing) if f.code is ScreenFlag.REHAB_PORTION_EXCEEDS_BUDGET
        )
        assert flag.severity is Severity.INFO
        assert f"{product.value} rehab portion $90,000.00" in flag.message
        assert "rehab cost $60,000.00" in flag.message
        assert consequence in flag.message


def test_no_rehab_portion_flag_when_the_portion_is_within_the_budget() -> None:
    sizing = size_deal(
        deal(
            product=Product.SPLIT_DRAW,
            purchase_price=D("150000.00"),
            rehab_costs=D("60000.00"),
            loan_requested=D("170000.00"),
            as_is_value=D("230000.00"),
            estimated_sale_price=D("290000.00"),
            loan_purchase_portion=D("110000.00"),
            loan_rehab_portion=D("60000.00"),  # exactly rehab_adj, so nothing is capped
        ),
        Tranche.T2,
        ExperienceTier.E2,
        CONFIG,
    )
    codes = {f.code for f in leverage_flags(sizing)}
    assert ScreenFlag.REHAB_PORTION_EXCEEDS_BUDGET not in codes


def test_no_commitment_flag_when_the_request_is_fully_allocated() -> None:
    for product in (Product.SPLIT_PRINCIPAL, Product.SPLIT_DRAW):
        sizing = size_deal(
            deal(
                product=product,
                purchase_price=D("150000.00"),
                rehab_costs=D("60000.00"),
                loan_requested=D("70000.00"),
                as_is_value=D("230000.00"),
                estimated_sale_price=D("290000.00"),
                loan_purchase_portion=D("10000.00"),
                loan_rehab_portion=D("60000.00"),  # exactly rehab_adj, so uncapped
            ),
            Tranche.T2,
            ExperienceTier.E2,
            CONFIG,
        )
        assert sizing.commitment == D("70000.00")
        assert ScreenFlag.COMMITMENT_BELOW_REQUEST not in {f.code for f in leverage_flags(sizing)}


# --- verdict, reasons, reply (SPEC §7.5) --------------------------------------------------------


def flag(severity: Severity, code: ScreenFlag = ScreenFlag.ESTIMATED_SALE_PRICE_MISSING) -> Flag:
    return Flag(code=code, severity=severity, message=f"{severity.value} test flag")


def test_verdict_precedence() -> None:
    assert verdict_from_flags([]) is Verdict.GO
    assert verdict_from_flags([flag(Severity.INFO)]) is Verdict.GO
    assert verdict_from_flags([flag(Severity.SOFT), flag(Severity.INFO)]) is Verdict.CONDITIONAL
    assert verdict_from_flags([flag(Severity.SOFT), flag(Severity.HARD)]) is Verdict.DECLINE


def test_reasons_are_severity_ordered_and_labelled() -> None:
    flags = [flag(Severity.INFO), flag(Severity.HARD), flag(Severity.SOFT)]
    assert [f.severity for f in order_flags(flags)] == [
        Severity.HARD,
        Severity.SOFT,
        Severity.INFO,
    ]
    assert reasons_from_flags(flags) == [
        "Hard: HARD test flag",
        "Soft: SOFT test flag",
        "Info: INFO test flag",
    ]


def test_suggested_reply_never_names_findings_on_decline() -> None:
    reply = suggested_reply(Verdict.DECLINE, [flag(Severity.HARD)], CONFIG)
    assert "not a fit" in reply
    assert "test flag" not in reply and "credit" not in reply.lower()


def test_suggested_reply_lists_deduplicated_asks_on_conditional() -> None:
    flags = [
        flag(Severity.SOFT, ScreenFlag.ESTIMATED_SALE_PRICE_MISSING),
        flag(Severity.SOFT, ScreenFlag.LTC_OVER_CAP),
        flag(Severity.SOFT, ScreenFlag.LTV_AS_IS_OVER_CAP),
        flag(Severity.SOFT, ScreenFlag.STATE_NOT_SERVED),
        Flag(
            code=CourtFlag.ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT, severity=Severity.SOFT, message="x"
        ),
        flag(Severity.INFO, ScreenFlag.REPEAT_BORROWER_UNVERIFIED),
    ]
    reply = suggested_reply(Verdict.CONDITIONAL, flags, CONFIG)
    assert reply.count("lower loan amount") == 1
    assert "estimated sale price after the work" in reply
    assert "we lend in OK, CO" in reply
    assert "public records" in reply
    assert "prior loan" not in reply  # INFO flags do not generate asks


def test_suggested_reply_go_asks_for_authorization_and_contract() -> None:
    reply = suggested_reply(Verdict.GO, [], CONFIG)
    assert "credit authorization" in reply and "purchase contract" in reply


# --- end to end ----------------------------------------------------------------------------------


def test_screen_records_version_hash_components_and_round_trips() -> None:
    result = screen(
        ScreenInputs(deal=deal(), state=State.OK, borrower=borrower(), court_records=records()),
        CONFIG,
    )
    assert result.verdict is Verdict.GO and result.flags == [] and result.reasons == []
    assert result.engine_version == ENGINE_VERSION
    assert result.config_hash == CONFIG.config_hash
    assert result.components.credit_tranche is Tranche.T2
    assert result.components.credit_meets_floor is True
    assert result.components.floor_tranche is Tranche.T4
    assert result.components.experience_tier is ExperienceTier.E2
    assert result.components.experience_tier_verified is None
    assert ScreenResult.model_validate_json(result.model_dump_json()) == result


def test_screen_uses_the_lifted_tier_for_the_caps_lookup() -> None:
    result = screen(
        ScreenInputs(
            deal=deal(),
            state=State.OK,
            borrower=borrower(
                experience_bucket_self_reported=ExperienceBucket.ZERO,
                repeat_borrower_verified=RepeatBorrowerStatus.CLEAN,
            ),
            court_records=records(),
        ),
        CONFIG,
    )
    assert result.sizing.experience_tier is ExperienceTier.E2
    assert result.components.experience_tier_self_reported is ExperienceTier.E0


def test_screen_without_court_records_still_runs_with_an_info_note() -> None:
    result = screen(ScreenInputs(deal=deal(), state=State.OK, borrower=borrower()), CONFIG)
    assert result.verdict is Verdict.GO
    assert codes(result.flags) == ["COURT_RECORDS_NOT_CHECKED"]


def test_engine_modules_do_no_io() -> None:
    """CLAUDE.md: nothing under engine/ does I/O, touches the DB, reads env, or hits the network."""
    forbidden = re.compile(
        r"^\s*(import|from)\s+(os|sys|io|pathlib|httpx|requests|sqlalchemy|db|api|dotenv|yaml)\b"
        r"|\bopen\(|\.today\(\)|\.now\(|os\.environ|getenv",
        re.MULTILINE,
    )
    engine_dir = Path(__file__).resolve().parents[1] / "engine"
    sources = list(engine_dir.rglob("*.py"))
    assert len(sources) >= 12  # incl. engine/calc/
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert forbidden.search(text) is None, f"{source.name}: {forbidden.search(text)}"


# --- TEAM_SOURCED_VALUES (SPEC §6.1) -------------------------------------------------------------


def sized_with(as_is: ValueSource | None, sale: ValueSource | None) -> SizingResult:
    """A sizing result whose valuation halves carry the sources asked for."""
    return size_deal(
        deal().model_copy(
            update={"as_is_value_source": as_is, "estimated_sale_price_source": sale}
        ),
        Tranche.T2,
        ExperienceTier.E2,
        CONFIG,
    )


def searched(source: ValueSource) -> CourtRecordInputs:
    return CourtRecordInputs(as_of=date(2026, 9, 17), source=source)


def test_no_flag_when_every_value_was_pulled() -> None:
    both_pulled = sized_with(ValueSource.ADAPTER, ValueSource.ADAPTER)
    assert team_sourced_flags(both_pulled, searched(ValueSource.ADAPTER)) == []
    assert team_sourced_flags(both_pulled, None) == []


def test_the_flag_names_which_values_the_team_entered() -> None:
    all_three = team_sourced_flags(
        sized_with(ValueSource.TEAM, ValueSource.TEAM), searched(ValueSource.TEAM)
    )
    assert len(all_three) == 1
    flag = all_three[0]
    assert flag.code is ScreenFlag.TEAM_SOURCED_VALUES
    assert flag.severity is Severity.INFO
    assert flag.message.startswith(
        "As-is value, estimated sale price, and court records came from the team"
    )

    only_sale = team_sourced_flags(
        sized_with(ValueSource.ADAPTER, ValueSource.TEAM), searched(ValueSource.ADAPTER)
    )
    assert only_sale[0].message.startswith("Estimated sale price came from the team")

    two_of_them = team_sourced_flags(
        sized_with(ValueSource.TEAM, ValueSource.ADAPTER), searched(ValueSource.TEAM)
    )
    assert two_of_them[0].message.startswith("As-is value and court records came from the team")

    court_only = team_sourced_flags(
        sized_with(ValueSource.ADAPTER, ValueSource.ADAPTER), searched(ValueSource.TEAM)
    )
    assert court_only[0].message.startswith("Court records came from the team")


def test_an_unavailable_value_is_not_a_team_value() -> None:
    """A missing valuation has its own flags; it is not also reported as hand-entered."""
    nothing = size_deal(
        deal(as_is_value=None, estimated_sale_price=None), Tranche.T2, ExperienceTier.E2, CONFIG
    )
    assert nothing.as_is_value_source is None and nothing.estimated_sale_price_source is None
    assert team_sourced_flags(nothing, None) == []
    # the screen still says the values are missing, through their own flags
    assert codes(leverage_flags(nothing)) == ["AS_IS_VALUE_MISSING", "ESTIMATED_SALE_PRICE_MISSING"]


def test_the_flag_is_information_and_never_moves_a_verdict() -> None:
    inputs = ScreenInputs(
        deal=deal().model_copy(
            update={
                "as_is_value_source": ValueSource.TEAM,
                "estimated_sale_price_source": ValueSource.TEAM,
            }
        ),
        state=State.OK,
        borrower=borrower(),
        court_records=searched(ValueSource.TEAM),
    )
    result = screen(inputs, CONFIG)
    assert result.verdict is Verdict.GO
    assert [f.code.value for f in result.flags] == ["TEAM_SOURCED_VALUES"]
    assert result.reasons == [f"Info: {result.flags[0].message}"]
