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


# A deal written against 0015 with the two columns 0016 drops, and an underwrite row on it.
BEFORE_0016 = text(
    """
    INSERT INTO deals (id, channel, status, missing_fields, court_records_team,
                       credit_authorization_signed, created_at, updated_at,
                       asset_type, stated_exit, defaulted_fields)
    VALUES (:id, 'TEAM', :status, '[]', '[]', false, now(), now(), 'SFR', 'FLIP', '[]')
    """
)
UNDERWRITE_ROW = text(
    """
    INSERT INTO underwrites (id, deal_id, engine_version, config_hash, inputs, outputs, irr,
                             created_at)
    VALUES (:id, :deal_id, '1.3.0', 'abc', '{}', '{}', 0.15, now())
    """
)
PAUSE_ROW = text(
    """
    INSERT INTO audit_log (id, actor, action, table_name, row_id, before, after, created_at)
    VALUES (:id, 'sam@glenwood.example', 'PAUSED', 'deals', :deal_id,
            '{"status": "IN_REVIEW"}', '{"status": "PAUSED"}', now())
    """
)


def test_0016_adds_paused_drops_the_exit_columns_and_deletes_the_underwrites(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    command.upgrade(alembic_cfg, "0015")
    worked, scripted = uuid.uuid4(), uuid.uuid4()
    with test_engine.begin() as conn:
        conn.execute(BEFORE_0016, {"id": worked, "status": "IN_REVIEW"})
        conn.execute(BEFORE_0016, {"id": scripted, "status": "NEW"})
        conn.execute(UNDERWRITE_ROW, {"id": uuid.uuid4(), "deal_id": worked})

    command.upgrade(alembic_cfg, "0016")

    with test_engine.connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("deals")}
        assert "asset_type" not in columns and "stated_exit" not in columns
        enums = {e["name"]: e["labels"] for e in inspect(conn).get_enums()}
        assert "asset_type" not in enums and "stated_exit" not in enums
        assert "PAUSED" in enums["deal_status"]
        assert conn.execute(text("SELECT count(*) FROM underwrites")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM deals")).scalar() == 2
    # ...and the new value is usable: pause both deals, one with the row that says whence
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE deals SET status = 'PAUSED'"))
        conn.execute(PAUSE_ROW, {"id": uuid.uuid4(), "deal_id": str(worked)})

    command.downgrade(alembic_cfg, "0015")

    with test_engine.connect() as conn:
        statuses = dict(conn.execute(text("SELECT id, status FROM deals")).all())
        assert statuses[worked] == "IN_REVIEW"  # back where its PAUSED row says it came from
        assert statuses[scripted] == "NEW"  # nothing said, so NEW rather than nowhere
        enums = {e["name"]: e["labels"] for e in inspect(conn).get_enums()}
        assert "PAUSED" not in enums["deal_status"]
        assert "asset_type" in enums and "stated_exit" in enums
        columns = {column["name"] for column in inspect(conn).get_columns("deals")}
        assert {"asset_type", "stated_exit"} <= columns

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


# A deal written against 0016, before the five assumption columns existed (SPEC §8.4-§8.6).
BEFORE_0017 = text(
    """
    INSERT INTO deals (id, channel, status, missing_fields, court_records_team,
                       credit_authorization_signed, created_at, updated_at, defaulted_fields,
                       interest_rate)
    VALUES (:id, 'TEAM', 'NEW', '[]', '[]', false, now(), now(), '["interest_rate"]', 0.12)
    """
)
ASSUMPTION_COLUMNS = (
    "broker_selling_pct",
    "rental_expenses_pct_of_rent",
    "rental_takeout_rate",
    "take_back_legal_costs_usd",
    "take_back_lost_interest_months",
)


def test_0017_adds_the_five_assumption_columns_empty_and_rewrites_nothing(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    """NULL on every existing deal - the engine reads config there - and the tagging the
    deal already carried is left exactly as it was, for the next open to extend."""
    command.upgrade(alembic_cfg, "0016")
    deal_id = uuid.uuid4()
    with test_engine.begin() as conn:
        conn.execute(BEFORE_0017, {"id": deal_id})

    command.upgrade(alembic_cfg, "0017")

    with test_engine.connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("deals")}
        assert set(ASSUMPTION_COLUMNS) <= columns
        row = conn.execute(
            text(
                "SELECT broker_selling_pct, rental_expenses_pct_of_rent, rental_takeout_rate, "
                "take_back_legal_costs_usd, take_back_lost_interest_months, defaulted_fields "
                "FROM deals WHERE id = :id"
            ),
            {"id": deal_id},
        ).one()
    assert all(value is None for value in row[:5])
    assert row.defaulted_fields == ["interest_rate"]

    command.downgrade(alembic_cfg, "0016")
    with test_engine.connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("deals")}
        assert not (set(ASSUMPTION_COLUMNS) & columns)
        assert conn.execute(text("SELECT count(*) FROM deals")).scalar() == 1

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


# A web deal written against 0013, when the public form put the borrower's numbers in the
# team's columns, and a team deal beside it (SPEC §4.2, §6.1).
BEFORE_0014 = text(
    """
    INSERT INTO deals (id, channel, status, missing_fields, court_records_team,
                       credit_authorization_signed, created_at, updated_at,
                       estimated_sale_price_team, monthly_rent)
    VALUES (:id, :channel, 'NEW', '[]', '[]', false, now(), now(), :sale, :rent)
    """
)
OVERRIDE_ROW = text(
    """
    INSERT INTO audit_log (id, actor, action, table_name, row_id, created_at)
    VALUES (:id, 'sam@glenwood.example', 'OVERRIDES_SAVED', 'deals', :deal_id, now())
    """
)


def test_0014_moves_an_untouched_web_deal_s_numbers_to_the_borrower_s_columns(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    command.upgrade(alembic_cfg, "0013")
    untouched, corrected, team = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with test_engine.begin() as conn:
        for deal_id, channel in ((untouched, "WEB"), (corrected, "WEB"), (team, "TEAM")):
            conn.execute(
                BEFORE_0014,
                {"id": deal_id, "channel": channel, "sale": "210000.00", "rent": "1700.00"},
            )
        # the team saved the override block on one of the web deals since it arrived
        conn.execute(OVERRIDE_ROW, {"id": uuid.uuid4(), "deal_id": str(corrected)})

    command.upgrade(alembic_cfg, "0014")

    with test_engine.connect() as conn:
        rows = {
            row.id: row
            for row in conn.execute(
                text(
                    "SELECT id, estimated_sale_price_team, monthly_rent, "
                    "estimated_sale_price_borrower, monthly_rent_borrower FROM deals"
                )
            )
        }
    # untouched web deal: the numbers are the borrower's, so that is where they go
    assert rows[untouched].estimated_sale_price_borrower == Decimal("210000.00")
    assert rows[untouched].monthly_rent_borrower == Decimal("1700.00")
    assert rows[untouched].estimated_sale_price_team is None
    assert rows[untouched].monthly_rent is None
    # a web deal the team has written to is left alone: the team column is now the team's
    assert rows[corrected].estimated_sale_price_team == Decimal("210000.00")
    assert rows[corrected].estimated_sale_price_borrower is None
    # a team deal is not a web deal
    assert rows[team].estimated_sale_price_team == Decimal("210000.00")
    assert rows[team].estimated_sale_price_borrower is None

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
