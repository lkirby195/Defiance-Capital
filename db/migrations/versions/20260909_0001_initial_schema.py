"""initial schema: every table in SPEC §5

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum types are shared across tables (channel is on two), so they are created
# and dropped explicitly rather than by create_table.
state_code = postgresql.ENUM("OK", "CO", "OTHER", name="state_code", create_type=False)
channel = postgresql.ENUM("SMS", "LINK", "CONTRACT", "TEAM", name="channel", create_type=False)
deal_status = postgresql.ENUM(
    "NEW",
    "NEEDS_INFO",
    "SCREENED",
    "IN_REVIEW",
    "UNDERWRITING",
    "LOI_SENT",
    "HANDED_OFF",
    "DECLINED",
    "DEAD",
    name="deal_status",
    create_type=False,
)
product = postgresql.ENUM(
    "NO_DRAW", "SPLIT_DRAW", "SPLIT_PRINCIPAL", "WHOLETAIL", name="product", create_type=False
)
credit_tranche = postgresql.ENUM(
    "T1", "T2", "T3", "T4", "T5", name="credit_tranche", create_type=False
)
experience_bucket = postgresql.ENUM(
    "0", "1_2", "3_5", "6_PLUS", name="experience_bucket", create_type=False
)
term_bucket = postgresql.ENUM("3", "6", "9", "12", "12_PLUS", name="term_bucket", create_type=False)
stated_exit = postgresql.ENUM(
    "FLIP", "HOLD", "WHOLETAIL", "UNKNOWN", name="stated_exit", create_type=False
)
verdict = postgresql.ENUM("GO", "CONDITIONAL", "DECLINE", name="verdict", create_type=False)
document_kind = postgresql.ENUM(
    "CREDIT_REPORT", "VALUATION", "CONTRACT", "CREDIT_MEMO", "LOI",
    name="document_kind",
    create_type=False,
)
ENUMS = [
    state_code,
    channel,
    deal_status,
    product,
    credit_tranche,
    experience_bucket,
    term_bucket,
    stated_exit,
    verdict,
    document_kind,
]

MONEY = sa.Numeric(14, 2)  # SPEC §5
RATE = sa.Numeric(7, 5)  # SPEC §5
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def _created_at(name: str = "created_at") -> sa.Column:
    return sa.Column(name, TS, server_default=NOW, nullable=False)


def _deal_fk(nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(
        "deal_id", sa.Uuid(), sa.ForeignKey("deals.id", ondelete=ondelete), nullable=nullable
    )


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "borrowers",
        _id(),
        sa.Column("name", sa.String(200)),
        sa.Column("phone", sa.String(32), nullable=False, unique=True),
        sa.Column("email", sa.String(254)),
        sa.Column("ma_borrower_id", sa.String(64)),
        _created_at(),
        _created_at("updated_at"),
    )
    op.create_table(
        "entities",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        _created_at(),
    )
    op.create_table(
        "borrower_entities",
        sa.Column(
            "borrower_id",
            sa.Uuid(),
            sa.ForeignKey("borrowers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "properties",
        _id(),
        sa.Column("address_raw", sa.Text()),
        sa.Column("address_normalized", sa.Text(), unique=True),
        sa.Column("listing_url", sa.Text()),
        sa.Column("parcel_id", sa.String(64)),
        sa.Column("county", sa.String(100)),
        sa.Column("state", state_code, nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "address_raw IS NOT NULL OR listing_url IS NOT NULL",
            name="ck_properties_address_or_url",
        ),
    )
    op.create_table(
        "deals",
        _id(),
        sa.Column("borrower_id", sa.Uuid(), sa.ForeignKey("borrowers.id", ondelete="RESTRICT")),
        sa.Column("property_id", sa.Uuid(), sa.ForeignKey("properties.id", ondelete="RESTRICT")),
        sa.Column("channel", channel, nullable=False),
        sa.Column("status", deal_status, nullable=False),
        sa.Column("product", product),
        sa.Column("credit_range_self_reported", credit_tranche),
        sa.Column("experience_bucket_self_reported", experience_bucket),
        sa.Column("repeat_borrower_self_reported", sa.Boolean()),
        sa.Column("purchase_price", MONEY),
        sa.Column("rehab_budget", MONEY),
        sa.Column("loan_requested", MONEY),
        sa.Column("term_bucket", term_bucket),
        sa.Column("stated_exit", stated_exit),
        sa.Column("missing_fields", JSONB, nullable=False),
        sa.Column(
            "credit_authorization_signed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        _created_at(),
        _created_at("updated_at"),
    )
    op.create_index(op.f("ix_deals_borrower_id"), "deals", ["borrower_id"])
    op.create_index(op.f("ix_deals_property_id"), "deals", ["property_id"])
    op.create_index("ix_deals_status", "deals", ["status"])

    op.create_table(
        "intake_submissions",
        _id(),
        _deal_fk(nullable=True, ondelete="SET NULL"),
        sa.Column("channel", channel, nullable=False),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column("file_ref", sa.Text()),
        _created_at("received_at"),
    )
    op.create_index(op.f("ix_intake_submissions_deal_id"), "intake_submissions", ["deal_id"])

    op.create_table(
        "enrichment_runs",
        _id(),
        _deal_fk(),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        _created_at("requested_at"),
        sa.Column("completed_at", TS),
        sa.Column("raw_response", JSONB),
        sa.Column("parsed_result", JSONB),
        sa.Column("error", sa.Text()),
    )
    op.create_index(op.f("ix_enrichment_runs_deal_id"), "enrichment_runs", ["deal_id"])

    op.create_table(
        "screens",
        _id(),
        _deal_fk(),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("score_components", JSONB, nullable=False),
        sa.Column("verdict", verdict, nullable=False),
        sa.Column("reasons", JSONB, nullable=False),
        sa.Column("suggested_reply", sa.Text()),
        _created_at(),
    )
    op.create_index(op.f("ix_screens_deal_id"), "screens", ["deal_id"])

    op.create_table(
        "underwrites",
        _id(),
        _deal_fk(),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("outputs", JSONB, nullable=False),
        sa.Column("grid_lender", JSONB, nullable=False),
        sa.Column("grid_borrower", JSONB, nullable=False),
        sa.Column("solved_rate", RATE),
        _created_at(),
    )
    op.create_index(op.f("ix_underwrites_deal_id"), "underwrites", ["deal_id"])

    op.create_table(
        "documents",
        _id(),
        _deal_fk(),
        sa.Column("kind", document_kind, nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(100)),
        _created_at(),
    )
    op.create_index(op.f("ix_documents_deal_id"), "documents", ["deal_id"])

    op.create_table(
        "ma_sync",
        _id(),
        _deal_fk(),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("ma_borrower_id", sa.String(64)),
        sa.Column("ma_property_id", sa.String(64)),
        sa.Column("ma_loan_id", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text()),
        _created_at(),
    )
    op.create_index(op.f("ix_ma_sync_deal_id"), "ma_sync", ["deal_id"])

    op.create_table(
        "audit_log",
        _id(),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("row_id", sa.String(64), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        _created_at(),
    )
    op.create_index("ix_audit_log_table_row", "audit_log", ["table_name", "row_id"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "ma_sync",
        "documents",
        "underwrites",
        "screens",
        "enrichment_runs",
        "intake_submissions",
        "deals",
        "properties",
        "borrower_entities",
        "entities",
        "borrowers",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in ENUMS:
        enum.drop(bind, checkfirst=True)
