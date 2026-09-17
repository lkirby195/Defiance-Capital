"""Persist an ``IntakeRecord`` as rows in borrowers / entities / properties / deals /
intake_submissions.  # SPEC §5

Borrowers are matched on the normalized phone (the primary match key) and
properties on the normalized address, so repeat inquiries reuse existing rows.
Existing rows are never modified here; the submission row is immutable.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Borrower, Deal, Entity, IntakeSubmission, Property
from schema.models import BorrowerInfo, IntakeRecord, PropertyInfo


def find_or_create_borrower(session: Session, info: BorrowerInfo) -> Borrower | None:
    """Borrower row keyed by phone; None until a phone is known."""
    if not info.phone:
        return None
    borrower = session.scalar(select(Borrower).where(Borrower.phone == info.phone))
    if borrower is None:
        borrower = Borrower(phone=info.phone, name=info.name, email=info.email)
        session.add(borrower)
    if info.entity_name:
        entity = session.scalar(
            select(Entity).where(func.lower(Entity.name) == info.entity_name.lower())
        )
        if entity is None:
            entity = Entity(name=info.entity_name)
            session.add(entity)
        if entity not in borrower.entities:
            borrower.entities.append(entity)
    session.flush()
    return borrower


def find_or_create_property(session: Session, info: PropertyInfo) -> Property | None:
    """Property row keyed by normalized address; None until an address or link is known."""
    if not (info.address_normalized or info.listing_url):
        return None
    prop = None
    if info.address_normalized:
        prop = session.scalar(
            select(Property).where(Property.address_normalized == info.address_normalized)
        )
    if prop is None:
        prop = Property(
            address_raw=info.address_raw,
            address_normalized=info.address_normalized,
            listing_url=info.listing_url,
            county=info.county,
            state=info.state,
            state_source=info.state_source,
        )
        session.add(prop)
        session.flush()
    return prop


def build_deal(record: IntakeRecord, borrower: Borrower | None, prop: Property | None) -> Deal:
    """The ``deals`` row for an ``IntakeRecord``, attached to nothing.

    Split out from ``create_deal_from_intake`` so the CLI can build the same row from a
    team-entry fixture and run it through the same assembly the API uses, with no database
    behind it (``cli/fixtures.py``).
    """
    deal = Deal(
        id=record.id,
        borrower=borrower,
        property=prop,
        channel=record.channel,
        status=record.status,
        product=record.deal.product,
        product_source=record.deal.product_source,
        credit_range_self_reported=record.borrower.credit_range,
        experience_bucket_self_reported=record.borrower.experience_bucket,
        repeat_borrower_self_reported=record.borrower.repeat_borrower,
        purchase_price=record.deal.purchase_price,
        rehab_budget=record.deal.rehab_budget,
        loan_requested=record.deal.loan_requested,
        term_bucket=record.deal.term_bucket,
        asset_type=record.deal.asset_type,
        stated_exit=record.deal.stated_exit,
        actual_annual_taxes_usd=record.deal.actual_annual_taxes_usd,
        actual_annual_insurance_usd=record.deal.actual_annual_insurance_usd,
        as_is_value_team=record.deal.as_is_value_team,
        arv_team=record.deal.arv_team,
        court_records_status=record.deal.court_records_status,
        court_records_as_of=record.deal.court_records_as_of,
        # exclude_none keeps the stored matter to the fields the team actually filled in;
        # every omitted field is the model's own default on the way back out.
        court_records_team=[
            matter.model_dump(mode="json", exclude_none=True)
            for matter in record.deal.court_records_team
        ],
        missing_fields=list(record.missing_fields),
    )
    return deal


def transient_deal(record: IntakeRecord) -> Deal:
    """A deal built from an ``IntakeRecord`` without a session, for the CLI.

    The property row comes along because the screen reads its state (SPEC §7.5); the
    borrower row does not, because credit and experience live on the deal's own columns.
    Nothing here is added to a session, so nothing is persisted.
    """
    info = record.property
    prop = (
        Property(
            address_raw=info.address_raw,
            address_normalized=info.address_normalized,
            listing_url=info.listing_url,
            county=info.county,
            state=info.state,
            state_source=info.state_source,
        )
        if info.address_raw or info.listing_url
        else None
    )
    return build_deal(record, borrower=None, prop=prop)


def create_deal_from_intake(session: Session, record: IntakeRecord) -> Deal:
    """Create the deal (id = ``record.id``) and its immutable submission row. Flushes, no commit."""
    deal = build_deal(
        record,
        borrower=find_or_create_borrower(session, record.borrower),
        prop=find_or_create_property(session, record.property),
    )
    deal.submissions.append(
        IntakeSubmission(channel=record.channel, raw_payload=record.raw_payload)
    )
    session.add(deal)
    session.flush()
    return deal
