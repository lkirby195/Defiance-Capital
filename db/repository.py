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
        )
        session.add(prop)
        session.flush()
    return prop


def create_deal_from_intake(session: Session, record: IntakeRecord) -> Deal:
    """Create the deal (id = ``record.id``) and its immutable submission row. Flushes, no commit."""
    deal = Deal(
        id=record.id,
        borrower=find_or_create_borrower(session, record.borrower),
        property=find_or_create_property(session, record.property),
        channel=record.channel,
        status=record.status,
        credit_range_self_reported=record.borrower.credit_range,
        experience_bucket_self_reported=record.borrower.experience_bucket,
        repeat_borrower_self_reported=record.borrower.repeat_borrower,
        purchase_price=record.deal.purchase_price,
        rehab_budget=record.deal.rehab_budget,
        loan_requested=record.deal.loan_requested,
        term_bucket=record.deal.term_bucket,
        stated_exit=record.deal.stated_exit,
        missing_fields=list(record.missing_fields),
    )
    deal.submissions.append(
        IntakeSubmission(channel=record.channel, raw_payload=record.raw_payload)
    )
    session.add(deal)
    session.flush()
    return deal
