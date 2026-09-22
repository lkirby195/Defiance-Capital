"""The deploy: the URL the platform hands us, and the blueprint that runs it.

A blueprint is code with no type checker and no import to fail, so a typo in it is found by
a failed deploy at the worst possible moment. These are cheap and catch the three things that
would actually break: the driver prefix, a command that stopped matching the project, and a
secret that quietly became something a person has to remember to set.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from db.session import DRIVER_PREFIX, with_driver

ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = ROOT / "render.yaml"
README = ROOT / "README.md"


@pytest.fixture(scope="module")
def service() -> dict[str, Any]:
    blueprint = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    services = blueprint["services"]
    assert len(services) == 1, "one web service; add a test if that changes"
    web: dict[str, Any] = services[0]
    return web


def env_var(service: dict[str, Any], key: str) -> dict[str, Any]:
    for entry in service["envVars"]:
        if entry["key"] == key:
            found: dict[str, Any] = entry
            return found
    raise AssertionError(f"{key} is not in render.yaml")


# --- the database URL a managed Postgres hands out --------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # already named, and left alone
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # some other driver is somebody's deliberate choice, not ours to rewrite
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("sqlite:///local.db", "sqlite:///local.db"),
        ("", ""),
    ],
)
def test_a_bare_postgres_url_gets_the_driver_this_project_installs(
    given: str, expected: str
) -> None:
    """Render hands out ``postgres://``; SQLAlchemy resolves that to a driver we do not have."""
    assert with_driver(given) == expected


def test_a_password_that_looks_like_a_prefix_is_not_mangled() -> None:
    """Only the scheme is rewritten, and only once."""
    url = "postgres://user:postgres://@host/db"
    assert with_driver(url) == f"{DRIVER_PREFIX}user:postgres://@host/db"


# --- the blueprint ------------------------------------------------------------------------------


def test_the_blueprint_parses_and_describes_one_python_web_service(service: dict[str, Any]) -> None:
    assert service["type"] == "web"
    assert service["runtime"] == "python"
    assert service["name"]


def test_the_build_installs_the_locked_dependency_set(service: dict[str, Any]) -> None:
    """``--frozen`` fails rather than re-resolving, so production runs what the tests ran."""
    build = service["buildCommand"]
    assert "uv" in build
    assert "uv sync --frozen" in build


def test_the_build_leaves_the_development_tools_out_of_production(
    service: dict[str, Any],
) -> None:
    """``--no-dev``: a type checker, a linter and a test database are not part of serving.

    They are not merely wasted install time. ``pgserver`` ships a whole PostgreSQL, and a
    type checker on a production image is a thing a person can be tempted to run against
    live code. Nothing in the dev group is imported outside ``tests/``, so leaving it out
    cannot break a boot - and if something ever does import one, the deploy fails rather
    than the group quietly coming back.
    """
    assert "--no-dev" in service["buildCommand"]


def test_the_tools_the_deploy_skips_are_declared_as_development_only() -> None:
    """``--no-dev`` only skips what is *in* the dev group, so this fails if one moves out."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = " ".join(project["project"]["dependencies"]).lower()
    development = " ".join(project["dependency-groups"]["dev"]).lower()
    for tool in ("mypy", "ruff", "pgserver", "hypothesis", "pytest"):
        assert tool in development, f"{tool} is not in the dev group"
        assert tool not in runtime, f"{tool} is a runtime dependency and ships either way"


def test_the_deploy_migrates_before_the_new_code_serves_anything(service: dict[str, Any]) -> None:
    assert service["preDeployCommand"] == "uv run alembic upgrade head"


def test_the_start_command_serves_this_app_on_the_port_render_gives_it(
    service: dict[str, Any],
) -> None:
    start = service["startCommand"]
    assert "uvicorn api.main:app" in start
    assert "--host 0.0.0.0" in start
    assert "$PORT" in start


def test_the_health_check_is_a_route_that_answers_without_a_session(
    service: dict[str, Any],
) -> None:
    """Everything else redirects to it, so nothing else is a health check."""
    assert service["healthCheckPath"] == "/login"


def test_the_python_version_matches_the_one_pinned_for_developers(
    service: dict[str, Any],
) -> None:
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert env_var(service, "PYTHON_VERSION")["value"] == pinned


def test_the_database_url_is_set_by_hand_and_never_committed(service: dict[str, Any]) -> None:
    assert env_var(service, "DATABASE_URL")["sync"] is False
    assert "value" not in env_var(service, "DATABASE_URL")


def test_the_session_secret_is_generated_rather_than_defaulted(service: dict[str, Any]) -> None:
    """A default secret is a signing key every copy of this repository knows."""
    secret = env_var(service, "SESSION_SECRET")
    assert secret["generateValue"] is True
    assert "value" not in secret and "sync" not in secret


def test_no_secret_is_sitting_in_the_blueprint(service: dict[str, Any]) -> None:
    for entry in service["envVars"]:
        if entry["key"] in {"DATABASE_URL", "SESSION_SECRET"}:
            assert "value" not in entry, f"{entry['key']} has a literal value in render.yaml"


def test_the_build_filter_covers_every_package_that_is_deployed(service: dict[str, Any]) -> None:
    """A package left out is a package whose change quietly does not deploy."""
    paths = set(service["buildFilter"]["paths"])
    packages = {
        directory.name
        for directory in ROOT.iterdir()
        if directory.is_dir()
        and (directory / "__init__.py").exists()
        and directory.name not in {"tests"}
    }
    missing = {name for name in packages if f"{name}/**" not in paths}
    assert not missing, f"render.yaml's buildFilter does not cover {sorted(missing)}"
    # and the files outside a package that a deploy still depends on
    assert {"alembic.ini", "pyproject.toml", "uv.lock", "render.yaml", ".python-version"} <= paths


# --- the README the blueprint refers to --------------------------------------------------------


@pytest.mark.parametrize(
    "heading",
    ["## Running it locally", "## Deploying", "### Creating the first user", "### Rotating"],
)
def test_the_readme_covers_what_an_operator_has_to_do(heading: str) -> None:
    assert heading in README.read_text(encoding="utf-8")


def test_the_readme_points_at_the_spec_rather_than_restating_it() -> None:
    body = README.read_text(encoding="utf-8")
    assert "SPEC.md" in body and "CLAUDE.md" in body


def test_the_readme_does_not_carry_a_secret_anyone_could_use() -> None:
    """Every example value has to be obviously an example."""
    body = README.read_text(encoding="utf-8")
    assert "SESSION_SECRET=" not in body
    assert "token_urlsafe" in body, "it should say how to generate one"
