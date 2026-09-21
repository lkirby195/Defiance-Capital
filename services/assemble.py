"""Turn a stored ``deals`` row into the engine's typed inputs.  # SPEC §7, §8.1

The engine is pure and knows nothing about the database; this module is the seam. It reads
the deal (and its property), fills the gaps from an ``UnderwriteRequest`` where there is
one, and raises ``DealNotReady`` naming every value the engine cannot run without rather
than failing on the first one.

``DealCore`` is the narrow, non-null view of a deal the engine needs. Building it is the
only place a missing value is checked, so the rest of the module works with real types
instead of ``| None`` everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from db.models import Deal
from schema.models import (
    AMOUNT_COURT_FLAGS,
    DATED_COURT_FLAGS,
    BorrowerInputs,
    CourtFlag,
    CourtRecordInputs,
    CourtRecordsStatus,
    ExperienceBucket,
    Product,
    ScreenInputs,
    SizingInputs,
    State,
    StatedExit,
    SubjectPropertyLien,
    TeamCourtRecord,
    TermBucket,
    Tranche,
    UnderwriteInputs,
    ValueSource,
)
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues
from services.errors import DealNotReady
from services.requests import UnderwriteRequest

# Term buckets that name a number of months; 12_PLUS is a team decision (SPEC §8.1).
TERM_BUCKET_MONTHS: dict[TermBucket, int] = {
    TermBucket.M3: 3,
    TermBucket.M6: 6,
    TermBucket.M9: 9,
    TermBucket.M12: 12,
}

# What the engine needs from the deal itself, named as the team would chase them.
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("product", "deal.product"),
    ("purchase_price", "deal.purchase_price"),
    ("rehab_budget", "deal.rehab_budget"),
    ("loan_requested", "deal.loan_requested"),
    ("credit_range_self_reported", "borrower.credit_range"),
    ("experience_bucket_self_reported", "borrower.experience_bucket"),
    ("repeat_borrower_self_reported", "borrower.repeat_borrower"),
)


def _present[T](value: T | None) -> T:
    """Narrow an optional column to its type; the caller has already checked it."""
    if value is None:  # pragma: no cover - unreachable once the missing list is empty
        raise ValueError("required value is missing")
    return value


def _first[T](*candidates: T | None) -> T | None:
    """The first candidate that is not None; None when every one of them is."""
    return next((value for value in candidates if value is not None), None)


@dataclass(frozen=True)
class DealCore:
    """The values on a deal that the engine cannot run without."""

    product: Product
    purchase_price: Decimal
    rehab_budget: Decimal
    loan_requested: Decimal
    credit_range: Tranche
    experience_bucket: ExperienceBucket
    repeat_borrower: bool
    state: State


def deal_core(deal: Deal) -> DealCore:
    """The engine-required values, or ``DealNotReady`` naming every one that is absent.

    The property's state is OTHER until a property row exists, which the screen flags
    (SPEC §7.5) rather than treating as a missing input.
    """
    missing = [name for attr, name in _REQUIRED if getattr(deal, attr) is None]
    if missing:
        raise DealNotReady(deal.id, missing)
    return DealCore(
        product=_present(deal.product),
        purchase_price=_present(deal.purchase_price),
        rehab_budget=_present(deal.rehab_budget),
        loan_requested=_present(deal.loan_requested),
        credit_range=_present(deal.credit_range_self_reported),
        experience_bucket=_present(deal.experience_bucket_self_reported),
        repeat_borrower=_present(deal.repeat_borrower_self_reported),
        state=deal.property.state if deal.property is not None else State.OTHER,
    )


class Valuation(NamedTuple):
    """The as-is value and ARV the engine will run on, and where each came from."""

    as_is_value: Decimal | None
    as_is_value_source: ValueSource | None
    arv: Decimal | None
    arv_source: ValueSource | None


NO_VALUATION = Valuation(None, None, None, None)


def resolve_one(
    adapter: Decimal | None, team: Decimal | None
) -> tuple[Decimal | None, ValueSource | None]:
    """Adapter first, then the team's own number, then nothing.  # SPEC §6"""
    if adapter is not None:
        return adapter, ValueSource.ADAPTER
    if team is not None:
        return team, ValueSource.TEAM
    return None, None


def resolve_valuation(
    deal: Deal,
    adapters: AdapterValues = NO_ADAPTER_VALUES,
    request: UnderwriteRequest | None = None,
) -> Valuation:
    """The valuation in force, adapter over team.  # SPEC §6, §8.1

    A team number reaches the engine two ways and both count as TEAM: the one stored on the
    deal at intake, and the one a team member types when they advance the deal to
    underwrite. The request wins between those two - it is the more recent judgement - and
    an adapter value wins over both. Nothing here writes back to the deal, so
    ``as_is_value_team`` and ``arv_team`` survive a run that did not use them.
    """
    team_as_is = deal.as_is_value_team
    team_arv = deal.arv_team
    if request is not None:
        team_as_is = request.as_is_value if request.as_is_value is not None else team_as_is
        team_arv = request.arv if request.arv is not None else team_arv
    as_is_value, as_is_source = resolve_one(adapters.as_is_value, team_as_is)
    arv, arv_source = resolve_one(adapters.arv, team_arv)
    return Valuation(as_is_value, as_is_source, arv, arv_source)


def team_court_records(deal: Deal) -> CourtRecordInputs | None:
    """The team's own court search as the engine's flag inputs.  # SPEC §6, §7.2

    NOT_CHECKED (and an unset status) returns None, which the screen reports as an INFO flag
    rather than treating as clean. CLEAN returns an empty record dated the day the team
    searched. FLAGS spreads one typed matter per entry across the fields the screen tests,
    so the config thresholds still decide the outcome.
    """
    status = deal.court_records_status
    if status is None or status is CourtRecordsStatus.NOT_CHECKED:
        return None
    as_of = deal.court_records_as_of
    if as_of is None:  # pragma: no cover - the schema and a check constraint both forbid it
        raise DealNotReady(deal.id, ["deal.court_records_as_of"])
    matters = [TeamCourtRecord.model_validate(entry) for entry in deal.court_records_team]
    dated = {code: [m.occurred_on for m in matters if m.code is code] for code in DATED_COURT_FLAGS}
    amounts = {
        code: [m.amount_usd for m in matters if m.code is code] for code in AMOUNT_COURT_FLAGS
    }
    return CourtRecordInputs(
        as_of=as_of,
        source=ValueSource.TEAM,
        bankruptcy_filing_dates=[
            day for day in dated[CourtFlag.BANKRUPTCY_IN_LOOKBACK] if day is not None
        ],
        active_foreclosure_as_owner=any(
            m.code is CourtFlag.ACTIVE_FORECLOSURE_AS_OWNER for m in matters
        ),
        unsatisfied_judgments_usd=[
            amount
            for amount in amounts[CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD]
            if amount is not None
        ],
        open_tax_liens_usd=[
            amount for amount in amounts[CourtFlag.OPEN_TAX_LIEN] if amount is not None
        ],
        active_civil_litigation_as_defendant_usd=[
            amount
            for amount in amounts[CourtFlag.ACTIVE_CIVIL_LITIGATION_AS_DEFENDANT]
            if amount is not None
        ],
        satisfied_judgment_or_released_lien_dates=[
            day
            for day in dated[CourtFlag.SATISFIED_JUDGMENT_OR_RELEASED_LIEN_IN_LOOKBACK]
            if day is not None
        ],
        landlord_tenant_matters_as_landlord=sum(
            1 for m in matters if m.code is CourtFlag.LANDLORD_TENANT_AS_LANDLORD
        ),
        subject_property_liens=[
            SubjectPropertyLien(
                kind=_present(m.lien_kind),
                senior=_present(m.senior),
                resolved_at_close=_present(m.resolved_at_close),
                amount_usd=m.amount_usd,
                description=m.description,
            )
            for m in matters
            if m.code is CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS
        ],
    )


def court_records(
    deal: Deal, adapters: AdapterValues = NO_ADAPTER_VALUES
) -> CourtRecordInputs | None:
    """The court record in force, adapter over team.  # SPEC §6, §7.2"""
    if adapters.court_records is not None:
        return adapters.court_records
    return team_court_records(deal)


def sizing_inputs(
    core: DealCore,
    valuation: Valuation = NO_VALUATION,
    purchase_portion_override: Decimal | None = None,
) -> SizingInputs:
    """The deal numbers plus the valuation in force and where each half of it came from."""
    return SizingInputs(
        product=core.product,
        purchase_price=core.purchase_price,
        rehab_budget=core.rehab_budget,
        loan_requested=core.loan_requested,
        as_is_value=valuation.as_is_value,
        as_is_value_source=valuation.as_is_value_source,
        arv=valuation.arv,
        arv_source=valuation.arv_source,
        purchase_portion_override=purchase_portion_override,
    )


def borrower_inputs(core: DealCore, request: UnderwriteRequest | None = None) -> BorrowerInputs:
    """Self-reported credit and experience, plus whatever the underwrite verified."""
    return BorrowerInputs(
        credit_range_self_reported=core.credit_range,
        experience_bucket_self_reported=core.experience_bucket,
        repeat_borrower_self_reported=core.repeat_borrower,
        verified_credit_score=request.verified_credit_score if request else None,
        verified_deals_36mo=request.verified_deals_36mo if request else None,
        repeat_borrower_verified=request.repeat_borrower_verified if request else None,
    )


def screen_inputs(deal: Deal, adapters: AdapterValues = NO_ADAPTER_VALUES) -> ScreenInputs:
    """Assemble ``ScreenInputs`` from the deal.  # SPEC §7

    No paid pulls at the screen (SPEC §7), so the valuation and the court record are
    whatever enrichment has produced, else whatever the team entered by hand, else nothing.
    Each gap the screen turns into its own flag (SPEC §7.4, §7.5) rather than guessing: a
    deal with no valuation and no court search reaches Conditional at best, and a deal the
    team has valued and searched can reach Go before a single adapter exists.
    """
    core = deal_core(deal)
    return ScreenInputs(
        deal=sizing_inputs(core, resolve_valuation(deal, adapters)),
        state=core.state,
        borrower=borrower_inputs(core),
        court_records=court_records(deal, adapters),
    )


def resolve_term_months(deal: Deal, request: UnderwriteRequest) -> int:
    """The request's term, else the months the deal's bucket names.  # SPEC §8.1

    ``12_PLUS`` names no number, so the team has to set one; so does a deal with no bucket.
    """
    if request.term_months is not None:
        return request.term_months
    months = TERM_BUCKET_MONTHS.get(deal.term_bucket) if deal.term_bucket is not None else None
    if months is None:
        raise DealNotReady(deal.id, ["term_months (the 12_PLUS bucket names no number)"])
    return months


def underwrite_inputs(
    deal: Deal, request: UnderwriteRequest, adapters: AdapterValues = NO_ADAPTER_VALUES
) -> UnderwriteInputs:
    """Assemble ``UnderwriteInputs`` from the deal plus the team's §8.1 additions.

    Every optional value falls back the same way: the request, then what is on the deal,
    then the engine's own default. Taxes and insurance end at None, which is the engine's
    signal to use the config percentage of the as-is value (SPEC §8.6). Three inputs have no
    such default - the two halves of the valuation (SPEC §8.1 requires both), the market rent
    the DSCR takeout is computed on, and the annual utilities - so a deal carrying none of
    them from an adapter, the request or the team is named as not ready rather than
    underwritten on a guess, and every one that is absent is named at once.

    The court record is resolved here the same way the screen resolves it, adapter over team
    (SPEC §6.1), and not read off the stored screen: the underwrite runs the SPEC §7.2 tests
    again on whatever is in force now, which may be a pull that landed after Stage 1.
    """
    core = deal_core(deal)
    taxes = _first(request.annual_taxes_usd, deal.actual_annual_taxes_usd)
    insurance = _first(request.annual_insurance_usd, deal.actual_annual_insurance_usd)
    utilities = _first(request.annual_utilities_usd, deal.actual_annual_utilities_usd)
    market_rent = _first(request.market_rent_monthly, deal.market_rent_monthly)
    valuation = resolve_valuation(deal, adapters, request)
    missing = [
        f"{name} ({why})"
        for name, value, why in (
            (
                "as_is_value",
                valuation.as_is_value,
                "no adapter value, none on the request, none on the deal",
            ),
            ("arv", valuation.arv, "no adapter value, none on the request, none on the deal"),
            ("market_rent_monthly", market_rent, "none on the request, none on the deal"),
            ("annual_utilities_usd", utilities, "none on the request, none on the deal"),
        )
        if value is None
    ]
    if missing:
        raise DealNotReady(deal.id, missing)
    return UnderwriteInputs(
        deal=sizing_inputs(
            core,
            valuation,
            purchase_portion_override=request.purchase_portion_override,
        ),
        state=core.state,
        borrower=borrower_inputs(core, request),
        term_months=resolve_term_months(deal, request),
        market_rent_monthly=_present(market_rent),
        annual_taxes_usd=taxes,
        annual_insurance_usd=insurance,
        annual_utilities_usd=_present(utilities),
        extension_fee_pct=request.extension_fee_pct,
        exit_price=request.exit_price,
        asset_type=request.asset_type or deal.asset_type,
        stated_exit=request.stated_exit or deal.stated_exit or StatedExit.UNKNOWN,
        court_records=court_records(deal, adapters),
    )
