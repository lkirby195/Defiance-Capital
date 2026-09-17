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

from db.models import Deal
from schema.models import (
    BorrowerInputs,
    ExperienceBucket,
    Product,
    ScreenInputs,
    SizingInputs,
    State,
    StatedExit,
    TermBucket,
    Tranche,
    UnderwriteInputs,
)
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
    """Narrow an optional column to its type; ``deal_core`` has already checked it."""
    if value is None:  # pragma: no cover - unreachable once the missing list is empty
        raise ValueError("required value is missing")
    return value


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


def sizing_inputs(
    core: DealCore,
    as_is_value: Decimal | None = None,
    arv: Decimal | None = None,
    purchase_portion_override: Decimal | None = None,
) -> SizingInputs:
    """The deal numbers; ``as_is_value`` / ``arv`` are None until a valuation exists."""
    return SizingInputs(
        product=core.product,
        purchase_price=core.purchase_price,
        rehab_budget=core.rehab_budget,
        loan_requested=core.loan_requested,
        as_is_value=as_is_value,
        arv=arv,
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


def screen_inputs(deal: Deal) -> ScreenInputs:
    """Assemble ``ScreenInputs`` from the deal.  # SPEC §7

    No paid pulls at the screen, and the enrichment adapters are Phase 3, so there is no
    valuation and no court record yet: the as-is value, the ARV, and the court records all
    go in as None. The screen turns each of those into its own flag (SPEC §7.4, §7.5)
    rather than guessing, which is why a screen run today lands at Conditional at best.
    """
    core = deal_core(deal)
    return ScreenInputs(
        deal=sizing_inputs(core),
        state=core.state,
        borrower=borrower_inputs(core),
        court_records=None,
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


def underwrite_inputs(deal: Deal, request: UnderwriteRequest) -> UnderwriteInputs:
    """Assemble ``UnderwriteInputs`` from the deal plus the team's §8.1 additions.

    Taxes and insurance fall back deal-ward, not config-ward: the request wins, then the
    team actuals on the deal, then None - which is the engine's signal to use the config
    default as a percentage of the as-is value (SPEC §8.6).
    """
    core = deal_core(deal)
    taxes = (
        request.annual_taxes_usd
        if request.annual_taxes_usd is not None
        else deal.actual_annual_taxes_usd
    )
    insurance = (
        request.annual_insurance_usd
        if request.annual_insurance_usd is not None
        else deal.actual_annual_insurance_usd
    )
    return UnderwriteInputs(
        deal=sizing_inputs(
            core,
            as_is_value=request.as_is_value,
            arv=request.arv,
            purchase_portion_override=request.purchase_portion_override,
        ),
        state=core.state,
        borrower=borrower_inputs(core, request),
        term_months=resolve_term_months(deal, request),
        market_rent_monthly=request.market_rent_monthly,
        annual_taxes_usd=taxes,
        annual_insurance_usd=insurance,
        annual_utilities_usd=request.annual_utilities_usd,
        extension_fee_pct=request.extension_fee_pct,
        exit_price=request.exit_price,
        asset_type=request.asset_type or deal.asset_type,
        stated_exit=request.stated_exit or deal.stated_exit or StatedExit.UNKNOWN,
    )
