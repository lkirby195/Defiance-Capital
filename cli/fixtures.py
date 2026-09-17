"""Read a fixture deal off disk and run the engine on it.  # SPEC §12 Phase 2

A fixture is the same JSON document ``tests/test_fixtures.py`` uses: ``inputs`` is a
``ScreenInputs`` document, and an optional ``underwrite.inputs`` is an ``UnderwriteInputs``
document with the verified values. Running one needs no database and no network - it is the
whole point of the fixture format - so the CLI can show the math on a deal before any of
the enrichment adapters exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config import Config
from engine.screen import screen
from engine.underwrite import underwrite
from schema.models import ScreenInputs, ScreenResult, UnderwriteInputs, UnderwriteResult


class FixtureError(ValueError):
    """The file is not a fixture, or does not carry what was asked of it."""


@dataclass(frozen=True)
class FixtureRun:
    """A fixture and what the engine made of it."""

    name: str
    description: str
    path: Path
    screen_inputs: ScreenInputs
    screen_result: ScreenResult
    underwrite_inputs: UnderwriteInputs | None
    underwrite_result: UnderwriteResult | None


def load_fixture(path: Path) -> dict[str, Any]:
    """Parse the fixture document, or raise ``FixtureError`` saying why it is not one."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "inputs" not in data:
        raise FixtureError(f"{path} has no 'inputs' block; it is not a deal fixture")
    loaded: dict[str, Any] = data
    return loaded


def has_underwrite(fixture: dict[str, Any]) -> bool:
    return "underwrite" in fixture


def run_fixture(path: Path, config: Config, with_underwrite: bool) -> FixtureRun:
    """Screen the fixture, and underwrite it when asked and the block is there."""
    fixture = load_fixture(path)
    screen_inputs = ScreenInputs.model_validate(fixture["inputs"])
    underwrite_inputs: UnderwriteInputs | None = None
    underwrite_result: UnderwriteResult | None = None
    if with_underwrite:
        if not has_underwrite(fixture):
            raise FixtureError(
                f"{path.name} has no 'underwrite' block: it carries no verified as-is value "
                "or ARV, and SPEC §8.1 requires both to underwrite"
            )
        underwrite_inputs = UnderwriteInputs.model_validate(fixture["underwrite"]["inputs"])
        underwrite_result = underwrite(underwrite_inputs, config)
    return FixtureRun(
        name=str(fixture.get("name", path.stem)),
        description=str(fixture.get("description", "")),
        path=path,
        screen_inputs=screen_inputs,
        screen_result=screen(screen_inputs, config),
        underwrite_inputs=underwrite_inputs,
        underwrite_result=underwrite_result,
    )
