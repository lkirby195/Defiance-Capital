"""Persist an ``IntakeRecord`` as rows in borrowers / entities / properties / deals /
intake_submissions.  # SPEC §5

Borrowers are matched on the normalized phone (the primary match key) and
properties on the normalized address, so repeat inquiries reuse existing rows.
Existing rows are never modified here; the submission row is immutable.
"""

from __future__ import annotations

from typing import Any

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
            city=info.city,
            county=info.county,
            state=info.state,
            state_source=info.state_source,
            units=info.units,
            structures=info.structures,
            sf=info.sf,
            year_built=info.year_built,
            year_renovated=info.year_renovated,
            beds=info.beds,
            baths=info.baths,
            garage_spaces=info.garage_spaces,
        )
        session.add(prop)
        session.flush()
    return prop


# The ``deals`` columns the team-entry form has a box for (``api/intake_form.py``), in the
# order the form asks. Edit Intake re-applies these and only these
# (``services/intake.update_intake``): the §8.1 economics, the valuation, the rent, the
# toggles and the court search are edited on the deal page and have no box on the form, so
# an edit that could not have known them must not clear them. ``term_stub_days`` is here
# because the form's term is whole months and a stub beside it would be stale.
FORM_COLUMNS: tuple[str, ...] = (
    "credit_range_self_reported",
    "experience_bucket_self_reported",
    "repeat_borrower_self_reported",
    "purchase_price",
    "rehab_costs",
    "loan_requested",
    "loan_purpose",
    "product",
    "product_source",
    "closing_date",
    "term_bucket",
    "term_months",
    "term_stub_days",
)


def intake_columns(record: IntakeRecord) -> dict[str, Any]:
    """The ``deals`` columns an ``IntakeRecord`` owns, by column name.

    One mapping, so creating a deal from an intake writes one set of columns whatever the
    channel. Everything outside it - the id, the channel, the status, the web form's
    ``intake_source`` and ``referral_note``, the borrower's own estimates
    (``borrower_columns``), the borrower and property links - belongs to the row rather than
    to the intake, and is set by whoever is writing the row. An edit re-applies the
    ``FORM_COLUMNS`` subset (``form_columns``).
    """
    return {
        "loan_purpose": record.deal.loan_purpose,
        "closing_date": record.deal.closing_date,
        "product": record.deal.product,
        "product_source": record.deal.product_source,
        "credit_range_self_reported": record.borrower.credit_range,
        "experience_bucket_self_reported": record.borrower.experience_bucket,
        "repeat_borrower_self_reported": record.borrower.repeat_borrower,
        "purchase_price": record.deal.purchase_price,
        "rehab_costs": record.deal.rehab_costs,
        "loan_requested": record.deal.loan_requested,
        "loan_purchase_portion": record.deal.loan_purchase_portion,
        "loan_rehab_portion": record.deal.loan_rehab_portion,
        "term_bucket": record.deal.term_bucket,
        "term_months": record.deal.term_months,
        "term_stub_days": record.deal.term_stub_days,
        "interest_rate": record.deal.interest_rate,
        "contingency_pct": record.deal.contingency_pct,
        "closing_costs_usd": record.deal.closing_costs_usd,
        "holding_costs_pct_of_cost": record.deal.holding_costs_pct_of_cost,
        "origination_fee_pct": record.deal.origination_fee_pct,
        "flip_analysis": record.deal.flip_analysis,
        "rental_analysis": record.deal.rental_analysis,
        "monthly_rent": record.deal.monthly_rent,
        "estimated_sale_price_team": record.deal.estimated_sale_price_team,
        "court_records_status": record.deal.court_records_status,
        "court_records_as_of": record.deal.court_records_as_of,
        # exclude_none keeps the stored matter to the fields the team actually filled in;
        # every omitted field is the model's own default on the way back out.
        "court_records_team": [
            matter.model_dump(mode="json", exclude_none=True)
            for matter in record.deal.court_records_team
        ],
    }


def form_columns(record: IntakeRecord) -> dict[str, Any]:
    """The columns the team-entry form carries, as the record says them.  # SPEC §4.1, §8.1

    What Edit Intake writes. A subset of ``intake_columns`` by name, so a column added to
    the form is added in one place and both writers pick it up; ``tests/test_intake_form.py``
    holds the form's own field list to this one.
    """
    every = intake_columns(record)
    return {column: every[column] for column in FORM_COLUMNS}


def borrower_columns(record: IntakeRecord) -> dict[str, Any]:
    """The ``deals`` columns that hold the borrower's own estimates.  # SPEC §4.2

    Kept apart from ``intake_columns`` on purpose. Those are re-applied whole when the team
    edits the intake (``services/intake.py``), and the team form has no box for what the
    borrower claimed - so a borrower's sale price and rent are written once, by the channel
    that asked, and never cleared by an edit that could not have known them.
    """
    return {
        "estimated_sale_price_borrower": record.deal.estimated_sale_price_borrower,
        "monthly_rent_borrower": record.deal.monthly_rent_borrower,
    }


def build_deal(record: IntakeRecord, borrower: Borrower | None, prop: Property | None) -> Deal:
    """The ``deals`` row for an ``IntakeRecord``, attached to nothing.

    Split out from ``create_deal_from_intake`` so the CLI can build the same row from a
    team-entry fixture and run it through the same assembly the API uses, with no database
    behind it (``cli/fixtures.py``).
    """
    return Deal(
        id=record.id,
        borrower=borrower,
        property=prop,
        channel=record.channel,
        status=record.status,
        intake_source=record.intake_source,
        referral_note=record.referral_note,
        missing_fields=list(record.missing_fields),
        **borrower_columns(record),
        **intake_columns(record),
    )


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
            city=info.city,
            county=info.county,
            state=info.state,
            state_source=info.state_source,
            units=info.units,
            structures=info.structures,
            sf=info.sf,
            year_built=info.year_built,
            year_renovated=info.year_renovated,
            beds=info.beds,
            baths=info.baths,
            garage_spaces=info.garage_spaces,
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
