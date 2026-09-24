"""The committed migration must produce exactly the schema the models describe.

...and where a migration converts data rather than only reshaping it, the conversion is
checked on a row written against the old schema. ``0012`` is that case: it turns each stored
holding-cost dollar figure into the percentage of the price plus the rehab it *was*, so a deal
that was carrying $9,000 of carry goes on carrying $9,000 of carry (SPEC §8.1).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from db.models import Base
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch, test_engine: Engine, test_database_url: str
) -> AlembicConfig:
    Base.metadata.drop_all(test_engine)
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    return AlembicConfig("alembic.ini")


def test_upgrade_head_matches_models_and_downgrade_is_clean(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    command.upgrade(alembic_cfg, "head")
    with test_engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(context, Base.metadata)
        assert diffs == [], f"migration drifts from models: {diffs}"
        expected = set(Base.metadata.tables) | {"alembic_version"}
        assert set(inspect(conn).get_table_names()) == expected

    command.downgrade(alembic_cfg, "base")
    with test_engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) == {"alembic_version"}
        enum_names = {e["name"] for e in inspect(conn).get_enums()}
        assert enum_names == set()
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


# One deal per shape, written against 0011 and read back after 0012 (SPEC §8.1).
BEFORE_0012 = text(
    """
    INSERT INTO deals (id, channel, status, missing_fields, court_records_team,
                       credit_authorization_signed, created_at, updated_at,
                       purchase_price, rehab_costs, holding_costs_total_usd, term_months)
    VALUES (:id, 'TEAM', 'NEW', '[]', '[]', false, now(), now(),
            :price, :rehab, :holding, 9)
    """
)


@pytest.mark.parametrize(
    ("price", "rehab", "holding", "expected"),
    [
        # 9,000 of 248,000 is 3.62903% to the column's five places.
        ("200000.00", "48000.00", "9000.00", Decimal("0.03629")),
        ("300000.00", "0.00", "9000.00", Decimal("0.03000")),
        # Nothing to be a percentage of, so nothing is claimed: NULL is the config default.
        ("0.00", "0.00", "9000.00", None),
        (None, None, "9000.00", None),
        ("200000.00", "48000.00", None, None),
    ],
)
def test_0012_turns_each_holding_cost_into_the_share_of_cost_it_was(
    alembic_cfg: AlembicConfig,
    test_engine: Engine,
    price: str | None,
    rehab: str | None,
    holding: str | None,
    expected: Decimal | None,
) -> None:
    command.upgrade(alembic_cfg, "0011")
    deal_id = uuid.uuid4()
    with test_engine.begin() as conn:
        conn.execute(
            BEFORE_0012, {"id": deal_id, "price": price, "rehab": rehab, "holding": holding}
        )

    command.upgrade(alembic_cfg, "0012")

    with test_engine.connect() as conn:
        got = conn.execute(
            text("SELECT holding_costs_pct_of_cost, term_stub_days FROM deals WHERE id = :id"),
            {"id": deal_id},
        ).one()
    assert got.holding_costs_pct_of_cost == expected
    # A row that existed before the stub column did has no stub, so its payoff date is where
    # it always was.
    assert got.term_stub_days is None

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
