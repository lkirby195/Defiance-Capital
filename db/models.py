"""SQLAlchemy models, one class per table in SPEC §5.

Money is ``NUMERIC(14,2)``, rates ``NUMERIC(7,5)``, JSON is ``JSONB``, and enum
columns persist the enum *values* (``"12_PLUS"``, not the member name) so the
database matches ``schema/intake.json``. Rows on ``intake_submissions``,
``enrichment_runs``, ``screens``, ``underwrites``, ``documents``, ``ma_sync`` and
``audit_log`` are append-only: re-runs create new rows (CLAUDE.md, "Record everything").
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from schema.models import (
    AssetType,
    Channel,
    CourtRecordsStatus,
    DocumentKind,
    ExperienceBucket,
    Product,
    ProductSource,
    State,
    StatedExit,
    StateSource,
    Status,
    TermBucket,
    Tranche,
    Verdict,
)

MONEY = Numeric(14, 2)  # SPEC §5
RATE = Numeric(7, 5)  # SPEC §5


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Postgres enum type persisting the member values, not the member names."""
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[dict[str, Any]]: JSONB,
        datetime: DateTime(timezone=True),
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)


def _event_at() -> Mapped[datetime]:
    """Insert time of an append-only run row, from ``clock_timestamp()``.

    Not ``now()``: that is the transaction start time and is identical for every row
    written in one transaction, which would leave two runs of the same screen with no
    order between them. ``clock_timestamp()`` reads the wall clock per row, so
    "the latest screen" is always a real answer.
    """
    return mapped_column(server_default=func.clock_timestamp(), nullable=False)


borrower_entities = Table(
    "borrower_entities",
    Base.metadata,
    Column("borrower_id", Uuid, ForeignKey("borrowers.id", ondelete="CASCADE"), primary_key=True),
    Column("entity_id", Uuid, ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True),
)


class Borrower(Base):
    """Person-level borrower; phone is the primary match key.  # SPEC §5"""

    __tablename__ = "borrowers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    ma_borrower_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    entities: Mapped[list[Entity]] = relationship(
        secondary=borrower_entities, back_populates="borrowers"
    )


class Entity(Base):
    """LLCs and other borrowing entities; many-to-many with borrowers.  # SPEC §5"""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    borrowers: Mapped[list[Borrower]] = relationship(
        secondary=borrower_entities, back_populates="entities"
    )


class Property(Base):
    """Normalized address, parcel, county, state; reused across deals.  # SPEC §5"""

    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint(
            "address_raw IS NOT NULL OR listing_url IS NOT NULL",
            name="ck_properties_address_or_url",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    address_raw: Mapped[str | None] = mapped_column(Text)
    address_normalized: Mapped[str | None] = mapped_column(Text, unique=True)
    listing_url: Mapped[str | None] = mapped_column(Text)
    parcel_id: Mapped[str | None] = mapped_column(String(64))
    county: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[State] = mapped_column(_enum(State, "state_code"), nullable=False)
    state_source: Mapped[StateSource] = mapped_column(
        _enum(StateSource, "state_source"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class IntakeSubmission(Base):
    """Every inbound message, file, or form; immutable and raw.  # SPEC §5"""

    __tablename__ = "intake_submissions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    raw_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    file_ref: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = _created_at()


class Deal(Base):
    """One per property x borrower inquiry; holds the current IntakeRecord state.  # SPEC §5"""

    __tablename__ = "deals"
    __table_args__ = (
        Index("ix_deals_status", "status"),
        CheckConstraint(
            "(product IS NULL) = (product_source IS NULL)",
            name="ck_deals_product_and_source_together",
        ),
        # A searched record is dated: the engine has no clock and the SPEC §7.2 lookbacks
        # are measured from the day the team searched.
        CheckConstraint(
            "court_records_status IS NULL "
            "OR court_records_status = 'NOT_CHECKED' "
            "OR court_records_as_of IS NOT NULL",
            name="ck_deals_court_records_dated_when_searched",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    borrower_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("borrowers.id", ondelete="RESTRICT"), index=True
    )
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    status: Mapped[Status] = mapped_column(_enum(Status, "deal_status"), nullable=False)
    product: Mapped[Product | None] = mapped_column(_enum(Product, "product"))
    product_source: Mapped[ProductSource | None] = mapped_column(
        _enum(ProductSource, "product_source")
    )
    credit_range_self_reported: Mapped[Tranche | None] = mapped_column(
        _enum(Tranche, "credit_tranche")
    )
    experience_bucket_self_reported: Mapped[ExperienceBucket | None] = mapped_column(
        _enum(ExperienceBucket, "experience_bucket")
    )
    repeat_borrower_self_reported: Mapped[bool | None] = mapped_column(Boolean)
    purchase_price: Mapped[Decimal | None] = mapped_column(MONEY)
    rehab_budget: Mapped[Decimal | None] = mapped_column(MONEY)
    loan_requested: Mapped[Decimal | None] = mapped_column(MONEY)
    term_bucket: Mapped[TermBucket | None] = mapped_column(_enum(TermBucket, "term_bucket"))
    # Asset type from intake; with the term it drives the exit inference (SPEC §3).
    asset_type: Mapped[AssetType | None] = mapped_column(_enum(AssetType, "asset_type"))
    stated_exit: Mapped[StatedExit | None] = mapped_column(_enum(StatedExit, "stated_exit"))
    # Team-supplied actuals overriding the %-of-value opex defaults (SPEC §8.6); annual USD.
    actual_annual_taxes_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_annual_insurance_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    # The two remaining SPEC §8.1 team inputs. They have no config default and no adapter
    # behind them, so the queue collects them once on the deal rather than asking for them
    # again on every run; an UnderwriteRequest still outranks what is stored here.
    actual_annual_utilities_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    market_rent_monthly: Mapped[Decimal | None] = mapped_column(MONEY)
    # Team-supplied valuation and court search, used until the Phase 3 adapters land
    # (SPEC §6). An adapter value always wins; these are never overwritten, so a later
    # reader can see what was entered by hand and what superseded it.
    as_is_value_team: Mapped[Decimal | None] = mapped_column(MONEY)
    arv_team: Mapped[Decimal | None] = mapped_column(MONEY)
    court_records_status: Mapped[CourtRecordsStatus | None] = mapped_column(
        _enum(CourtRecordsStatus, "court_records_status")
    )
    court_records_as_of: Mapped[date | None] = mapped_column(Date)
    # One TeamCourtRecord per matter (SPEC §7.2), validated by the schema on the way in.
    court_records_team: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    credit_authorization_signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    borrower: Mapped[Borrower | None] = relationship()
    property: Mapped[Property | None] = relationship()
    submissions: Mapped[list[IntakeSubmission]] = relationship(
        order_by=IntakeSubmission.received_at
    )


class EnrichmentRun(Base):
    """One row per adapter call; the raw response is retained.  # SPEC §5, §6"""

    __tablename__ = "enrichment_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_response: Mapped[Any | None] = mapped_column(JSONB)
    parsed_result: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class Screen(Base):
    """Screen inputs, score components, verdict, reasons; one row per run.  # SPEC §5, §7

    ``score_components`` is the whole ``ScreenResult`` less the columns broken out beside it
    (verdict, reasons, suggested_reply, engine_version, config_hash): components, sizing,
    and flags. A stored row therefore rebuilds the exact result.
    """

    __tablename__ = "screens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score_components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict, "verdict"), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    suggested_reply: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _event_at()


class Underwrite(Base):
    """Full underwrite inputs, outputs, and the sensitivity grid.  # SPEC §5, §8

    ``outputs`` is the whole ``UnderwriteResult`` less ``grid_lender``, which has its own
    column; together they rebuild the result exactly. ``solved_rate`` is a NUMERIC(7,5) copy
    of r* for querying - the JSONB keeps full precision. There is one grid (SPEC §8.5): the
    borrower grid was removed in Phase 2b and its column dropped in migration 0004.
    """

    __tablename__ = "underwrites"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    grid_lender: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    solved_rate: Mapped[Decimal | None] = mapped_column(RATE)
    created_at: Mapped[datetime] = _event_at()


class Document(Base):
    """Credit reports, valuations, contracts, generated memos and LOIs.  # SPEC §5"""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[DocumentKind] = mapped_column(_enum(DocumentKind, "document_kind"), nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created_at()


class MASync(Base):
    """Handoff log to Mortgage Automator.  # SPEC §5, §9.4"""

    __tablename__ = "ma_sync"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ma_borrower_id: Mapped[str | None] = mapped_column(String(64))
    ma_property_id: Mapped[str | None] = mapped_column(String(64))
    ma_loan_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    """A GLENWOOD team member who can sign in to the review queue.  # SPEC §5, §11

    Not in the SPEC §5 table because SPEC §5 is the deal pipeline; this is the other half of
    ``audit_log``, which records *who* changed what and is required because credit and court
    data are in here (SPEC §11). There is no self-signup and no password reset in v1: a user
    is created from the command line and deactivated the same way.

    ``email`` is the sign-in name and is stored lower-cased (``services/users.py`` normalizes
    it), so the unique index is the uniqueness rule a person would expect. ``password_hash``
    carries its own algorithm and parameters (``services/passwords.py``); the password itself
    is never stored, logged, or put in an ``audit_log`` row.

    Deactivating is not deleting: ``audit_log.actor`` names people who may have left, and a
    row whose actor no longer resolves to anyone would be worse than useless. ``active`` is
    checked on every request, so a deactivated user's live session stops working at once.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AuditLog(Base):
    """Who changed what; credit and court data live here.  # SPEC §5, §11"""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_table_row", "table_name", "row_id"),
        # The deal page reads one row's trail newest-first, and the queue asks when a deal
        # last entered a status (SPEC §12 pinning), so time is part of the lookup.
        Index("ix_audit_log_table_row_created", "table_name", "row_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    row_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
