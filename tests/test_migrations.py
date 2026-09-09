"""The committed migration must produce exactly the schema the models describe."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from db.models import Base
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, test_engine: Engine) -> AlembicConfig:
    Base.metadata.drop_all(test_engine)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
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
