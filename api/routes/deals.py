"""Deal endpoints: run the screen, run the underwrite, read the deal back.  # SPEC §7, §8

Both POSTs append a row (SPEC §5), move the deal one step along the lifecycle (SPEC §4.5)
and return the engine result as it was recorded. The GET returns the deal with the latest of
each, rebuilt from those rows rather than recomputed, so what the team reads is exactly what
was stored - engine version, config hash and all.

Three failures are distinguished, because the fix for each is different: 404 the deal does
not exist, 422 it exists but is missing values the engine needs (named, so the queue can
chase them), 409 the inputs are fine but its status rules the run out - including the case
where the underwrite screened an unscreened deal first and that screen declined it. A fourth
sits in front of all of them: 401 when there is no session cookie, because every run is
recorded against the person who asked for it (SPEC §11).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from api.security import ApiUser
from db.models import Deal
from db.session import get_session
from schema.models import (
    AssetType,
    Channel,
    CourtRecordsStatus,
    ExperienceBucket,
    Product,
    ProductSource,
    ScreenResult,
    State,
    StatedExit,
    Status,
    TeamCourtRecord,
    TermBucket,
    Tranche,
    UnderwriteResult,
)
from services import (
    DealNotFound,
    DealNotReady,
    DealNotUnderwritable,
    UnderwriteRequest,
    latest_screen,
    latest_underwrite,
    load_deal,
    run_screen,
    run_underwrite,
    screen_result,
    underwrite_result,
)

router = APIRouter(prefix="/deals", tags=["deals"])

SessionDep = Annotated[Session, Depends(get_session)]


class BorrowerView(BaseModel):
    """The borrower rows a team member needs on the deal page."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    entities: list[str] = Field(default_factory=list)


class PropertyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address_raw: str | None = None
    address_normalized: str | None = None
    listing_url: str | None = None
    county: str | None = None
    state: State


class ScreenRecord(BaseModel):
    """One ``screens`` row: when it ran and the result it recorded."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    created_at: datetime
    result: ScreenResult


class UnderwriteRecord(BaseModel):
    """One ``underwrites`` row: when it ran and the result it recorded."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    created_at: datetime
    result: UnderwriteResult


class DealView(BaseModel):
    """A deal with its latest screen and underwrite; each None until it has run."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    created_at: datetime
    updated_at: datetime
    channel: Channel
    status: Status
    missing_fields: list[str]
    credit_authorization_signed: bool
    product: Product | None
    product_source: ProductSource | None
    purchase_price: Decimal | None
    rehab_budget: Decimal | None
    loan_requested: Decimal | None
    term_bucket: TermBucket | None
    asset_type: AssetType | None
    stated_exit: StatedExit | None
    credit_range_self_reported: Tranche | None
    experience_bucket_self_reported: ExperienceBucket | None
    repeat_borrower_self_reported: bool | None
    actual_annual_taxes_usd: Decimal | None
    actual_annual_insurance_usd: Decimal | None
    actual_annual_utilities_usd: Decimal | None
    market_rent_monthly: Decimal | None
    as_is_value_team: Decimal | None
    arv_team: Decimal | None
    court_records_status: CourtRecordsStatus | None
    court_records_as_of: date | None
    court_records_team: list[TeamCourtRecord]
    borrower: BorrowerView | None
    property: PropertyView | None
    screen: ScreenRecord | None
    underwrite: UnderwriteRecord | None


def _not_found(exc: DealNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _not_ready(exc: DealNotReady) -> HTTPException:
    """422 with the list of values the deal still needs, so the queue can show them."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"message": str(exc), "missing": exc.missing},
    )


def _not_underwritable(exc: DealNotUnderwritable) -> HTTPException:
    """409: the deal exists and the inputs are fine, its status is what refuses.  # SPEC §4.5"""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"message": str(exc), "status": exc.status.value},
    )


@router.post("/{deal_id}/screen", response_model=ScreenResult, status_code=status.HTTP_201_CREATED)
def screen_deal(deal_id: UUID, session: SessionDep, user: ApiUser) -> ScreenResult:
    """Screen the deal, append a ``screens`` row, return the verdict.  # SPEC §7"""
    try:
        result = run_screen(session, deal_id, actor=user.email)
    except DealNotFound as exc:
        raise _not_found(exc) from exc
    except DealNotReady as exc:
        raise _not_ready(exc) from exc
    session.commit()
    return result


@router.post(
    "/{deal_id}/underwrite",
    response_model=UnderwriteResult,
    status_code=status.HTTP_201_CREATED,
)
def underwrite_deal(
    deal_id: UUID, request: UnderwriteRequest, session: SessionDep, user: ApiUser
) -> UnderwriteResult:
    """Underwrite the deal on the §8.1 inputs, append an ``underwrites`` row.  # SPEC §8"""
    try:
        result = run_underwrite(session, deal_id, request, actor=user.email)
    except DealNotFound as exc:
        raise _not_found(exc) from exc
    except DealNotUnderwritable as exc:
        # An unscreened deal is screened first (SPEC §8), so a refusal here can arrive with
        # a real screen behind it: committed, because it ran and its verdict is why the
        # underwrite stopped. Rolling it back would leave the deal looking unscreened and
        # let the next request screen it again. When nothing ran, this commits nothing.
        session.commit()
        raise _not_underwritable(exc) from exc
    except DealNotReady as exc:
        raise _not_ready(exc) from exc
    session.commit()
    return result


def _borrower_view(deal: Deal) -> BorrowerView | None:
    if deal.borrower is None:
        return None
    return BorrowerView(
        name=deal.borrower.name,
        phone=deal.borrower.phone,
        email=deal.borrower.email,
        entities=[entity.name for entity in deal.borrower.entities],
    )


def _property_view(deal: Deal) -> PropertyView | None:
    if deal.property is None:
        return None
    return PropertyView(
        address_raw=deal.property.address_raw,
        address_normalized=deal.property.address_normalized,
        listing_url=deal.property.listing_url,
        county=deal.property.county,
        state=deal.property.state,
    )


def deal_view(deal: Deal, session: Session) -> DealView:
    """Assemble the read model, rebuilding each stored result from its own row."""
    screen_row = latest_screen(session, deal.id)
    underwrite_row = latest_underwrite(session, deal.id)
    return DealView(
        id=deal.id,
        created_at=deal.created_at,
        updated_at=deal.updated_at,
        channel=deal.channel,
        status=deal.status,
        missing_fields=list(deal.missing_fields),
        credit_authorization_signed=deal.credit_authorization_signed,
        product=deal.product,
        product_source=deal.product_source,
        purchase_price=deal.purchase_price,
        rehab_budget=deal.rehab_budget,
        loan_requested=deal.loan_requested,
        term_bucket=deal.term_bucket,
        asset_type=deal.asset_type,
        stated_exit=deal.stated_exit,
        credit_range_self_reported=deal.credit_range_self_reported,
        experience_bucket_self_reported=deal.experience_bucket_self_reported,
        repeat_borrower_self_reported=deal.repeat_borrower_self_reported,
        actual_annual_taxes_usd=deal.actual_annual_taxes_usd,
        actual_annual_insurance_usd=deal.actual_annual_insurance_usd,
        actual_annual_utilities_usd=deal.actual_annual_utilities_usd,
        market_rent_monthly=deal.market_rent_monthly,
        as_is_value_team=deal.as_is_value_team,
        arv_team=deal.arv_team,
        court_records_status=deal.court_records_status,
        court_records_as_of=deal.court_records_as_of,
        court_records_team=[
            TeamCourtRecord.model_validate(matter) for matter in deal.court_records_team
        ],
        borrower=_borrower_view(deal),
        property=_property_view(deal),
        screen=(
            None
            if screen_row is None
            else ScreenRecord(
                id=screen_row.id,
                created_at=screen_row.created_at,
                result=screen_result(screen_row),
            )
        ),
        underwrite=(
            None
            if underwrite_row is None
            else UnderwriteRecord(
                id=underwrite_row.id,
                created_at=underwrite_row.created_at,
                result=underwrite_result(underwrite_row),
            )
        ),
    )


@router.get("/{deal_id}", response_model=DealView)
def read_deal(deal_id: UUID, session: SessionDep, user: ApiUser) -> DealView:
    """The deal with its latest screen and underwrite.  # SPEC §9.1"""
    del user  # the dependency is the access check; the read itself needs no actor
    try:
        deal = load_deal(session, deal_id)
    except DealNotFound as exc:
        raise _not_found(exc) from exc
    return deal_view(deal, session)
