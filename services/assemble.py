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
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from config.config import Config, get_config
from db.models import Deal
from engine.calc.exit import flip_default, infer_exit, rental_default
from schema.models import (
    AMOUNT_COURT_FLAGS,
    DATED_COURT_FLAGS,
    SPLIT_PRODUCTS,
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
    Tranche,
    UnderwriteInputs,
    ValueSource,
)
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues
from services.errors import DealNotReady
from services.requests import UnderwriteRequest

# What the engine needs from the deal itself, named as the team would chase them.
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("product", "deal.product"),
    ("purchase_price", "deal.purchase_price"),
    ("rehab_costs", "deal.rehab_costs"),
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
    rehab_costs: Decimal
    loan_requested: Decimal
    loan_purchase_portion: Decimal | None
    loan_rehab_portion: Decimal | None
    contingency_pct: Decimal | None
    closing_costs_usd: Decimal | None
    credit_range: Tranche
    experience_bucket: ExperienceBucket
    repeat_borrower: bool
    state: State


def intake_gaps(deal: Deal) -> list[str]:
    """The engine-required intake values this deal is still without, named as the team
    would chase them.

    Read by ``deal_core`` to refuse a run and by ``services/readiness.py`` to say in advance
    why the button is off, so the page and the refusal cannot name different things.
    """
    return [name for attr, name in _REQUIRED if getattr(deal, attr) is None]


def deal_core(deal: Deal) -> DealCore:
    """The engine-required values, or ``DealNotReady`` naming every one that is absent.

    The property's state is OTHER until a property row exists, which the screen flags
    (SPEC §7.5) rather than treating as a missing input.
    """
    missing = intake_gaps(deal)
    if missing:
        raise DealNotReady(deal.id, missing)
    return DealCore(
        product=_present(deal.product),
        purchase_price=_present(deal.purchase_price),
        rehab_costs=_present(deal.rehab_costs),
        loan_requested=_present(deal.loan_requested),
        loan_purchase_portion=deal.loan_purchase_portion,
        loan_rehab_portion=deal.loan_rehab_portion,
        contingency_pct=deal.contingency_pct,
        closing_costs_usd=deal.closing_costs_usd,
        credit_range=_present(deal.credit_range_self_reported),
        experience_bucket=_present(deal.experience_bucket_self_reported),
        repeat_borrower=_present(deal.repeat_borrower_self_reported),
        state=deal.property.state if deal.property is not None else State.OTHER,
    )


class Valuation(NamedTuple):
    """The valuation the engine will run on, and where each half of it came from."""

    as_is_value: Decimal | None
    as_is_value_source: ValueSource | None
    estimated_sale_price: Decimal | None
    estimated_sale_price_source: ValueSource | None


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
    ``as_is_value_team`` and ``estimated_sale_price_team`` survive a run that did not use
    them.
    """
    team_as_is = deal.as_is_value_team
    team_sale = deal.estimated_sale_price_team
    if request is not None:
        team_as_is = request.as_is_value if request.as_is_value is not None else team_as_is
        team_sale = (
            request.estimated_sale_price if request.estimated_sale_price is not None else team_sale
        )
    as_is_value, as_is_source = resolve_one(adapters.as_is_value, team_as_is)
    sale_price, sale_source = resolve_one(adapters.estimated_sale_price, team_sale)
    return Valuation(as_is_value, as_is_source, sale_price, sale_source)


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
    request: UnderwriteRequest | None = None,
) -> SizingInputs:
    """The deal numbers plus the valuation in force and where each half of it came from.

    The loan split travels with the deal (SPEC §8.2) rather than being handed in at run
    time: it is a description of the loan, not a judgement made at pricing. Both halves are
    None on a deal nobody has divided, which the screen sizes without a split and the
    underwrite refuses (SPEC §8.1).

    The contingency and the lender's closing costs are here because both stages size on
    them (SPEC §8.2): a request may carry a newer number than the deal does, and None at the
    end of both means the config default.
    """
    return SizingInputs(
        product=core.product,
        purchase_price=core.purchase_price,
        rehab_costs=core.rehab_costs,
        loan_requested=core.loan_requested,
        loan_purchase_portion=core.loan_purchase_portion,
        loan_rehab_portion=core.loan_rehab_portion,
        contingency_pct=_first(request.contingency_pct if request else None, core.contingency_pct),
        closing_costs_usd=_first(
            request.closing_costs_usd if request else None, core.closing_costs_usd
        ),
        as_is_value=valuation.as_is_value,
        as_is_value_source=valuation.as_is_value_source,
        estimated_sale_price=valuation.estimated_sale_price,
        estimated_sale_price_source=valuation.estimated_sale_price_source,
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


def resolve_closing_date(deal: Deal, request: UnderwriteRequest) -> date | None:
    """The request's closing date, else the one on the deal.  # SPEC §8.1"""
    return request.closing_date if request.closing_date is not None else deal.closing_date


def resolve_term_months(deal: Deal, request: UnderwriteRequest) -> int | None:
    """The request's term, else the one on the deal.  # SPEC §8.1

    A request may name the term as a payoff date instead, which ``UnderwriteRequest``
    converts against its own closing date; the column is otherwise the term, seeded from the
    bucket at intake and free to differ from it. None means nobody has set one at all.
    """
    requested = request.requested_term_months
    return requested if requested is not None else deal.term_months


def resolved_exit(
    deal: Deal, request: UnderwriteRequest, term_months: int | None, config: Config | None = None
) -> StatedExit:
    """The §3 exit this deal would be underwritten under, as far as it can be known here.

    UNKNOWN when there is no term or no product yet, which is exactly what the inference
    would say about a deal it cannot place. The engine runs the same ``infer_exit``; this is
    here so the readiness checklist and the assembly's own refusal agree with it in advance
    about which analyses are on (SPEC §8.1).
    """
    if term_months is None or deal.product is None:
        return StatedExit.UNKNOWN
    exit_type, _ = infer_exit(
        _first(request.stated_exit, deal.stated_exit) or StatedExit.UNKNOWN,
        _first(request.asset_type, deal.asset_type),
        term_months,
        deal.product,
        config if config is not None else get_config(),
    )
    return exit_type


def flip_is_on(
    deal: Deal, request: UnderwriteRequest, term_months: int | None, config: Config | None = None
) -> bool:
    """Whether the Flip analysis would run, which decides if a sale price is required.

    # SPEC §8.1. A toggle set by hand wins; otherwise the §3 exit decides.
    """
    toggle = _first(request.flip_analysis, deal.flip_analysis)
    if toggle is not None:
        return toggle
    return flip_default(resolved_exit(deal, request, term_months, config))


def rental_is_on(
    deal: Deal, request: UnderwriteRequest, term_months: int | None, config: Config | None = None
) -> bool:
    """Whether the Rental analysis would run.  # SPEC §8.1

    Nothing is required by it either way - the Take-Back analysis needs the same rent and
    runs regardless - so this is for the checklist to report rather than for the button.
    """
    toggle = _first(request.rental_analysis, deal.rental_analysis)
    if toggle is not None:
        return toggle
    rent = _first(request.monthly_rent, deal.monthly_rent)
    return rental_default(resolved_exit(deal, request, term_months, config), rent)


def underwrite_inputs(
    deal: Deal, request: UnderwriteRequest, adapters: AdapterValues = NO_ADAPTER_VALUES
) -> UnderwriteInputs:
    """Assemble ``UnderwriteInputs`` from the deal plus the team's §8.1 additions.

    Every optional value falls back the same way: the request, then what is on the deal, then
    the engine's own default. The contingency, the closing costs, the holding costs and the
    origination fee end at None, which is the engine's signal to use the config default
    (SPEC §8.1); the monthly rent ends at None too, which leaves the Rental and Take-Back
    analyses NOT_EVALUATED with an INFO flag rather than computed on a zero.

    Five things have no default and stop the run, and every one that is absent is named at
    once rather than one per attempt:

    * the closing date, the term and the interest rate, because the ledger is dated months of
      interest and none of the three has a defensible stand-in (SPEC §8.3);
    * the loan split on a split product, because SPEC §8.2 advances the two portions
      differently and the draw schedule is one of them;
    * the estimated sale price **while the Flip analysis is on**, because that is what the
      flip sells at (SPEC §8.4). With the toggle off it is optional, and a deal without one
      is sized with LTARV not available, exactly as at the screen.

    The as-is value is no longer one of them: it feeds LTV only, and LTV falls back to the
    purchase price with a flag (SPEC §7.4).

    The court record is resolved here the same way the screen resolves it, adapter over team
    (SPEC §6.1), and not read off the stored screen: the underwrite runs the SPEC §7.2 tests
    again on whatever is in force now, which may be a pull that landed after Stage 1.
    """
    core = deal_core(deal)
    valuation = resolve_valuation(deal, adapters, request)
    closing_date = resolve_closing_date(deal, request)
    term_months = resolve_term_months(deal, request)
    interest_rate = _first(request.interest_rate, deal.interest_rate)
    needed: list[tuple[str, object | None, str]] = [
        ("deal.closing_date", closing_date, "month 0 of the ledger (SPEC §8.3)"),
        (
            "deal.term_months",
            term_months,
            "the ledger runs closing to payoff; enter a term or a payoff date (SPEC §8.1)",
        ),
        ("deal.interest_rate", interest_rate, "the ledger's interest rows (SPEC §8.3)"),
    ]
    if core.product in SPLIT_PRODUCTS:
        split_why = f"a {core.product.value} loan is advanced in two parts (SPEC §8.2)"
        needed += [
            ("deal.loan_purchase_portion", core.loan_purchase_portion, split_why),
            ("deal.loan_rehab_portion", core.loan_rehab_portion, split_why),
        ]
    if flip_is_on(deal, request, term_months):
        needed.append(
            (
                "estimated_sale_price",
                valuation.estimated_sale_price,
                "the Flip analysis sells at it (SPEC §8.4); turn the toggle off to run without",
            )
        )
    missing = [f"{name} ({why})" for name, value, why in needed if value is None]
    if missing:
        raise DealNotReady(deal.id, missing)
    return UnderwriteInputs(
        deal=sizing_inputs(core, valuation, request),
        state=core.state,
        borrower=borrower_inputs(core, request),
        closing_date=_present(closing_date),
        term_months=_present(term_months),
        interest_rate=_present(interest_rate),
        origination_fee_pct=_first(request.origination_fee_pct, deal.origination_fee_pct),
        holding_costs_total_usd=_first(
            request.holding_costs_total_usd, deal.holding_costs_total_usd
        ),
        monthly_rent=_first(request.monthly_rent, deal.monthly_rent),
        flip_analysis=_first(request.flip_analysis, deal.flip_analysis),
        rental_analysis=_first(request.rental_analysis, deal.rental_analysis),
        loan_purpose=_first(request.loan_purpose, deal.loan_purpose),
        asset_type=_first(request.asset_type, deal.asset_type),
        stated_exit=_first(request.stated_exit, deal.stated_exit) or StatedExit.UNKNOWN,
        court_records=court_records(deal, adapters),
    )
