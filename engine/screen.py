"""Screen: credit tranche, court/filing flags, experience tier, implied leverage.  # SPEC §7

Pure: typed inputs and ``Config`` in, ``ScreenResult`` out. Thresholds, lookbacks,
court-flag severities, and caps come from config. The verdict rules are SPEC §7.5:

* Decline      any HARD flag: a court/filing flag configured HARD, credit below the
               floor tranche, or a leverage metric above its cap by more than the
               tolerance band
* Conditional  no HARD flag and any SOFT flag: a SOFT court/filing flag, leverage
               within the tolerance band above its cap, missing ARV or as-is value,
               a self-reported vs. verified mismatch, or state = OTHER
* Go           neither

Every flag carries a stable code, a severity, and a message naming the threshold it was
tested against. ``reasons`` are the flag messages in severity order, and
``suggested_reply`` is a draft for a human to send (never sent automatically, SPEC §4.3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from engine.sizing import size_deal
from engine.version import ENGINE_VERSION
from schema.labels import tranche_label
from schema.models import (
    BorrowerInputs,
    CapStatus,
    CourtFlag,
    CourtRecordInputs,
    ExperienceBucket,
    ExperienceTier,
    Flag,
    LeverageMetric,
    LienKind,
    Product,
    RepeatBorrowerStatus,
    ScreenComponents,
    ScreenFlag,
    ScreenInputs,
    ScreenResult,
    Severity,
    SizingResult,
    State,
    Tranche,
    ValueBasis,
    ValueSource,
    Verdict,
)

_TRANCHE_ORDER: list[Tranche] = list(Tranche)  # T1 (best) .. T5 (worst)  # SPEC §7.1
_TIER_ORDER: list[ExperienceTier] = list(ExperienceTier)  # E0 .. E3  # SPEC §7.3
_SEVERITY_RANK: dict[Severity, int] = {Severity.HARD: 0, Severity.SOFT: 1, Severity.INFO: 2}

# The self-reported buckets (SPEC §4.1) and the verified tiers (SPEC §7.3) share one set of
# cut points: 0 / 1-2 / 3-5 / 6+. They mirror the ExperienceBucket enum, not a tunable.
_BUCKET_TO_TIER: dict[ExperienceBucket, ExperienceTier] = {
    ExperienceBucket.ZERO: ExperienceTier.E0,
    ExperienceBucket.ONE_TO_TWO: ExperienceTier.E1,
    ExperienceBucket.THREE_TO_FIVE: ExperienceTier.E2,
    ExperienceBucket.SIX_PLUS: ExperienceTier.E3,
}
_TIER_MIN_DEALS: tuple[tuple[ExperienceTier, int], ...] = (
    (ExperienceTier.E3, 6),
    (ExperienceTier.E2, 3),
    (ExperienceTier.E1, 1),
    (ExperienceTier.E0, 0),
)

_METRIC_FLAG: dict[LeverageMetric, ScreenFlag] = {
    LeverageMetric.LTC: ScreenFlag.LTC_OVER_CAP,
    LeverageMetric.LTV_AS_IS: ScreenFlag.LTV_AS_IS_OVER_CAP,
    LeverageMetric.LTARV: ScreenFlag.LTARV_OVER_CAP,
}
_METRIC_LABEL: dict[LeverageMetric, str] = {
    LeverageMetric.LTC: "LTC",
    LeverageMetric.LTV_AS_IS: "LTV (as-is)",
    LeverageMetric.LTARV: "LTARV",
}

# Reply drafts. A human edits and sends these (SPEC §4.3); they never name credit or
# court findings, only what the team still needs from the borrower.
_REPLY_DECLINE = (
    "Thanks for sending this over. After a first look, this one is not a fit for us right "
    "now, so we will pass. Happy to take a look at the next one."
)
_REPLY_GO = (
    "Thanks - this looks like a fit on first pass. Next step is a full underwrite: we will "
    "need a signed credit authorization and we will order a valuation. Can you send the "
    "purchase contract when you have it?"
)
_ASK_BY_FLAG: dict[ScreenFlag, str] = {
    ScreenFlag.ARV_MISSING: "an after-repair value or comps for the property",
    ScreenFlag.AS_IS_VALUE_MISSING: "a current as-is value or a recent appraisal",
    ScreenFlag.STATE_NOT_SERVED: "the full property address (we lend in {served})",
    ScreenFlag.LTC_OVER_CAP: "whether you can work with a somewhat lower loan amount",
    ScreenFlag.LTV_AS_IS_OVER_CAP: "whether you can work with a somewhat lower loan amount",
    ScreenFlag.LTARV_OVER_CAP: "whether you can work with a somewhat lower loan amount",
    ScreenFlag.EXPERIENCE_MISMATCH: (
        "the addresses of the deals you have completed in the last three years"
    ),
    ScreenFlag.REPEAT_BORROWER_MISMATCH: "the address or loan number of your prior loan with us",
}
_ASK_COURT = "a bit of background on some public records we found"
_ASK_DEFAULT = "a few more details"


# --- formatting helpers ------------------------------------------------------------------------


def money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def pct(value: Decimal) -> str:
    return f"{value:.1%}"


def points(band: Decimal) -> str:
    return f"{band * 100:.1f} pt"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# --- credit (SPEC §7.1) -------------------------------------------------------------------------


def tranche_from_score(score: int, config: Config) -> Tranche:
    """T1..T4 by the descending FICO cutoffs in config; T5 below the T4 cutoff.  # SPEC §7.1"""
    for tranche, cutoff in config.credit.tranche_cutoffs.items():
        if score >= cutoff:
            return tranche
    return Tranche.T5


def tranche_range_text(tranche: Tranche, config: Config) -> str:
    """The FICO range a tranche stands for: '740+', '700–739', ..., 'Under 620'.

    The one formatter, shared with the review queue (``schema/labels.py``), so a flag
    message and the screen summary beside it never disagree about what T3 means.
    """
    return tranche_label(tranche, config.credit.tranche_cutoffs)


def is_below_floor(tranche: Tranche, config: Config) -> bool:
    """True when ``tranche`` is worse than ``credit.floor_tranche``.  # SPEC §7.1, §7.5"""
    return _TRANCHE_ORDER.index(tranche) > _TRANCHE_ORDER.index(config.credit.floor_tranche)


class CreditOutcome(NamedTuple):
    tranche: Tranche
    verified: bool
    flags: list[Flag]


def credit_check(borrower: BorrowerInputs, config: Config) -> CreditOutcome:
    """Tranche in use (a verified score replaces the self-report), floor check, mismatch flag."""
    self_reported = borrower.credit_range_self_reported
    flags: list[Flag] = []
    verified = borrower.verified_credit_score is not None
    if borrower.verified_credit_score is not None:
        tranche = tranche_from_score(borrower.verified_credit_score, config)
        if tranche is not self_reported:
            flags.append(
                Flag(
                    code=ScreenFlag.CREDIT_MISMATCH,
                    severity=Severity.SOFT,
                    message=(
                        f"Verified credit score {borrower.verified_credit_score} "
                        f"({tranche_range_text(tranche, config)}) differs from the "
                        f"self-reported {tranche_range_text(self_reported, config)}."
                    ),
                )
            )
    else:
        tranche = self_reported
    if is_below_floor(tranche, config):
        floor = config.credit.floor_tranche
        flags.append(
            Flag(
                code=ScreenFlag.CREDIT_BELOW_FLOOR,
                severity=Severity.HARD,
                message=(
                    f"{'Verified' if verified else 'Self-reported'} credit "
                    f"{tranche_range_text(tranche, config)} is below the floor of "
                    f"{tranche_range_text(floor, config)}."
                ),
            )
        )
    return CreditOutcome(tranche, verified, flags)


# --- experience (SPEC §7.3) ---------------------------------------------------------------------


def tier_from_bucket(bucket: ExperienceBucket) -> ExperienceTier:
    """Self-reported bucket -> tier with the same cut points.  # SPEC §4.1, §7.3"""
    return _BUCKET_TO_TIER[bucket]


def tier_from_verified_deals(count: int) -> ExperienceTier:
    """Verified buy->sell pairs in the last 36 months -> tier.  # SPEC §6, §7.3"""
    if count < 0:
        raise ValueError("verified deal count cannot be negative")
    for tier, minimum in _TIER_MIN_DEALS:
        if count >= minimum:
            return tier
    return ExperienceTier.E0


def higher_tier(a: ExperienceTier, b: ExperienceTier) -> ExperienceTier:
    return a if _TIER_ORDER.index(a) >= _TIER_ORDER.index(b) else b


class ExperienceOutcome(NamedTuple):
    tier_self_reported: ExperienceTier
    tier_verified: ExperienceTier | None
    tier: ExperienceTier  # tier used for the caps lookup
    override_applied: bool  # the repeat-borrower override raised the tier
    flags: list[Flag]


def experience_check(borrower: BorrowerInputs, config: Config) -> ExperienceOutcome:
    """Tier from the verified count when known (else self-reported), mismatch flag, and the
    repeat-borrower override: a Mortgage Automator match with clean payoff history lifts the
    tier to at least ``experience.repeat_borrower_min_tier``.  # SPEC §7.3, §7.5
    """
    bucket = borrower.experience_bucket_self_reported
    tier_self = tier_from_bucket(bucket)
    tier_verified: ExperienceTier | None = None
    flags: list[Flag] = []
    if borrower.verified_deals_36mo is not None:
        tier_verified = tier_from_verified_deals(borrower.verified_deals_36mo)
        if tier_verified is not tier_self:
            flags.append(
                Flag(
                    code=ScreenFlag.EXPERIENCE_MISMATCH,
                    severity=Severity.SOFT,
                    message=(
                        f"Verified {borrower.verified_deals_36mo} deal(s) in the last 36 months "
                        f"({tier_verified.value}) differs from self-reported bucket "
                        f"{bucket.value} ({tier_self.value})."
                    ),
                )
            )
    tier = tier_verified if tier_verified is not None else tier_self

    status = borrower.repeat_borrower_verified
    minimum = config.experience.repeat_borrower_min_tier
    applied = False
    if status is RepeatBorrowerStatus.CLEAN:
        raised = higher_tier(tier, minimum)
        applied = raised is not tier
        detail = (
            f"experience tier raised from {tier.value} to {raised.value}"
            if applied
            else f"tier {tier.value} already at or above the minimum"
        )
        flags.append(
            Flag(
                code=ScreenFlag.REPEAT_BORROWER_OVERRIDE_APPLIED,
                severity=Severity.INFO,
                message=(
                    f"Repeat GLENWOOD borrower with clean payoff history: {detail} "
                    f"(config minimum {minimum.value})."
                ),
            )
        )
        tier = raised
    elif status is RepeatBorrowerStatus.NOT_CLEAN:
        flags.append(
            Flag(
                code=ScreenFlag.REPEAT_BORROWER_PAYOFF_NOT_CLEAN,
                severity=Severity.INFO,
                message=(
                    "Mortgage Automator match found but prior payoff history is not clean; "
                    "no experience override applied."
                ),
            )
        )
    elif status is RepeatBorrowerStatus.NO_MATCH and borrower.repeat_borrower_self_reported:
        flags.append(
            Flag(
                code=ScreenFlag.REPEAT_BORROWER_MISMATCH,
                severity=Severity.SOFT,
                message=(
                    "Self-reported repeat borrower but no Mortgage Automator match; "
                    "no experience override applied."
                ),
            )
        )
    elif status is None and borrower.repeat_borrower_self_reported:
        flags.append(
            Flag(
                code=ScreenFlag.REPEAT_BORROWER_UNVERIFIED,
                severity=Severity.INFO,
                message=(
                    "Self-reported repeat borrower not yet verified against Mortgage Automator; "
                    "no experience override applied."
                ),
            )
        )
    return ExperienceOutcome(tier_self, tier_verified, tier, applied, flags)


# --- court and filing flags (SPEC §7.2) ---------------------------------------------------------


def years_before(day: date, years: int) -> date:
    """``day`` minus ``years`` calendar years; Feb 29 falls back to Feb 28."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def within_lookback(event: date, as_of: date, years: int) -> bool:
    """True when ``event`` is on or after ``as_of`` minus ``years``; future dates count."""
    return event >= years_before(as_of, years)


def _court_flag(code: CourtFlag, message: str, config: Config) -> Flag:
    return Flag(code=code, severity=config.flags.severities[code], message=message)


def court_flags(records: CourtRecordInputs | None, config: Config) -> list[Flag]:
    """Evaluate the SPEC §7.2 table with severities from config.

    Unsatisfied judgments are summed across matters and tested against the aggregate
    threshold (one flag). Any open tax lien is flagged regardless of amount (one flag naming
    the total). Bankruptcies, satisfied judgments, active litigation and subject-property
    liens produce one flag per matching matter. Lookbacks
    run against ``records.as_of``. ``None`` records mean no source was checked, which is
    reported as an INFO flag rather than treated as clean.
    """
    if records is None:
        return [
            Flag(
                code=ScreenFlag.COURT_RECORDS_NOT_CHECKED,
                severity=Severity.INFO,
                message="Court and filing records not checked; no court flags evaluated.",
            )
        ]
    lookbacks = config.flags.lookbacks
    thresholds = config.flags.thresholds
    flags: list[Flag] = []

    since = years_before(records.as_of, lookbacks.bankruptcy_years)
    for filed in records.bankruptcy_filing_dates:
        if within_lookback(filed, records.as_of, lookbacks.bankruptcy_years):
            flags.append(
                _court_flag(
                    CourtFlag.BANKRUPTCY_IN_LOOKBACK,
                    f"Bankruptcy filed {filed.isoformat()} is within the "
                    f"{lookbacks.bankruptcy_years}-year lookback "
                    f"(on or after {since.isoformat()}).",
                    config,
                )
            )
    if records.active_foreclosure_as_owner:
        flags.append(
            _court_flag(
                CourtFlag.ACTIVE_FORECLOSURE_AS_OWNER,
                "Active foreclosure as owner on record (any active foreclosure is flagged).",
                config,
            )
        )
    judgments_total = sum(records.unsatisfied_judgments_usd, Decimal(0))
    if judgments_total > thresholds.unsatisfied_judgments_aggregate_usd:
        flags.append(
            _court_flag(
                CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD,
                f"Unsatisfied judgments total {money(judgments_total)} across "
                f"{len(records.unsatisfied_judgments_usd)} matter(s), exceeding the "
                f"{money(thresholds.unsatisfied_judgments_aggregate_usd)} aggregate threshold.",
                config,
            )
        )
    if records.open_tax_liens_usd:
        liens_total = sum(records.open_tax_liens_usd, Decimal(0))
        flags.append(
            _court_flag(
                CourtFlag.OPEN_TAX_LIEN,
                f"Open tax liens total {money(liens_total)} across "
                f"{len(records.open_tax_liens_usd)} matter(s) (any open tax lien is flagged, "
                "regardless of amount).",
                config,
            )
        )
    for amount in records.active_civil_litigation_as_defendant_usd:
        if amount > thresholds.active_civil_litigation_usd:
            flags.append(
                _court_flag(
                    CourtFlag.ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT,
                    f"Active civil litigation as defendant for {money(amount)} exceeds the "
                    f"{money(thresholds.active_civil_litigation_usd)} threshold.",
                    config,
                )
            )
    since = years_before(records.as_of, lookbacks.satisfied_judgment_years)
    for dated in records.satisfied_judgment_or_released_lien_dates:
        if within_lookback(dated, records.as_of, lookbacks.satisfied_judgment_years):
            flags.append(
                _court_flag(
                    CourtFlag.SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK,
                    f"Satisfied judgment or released lien dated {dated.isoformat()} is within "
                    f"the {lookbacks.satisfied_judgment_years}-year lookback "
                    f"(on or after {since.isoformat()}).",
                    config,
                )
            )
    if records.landlord_tenant_matters_as_landlord > 0:
        count = records.landlord_tenant_matters_as_landlord
        flags.append(
            _court_flag(
                CourtFlag.LANDLORD_TENANT_AS_LANDLORD,
                f"{count} landlord-tenant matter(s) as landlord on record "
                "(informational; no threshold).",
                config,
            )
        )
    for lien in records.subject_property_liens:
        if lien.senior and not lien.resolved_at_close:
            what = "lis pendens" if lien.kind is LienKind.LIS_PENDENS else "lien"
            amount_text = f" of {money(lien.amount_usd)}" if lien.amount_usd is not None else ""
            flags.append(
                _court_flag(
                    CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS,
                    f"Senior {what}{amount_text} on the subject property unresolved at close "
                    "(flagged when senior and unresolved at close).",
                    config,
                )
            )
    return flags


# --- state and leverage (SPEC §7.4, §7.5) -------------------------------------------------------


def state_flags(state: State, config: Config) -> list[Flag]:
    """SOFT flag when the property is outside ``states.served``.  # SPEC §7.5"""
    if state in config.states.served:
        return []
    served = ", ".join(s.value for s in config.states.served)
    return [
        Flag(
            code=ScreenFlag.STATE_NOT_SERVED,
            severity=Severity.SOFT,
            message=f"Property state {state.value} is outside the served states ({served}).",
        )
    ]


def leverage_flags(sizing: SizingResult) -> list[Flag]:
    """Cap breaches (HARD beyond the band, SOFT within), missing values, and the loan split.

    # SPEC §7.4, §7.5, §8.2
    """
    flags: list[Flag] = []
    cell = f"{sizing.product.value}/{sizing.credit_tranche.value}/{sizing.experience_tier.value}"
    if sizing.ltv_basis is ValueBasis.PURCHASE_PRICE:
        ltv = sizing.metrics[LeverageMetric.LTV_AS_IS]
        flags.append(
            Flag(
                code=ScreenFlag.AS_IS_VALUE_MISSING,
                severity=Severity.SOFT,
                message=(
                    "As-is value unavailable; LTV computed on the purchase price instead "
                    f"(cap {pct(ltv.cap)} for {cell})."
                ),
            )
        )
    split = sizing.split
    if split is not None and split.rehab_portion_capped:
        # What the cap does next differs by product, and a reader wants the consequence as
        # much as the fact: SPLIT_PRINCIPAL loses the difference off the commitment, which
        # COMMITMENT_BELOW_REQUEST then reports; SPLIT_DRAW keeps it and advances it at
        # close, so only the timing of the money moves.
        consequence = (
            "Tranche A is capped at the budget, so the commitment falls below the request"
            if sizing.product is Product.SPLIT_PRINCIPAL
            else "the holdback is capped at the budget and the difference is advanced at close"
        )
        flags.append(
            Flag(
                code=ScreenFlag.REHAB_PORTION_EXCEEDS_BUDGET,
                severity=Severity.INFO,
                message=(
                    f"{sizing.product.value} rehab portion "
                    f"{money(split.rehab_portion_requested)} exceeds the "
                    f"contingency-adjusted rehab budget {money(sizing.rehab_adj)}; "
                    f"{consequence}."
                ),
            )
        )
    if sizing.product is Product.SPLIT_PRINCIPAL and sizing.commitment < sizing.loan_requested:
        notes = (
            f" (Principal Note {money(split.purchase_portion)} + "
            f"Tranche A {money(split.rehab_portion)})"
            if split is not None
            else ""
        )
        flags.append(
            Flag(
                code=ScreenFlag.COMMITMENT_BELOW_REQUEST,
                severity=Severity.INFO,
                message=(
                    f"SPLIT_PRINCIPAL commitment {money(sizing.commitment)}{notes} is below the "
                    f"{money(sizing.loan_requested)} requested: the entered rehab portion is "
                    f"capped at the contingency-adjusted rehab budget {money(sizing.rehab_adj)}."
                ),
            )
        )
    for metric, check in sizing.metrics.items():
        if check.status is CapStatus.NOT_AVAILABLE:
            if metric is LeverageMetric.LTARV:
                flags.append(
                    Flag(
                        code=ScreenFlag.ARV_MISSING,
                        severity=Severity.SOFT,
                        message=(
                            f"ARV unavailable; LTARV not computed "
                            f"(cap {pct(check.cap)} for {cell})."
                        ),
                    )
                )
            continue
        if check.status is CapStatus.PASS or check.actual is None:
            continue
        label = _METRIC_LABEL[metric]
        if check.basis is ValueBasis.PURCHASE_PRICE:
            label = "LTV (on purchase price)"
        limit = check.cap + check.tolerance_band
        if check.status is CapStatus.FAIL:
            severity = Severity.HARD
            message = (
                f"{label} {pct(check.actual)} exceeds the {pct(check.cap)} cap for {cell} by more "
                f"than the {points(check.tolerance_band)} tolerance band (limit {pct(limit)})."
            )
        else:
            severity = Severity.SOFT
            message = (
                f"{label} {pct(check.actual)} is over the {pct(check.cap)} cap for {cell} but "
                f"within the {points(check.tolerance_band)} tolerance band (limit {pct(limit)})."
            )
        flags.append(Flag(code=_METRIC_FLAG[metric], severity=severity, message=message))
    return flags


# --- provenance (SPEC §6.1) ---------------------------------------------------------------------


def team_sourced_flags(sizing: SizingResult, court_records: CourtRecordInputs | None) -> list[Flag]:
    """TEAM_SOURCED_VALUES when a value the engine ran on was entered by hand.  # SPEC §6.1

    Fixed INFO, and it never moves a verdict: a hand-entered value is a real input, not a
    defect. But a Go resting on the team's own valuation and the team's own court search is
    a different thing from a Go resting on a pull, and neither the verdict nor the numbers
    say which it is. The message names which values it was, so the reason survives into the
    credit memo (SPEC §9.2) where a reader is deciding how much weight to put on it.

    Both stages pass their own court record: the underwrite re-runs the SPEC §7.2 tests on
    whatever source is in force at underwrite time (SPEC §8.1), so a hand search can be named
    there too - and named as the team's only if no adapter has superseded it by then.
    """
    entered = [
        label
        for label, source in (
            ("as-is value", sizing.as_is_value_source),
            ("ARV", sizing.arv_source),
            ("court records", court_records.source if court_records is not None else None),
        )
        if source is ValueSource.TEAM
    ]
    if not entered:
        return []
    named = _join(entered)
    return [
        Flag(
            code=ScreenFlag.TEAM_SOURCED_VALUES,
            severity=Severity.INFO,
            message=(
                f"{named[0].upper()}{named[1:]} came from the team, entered by hand rather "
                "than pulled from an adapter; an adapter value would take precedence."
            ),
        )
    ]


# --- verdict, reasons, reply (SPEC §7.5) --------------------------------------------------------


def order_flags(flags: list[Flag]) -> list[Flag]:
    """HARD, then SOFT, then INFO; stable within a severity."""
    return sorted(flags, key=lambda f: _SEVERITY_RANK[f.severity])


def verdict_from_flags(flags: list[Flag]) -> Verdict:
    """Any HARD -> Decline; else any SOFT -> Conditional; else Go.  # SPEC §7.5"""
    severities = {flag.severity for flag in flags}
    if Severity.HARD in severities:
        return Verdict.DECLINE
    if Severity.SOFT in severities:
        return Verdict.CONDITIONAL
    return Verdict.GO


def reasons_from_flags(flags: list[Flag]) -> list[str]:
    """Plain-language reasons for the team, one per flag, in severity order.  # SPEC §7.5"""
    return [f"{flag.severity.value.title()}: {flag.message}" for flag in order_flags(flags)]


def suggested_reply(verdict: Verdict, flags: list[Flag], config: Config) -> str:
    """Draft reply for a human to send; names only what the team still needs.  # SPEC §4.3, §7.5"""
    if verdict is Verdict.DECLINE:
        return _REPLY_DECLINE
    if verdict is Verdict.GO:
        return _REPLY_GO
    served = ", ".join(s.value for s in config.states.served)
    asks: list[str] = []
    for flag in order_flags(flags):
        if flag.severity is not Severity.SOFT:
            continue
        if isinstance(flag.code, CourtFlag):
            ask = _ASK_COURT
        elif isinstance(flag.code, ScreenFlag):
            ask = _ASK_BY_FLAG.get(flag.code, _ASK_DEFAULT).format(served=served)
        else:
            ask = _ASK_DEFAULT
        if ask not in asks:
            asks.append(ask)
    return (
        "Thanks - this looks workable on first pass. To move forward we need "
        f"{_join(asks or [_ASK_DEFAULT])}. Once we have that we can get you a quick answer."
    )


# --- entry point ---------------------------------------------------------------------------------


def screen(inputs: ScreenInputs, config: Config) -> ScreenResult:
    """Run the Stage 1 screen and return the verdict with its reasons and components.  # SPEC §7"""
    credit = credit_check(inputs.borrower, config)
    experience = experience_check(inputs.borrower, config)
    sizing = size_deal(inputs.deal, credit.tranche, experience.tier, config)
    flags = order_flags(
        credit.flags
        + experience.flags
        + court_flags(inputs.court_records, config)
        + state_flags(inputs.state, config)
        + leverage_flags(sizing)
        + team_sourced_flags(sizing, inputs.court_records)
    )
    verdict = verdict_from_flags(flags)
    return ScreenResult(
        engine_version=ENGINE_VERSION,
        config_hash=config.config_hash,
        verdict=verdict,
        reasons=reasons_from_flags(flags),
        flags=flags,
        suggested_reply=suggested_reply(verdict, flags, config),
        components=ScreenComponents(
            credit_tranche=credit.tranche,
            credit_tranche_verified=credit.verified,
            floor_tranche=config.credit.floor_tranche,
            credit_meets_floor=not is_below_floor(credit.tranche, config),
            experience_tier_self_reported=experience.tier_self_reported,
            experience_tier_verified=experience.tier_verified,
            repeat_borrower_override_applied=experience.override_applied,
            experience_tier=experience.tier,
            court_records_source=(
                inputs.court_records.source if inputs.court_records is not None else None
            ),
        ),
        sizing=sizing,
    )
