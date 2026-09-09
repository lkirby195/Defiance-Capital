"""Engine and session factory. ``DATABASE_URL`` comes from the environment or ``.env``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def database_url() -> str:
    """Return ``DATABASE_URL``; a real environment variable wins over ``.env``."""
    load_dotenv(DOTENV_PATH, override=False)
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
            "e.g. postgresql+psycopg://user:password@localhost:5432/glenwood_uw"
        )
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(database_url())


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    with Session(get_engine()) as session:
        yield session
