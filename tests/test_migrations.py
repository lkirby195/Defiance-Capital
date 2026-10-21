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


# Two deals written against 0017: one run three times - two Run Analysis pairs and a JSON
# underwrite on its own after them - and one screened once (SPEC §5, phase 8b).
DEAL_0017 = text(
    """
    INSERT INTO deals (id, channel, status, missing_fields, court_records_team,
                       credit_authorization_signed, created_at, updated_at, defaulted_fields)
    VALUES (:id, 'TEAM', 'UNDERWRITING', '[]', '[]', false, :at, :at, '[]')
    """
)
SUBMISSION_0017 = text(
    """
    INSERT INTO intake_submissions (id, deal_id, channel, raw_payload, received_at)
    VALUES (:id, :deal_id, 'TEAM', '{}', :at)
    """
)
SCREEN_0017 = text(
    """
    INSERT INTO screens (id, deal_id, engine_version, config_hash, inputs, score_components,
                         verdict, reasons, created_at)
    VALUES (:id, :deal_id, '1.7.0', 'screen-hash', '{}', '{}', 'GO', '[]', :at)
    """
)
UNDERWRITE_0017 = text(
    """
    INSERT INTO underwrites (id, deal_id, engine_version, config_hash, inputs, outputs, irr,
                             created_at)
    VALUES (:id, :deal_id, '1.7.0', 'uw-hash', '{}', '{}', 0.18, :at)
    """
)
RUN_AUDIT_0017 = text(
    """
    INSERT INTO audit_log (id, actor, action, table_name, row_id, after, created_at)
    VALUES (:id, :actor, :action, 'deals', :deal_id, CAST(:after AS JSONB), :at)
    """
)


def test_0018_backfills_a_legacy_version_per_run_pair_with_the_latest_working(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    command.upgrade(alembic_cfg, "0017")
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    worked, screened = uuid.uuid4(), uuid.uuid4()
    first_sub, second_sub, other_sub = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    s1, u1, s2, u2, u3, lone = (uuid.uuid4() for _ in range(6))

    def at(minutes: int) -> datetime:
        return start + timedelta(minutes=minutes)

    with test_engine.begin() as conn:
        conn.execute(DEAL_0017, {"id": worked, "at": at(0)})
        conn.execute(DEAL_0017, {"id": screened, "at": at(0)})
        conn.execute(SUBMISSION_0017, {"id": first_sub, "deal_id": worked, "at": at(0)})
        conn.execute(SUBMISSION_0017, {"id": second_sub, "deal_id": worked, "at": at(5)})
        conn.execute(SUBMISSION_0017, {"id": other_sub, "deal_id": screened, "at": at(0)})
        for row_id, minute, statement in (
            (s1, 1, SCREEN_0017),
            (u1, 2, UNDERWRITE_0017),
            (s2, 6, SCREEN_0017),
            (u2, 7, UNDERWRITE_0017),
            (u3, 9, UNDERWRITE_0017),
        ):
            conn.execute(statement, {"id": row_id, "deal_id": worked, "at": at(minute)})
        conn.execute(SCREEN_0017, {"id": lone, "deal_id": screened, "at": at(3)})
        for actor, action, key, row_id in (
            ("sam@glenwood.example", "SCREEN_RUN", "screen_id", s1),
            ("sam@glenwood.example", "UNDERWRITE_RUN", "underwrite_id", u1),
            ("system", "SCREEN_RUN", "screen_id", s2),
            ("system", "UNDERWRITE_RUN", "underwrite_id", u2),
            ("api@glenwood.example", "UNDERWRITE_RUN", "underwrite_id", u3),
        ):
            conn.execute(
                RUN_AUDIT_0017,
                {
                    "id": uuid.uuid4(),
                    "actor": actor,
                    "action": action,
                    "deal_id": str(worked),
                    "after": json.dumps({key: str(row_id)}),
                    "at": at(1),
                },
            )

    command.upgrade(alembic_cfg, "0018")

    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT deal_id, number, name, actor, engine_version, config_hash, screen_id, "
                "underwrite_id, is_working, inputs_snapshot, created_at FROM versions "
                "ORDER BY deal_id = :worked DESC, number"
            ),
            {"worked": worked},
        ).all()
    mine = [row for row in rows if row.deal_id == worked]
    assert [(row.number, row.name) for row in mine] == [(1, "v1"), (2, "v2"), (3, "v3")]
    assert [(row.screen_id, row.underwrite_id) for row in mine] == [(s1, u1), (s2, u2), (s2, u3)]
    assert [row.actor for row in mine] == [
        "sam@glenwood.example",
        "system",
        "api@glenwood.example",
    ]
    assert [row.is_working for row in mine] == [False, False, True]
    assert [row.config_hash for row in mine] == ["uw-hash"] * 3
    assert [row.created_at for row in mine] == [at(2), at(7), at(9)]
    assert [row.inputs_snapshot for row in mine] == [
        {"legacy": True, "intake": {"submission_id": str(first_sub)}},
        {"legacy": True, "intake": {"submission_id": str(second_sub)}},
        {"legacy": True, "intake": {"submission_id": str(second_sub)}},
    ]
    (theirs,) = [row for row in rows if row.deal_id == screened]
    assert (theirs.number, theirs.screen_id, theirs.underwrite_id) == (1, lone, None)
    assert theirs.is_working and theirs.actor == "unknown"
    assert theirs.engine_version == "1.7.0" and theirs.config_hash == "screen-hash"

    command.downgrade(alembic_cfg, "0017")
    with test_engine.connect() as conn:
        assert "versions" not in inspect(conn).get_table_names()
        assert conn.execute(text("SELECT count(*) FROM underwrites")).scalar() == 3

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


# Versions written against 0018: default names, a typed one, and one a person marked Working
# (SPEC §5, phase 8c prep).
VERSION_0018 = text(
    """
    INSERT INTO versions (id, deal_id, number, name, created_at, actor, engine_version,
                          config_hash, inputs_snapshot, is_working)
    VALUES (:id, :deal_id, :number, :name, now(), 'sam@glenwood.example', '1.8.0', 'h',
            '{"schema": 1}', :working)
    """
)
MARKED_0018 = text(
    """
    INSERT INTO audit_log (id, actor, action, table_name, row_id, after, created_at)
    VALUES (:id, 'sam@glenwood.example', 'VERSION_MARKED_WORKING', 'deals', :deal_id,
            CAST(:after AS JSONB), now())
    """
)


def test_0019_marks_the_versions_a_person_named_or_marked(
    alembic_cfg: AlembicConfig, test_engine: Engine
) -> None:
    import json
    from datetime import UTC, datetime

    command.upgrade(alembic_cfg, "0018")
    one, other = uuid.uuid4(), uuid.uuid4()
    with test_engine.begin() as conn:
        for deal_id in (one, other):
            conn.execute(DEAL_0017, {"id": deal_id, "at": datetime(2026, 10, 1, tzinfo=UTC)})
        for number, name, working in (
            (1, "Auto v1", False),
            (2, "v2", False),
            (3, "Base case", False),
            (4, "Auto v4", True),
        ):
            conn.execute(
                VERSION_0018,
                {
                    "id": uuid.uuid4(),
                    "deal_id": one,
                    "number": number,
                    "name": name,
                    "working": working,
                },
            )
        conn.execute(
            VERSION_0018,
            {"id": uuid.uuid4(), "deal_id": other, "number": 4, "name": "v4", "working": True},
        )
        conn.execute(
            MARKED_0018,
            {"id": uuid.uuid4(), "deal_id": str(one), "after": json.dumps({"version": 4})},
        )

    command.upgrade(alembic_cfg, "0019")

    with test_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT deal_id, number, human_selected FROM versions ORDER BY number")
        ).all()
    flags = {(row.deal_id, row.number): row.human_selected for row in rows}
    assert flags == {
        (one, 1): False,  # a default automatic name
        (one, 2): False,  # a default name
        (one, 3): True,  # typed by a person
        (one, 4): True,  # marked Working by a person
        (other, 4): False,  # the mark was on the other deal
    }

    command.downgrade(alembic_cfg, "0018")
    with test_engine.connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("versions")}
        assert "human_selected" not in columns
        assert conn.execute(text("SELECT count(*) FROM versions")).scalar() == 5

    command.downgrade(alembic_cfg, "base")
    with test_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
