"""Engine and session factory. ``DATABASE_URL`` comes from the environment or ``.env``.

The driver is normalized on the way in. A managed Postgres hands out its URL as
``postgres://`` or ``postgresql://``, which SQLAlchemy reads as "use the default driver" -
and the default is psycopg2, which is not installed here. Left alone that is a deploy that
fails at the first query with a plugin error naming a library nobody chose, so the prefix is
rewritten to the driver this project actually depends on rather than documented as a thing
to remember.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


# What a managed Postgres hands out, and what this project's driver is called.
BARE_PREFIXES: tuple[str, ...] = ("postgres://", "postgresql://")
DRIVER_PREFIX = "postgresql+psycopg://"


def with_driver(url: str) -> str:
    """Name the psycopg driver in a URL that left it to SQLAlchemy to guess."""
    for prefix in BARE_PREFIXES:
        if url.startswith(prefix):
            return DRIVER_PREFIX + url[len(prefix) :]
    return url


def database_url() -> str:
    """Return ``DATABASE_URL``; a real environment variable wins over ``.env``."""
    load_dotenv(DOTENV_PATH, override=False)
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
            "e.g. postgresql+psycopg://user:password@localhost:5432/glenwood_uw"
        )
    return with_driver(url)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(database_url())


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    with Session(get_engine()) as session:
        yield session
