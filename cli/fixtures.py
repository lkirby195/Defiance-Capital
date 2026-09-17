"""Read a fixture deal off disk and run the engine on it.  # SPEC §12 Phase 2

A fixture comes in one of two shapes, and ``tests/test_fixtures.py`` uses the same loader:

``inputs``
    A ``ScreenInputs`` document handed straight to the engine, with an optional
    ``underwrite.inputs`` (an ``UnderwriteInputs`` document). This is the engine's own test
    format: every value is already resolved, so it exercises the math and nothing else.

``team_entry``
    A team-entry form payload (SPEC §4.2), including the team's own valuation and court
    search. It is normalized into an ``IntakeRecord``, built into a ``deals`` row that is
    never persisted, and assembled by ``services.assemble`` - the same path
    ``POST /deals/{id}/screen`` takes, precedence rules and all. An optional
    ``underwrite.request`` is an ``UnderwriteRequest``, assembled the same way. This is how
    a fixture proves that a deal the team has valued and searched reaches Go before a single
    enrichment adapter exists.

Either way there is no database and no network: the CLI can show the math on a deal, and on
the assembly in front of it, before any of it is wired to anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config import Config
from db.models import Deal
from db.repository import transient_deal
from engine.screen import screen
from engine.underwrite import underwrite
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import (
    Channel,
    ScreenInputs,
    ScreenResult,
    UnderwriteInputs,
    UnderwriteResult,
)
from services.assemble import screen_inputs, underwrite_inputs
from services.requests import UnderwriteRequest


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
    deal: Deal | None  # the assembled row, for a team-entry fixture


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
    if not isinstance(data, dict) or not ({"inputs", "team_entry"} & set(data)):
        raise FixtureError(
            f"{path} has no 'inputs' or 'team_entry' block; it is not a deal fixture"
        )
    loaded: dict[str, Any] = data
    return loaded


def is_team_entry(fixture: dict[str, Any]) -> bool:
    """True when the fixture runs through the service assembly rather than straight in."""
    return "team_entry" in fixture


def has_underwrite(fixture: dict[str, Any]) -> bool:
    return "underwrite" in fixture


def deal_from_fixture(fixture: dict[str, Any]) -> Deal:
    """Normalize the team-entry payload into an unpersisted ``deals`` row."""
    payload = fixture["team_entry"]
    record = normalize(parse_team_form(TeamEntryForm(**payload)), Channel.TEAM, raw_payload=payload)
    return transient_deal(record)


def screen_inputs_for(fixture: dict[str, Any], deal: Deal | None) -> ScreenInputs:
    """The screen inputs, assembled from the deal when there is one."""
    if deal is not None:
        return screen_inputs(deal)
    return ScreenInputs.model_validate(fixture["inputs"])


def underwrite_inputs_for(
    fixture: dict[str, Any], deal: Deal | None, path: Path
) -> UnderwriteInputs:
    """The underwrite inputs, assembled from the deal and its request when there is one."""
    block = fixture["underwrite"]
    if deal is not None:
        if "request" not in block:
            raise FixtureError(
                f"{path.name} is a team-entry fixture, so its underwrite block needs a "
                "'request' (an UnderwriteRequest), not 'inputs'"
            )
        return underwrite_inputs(deal, UnderwriteRequest(**block["request"]))
    return UnderwriteInputs.model_validate(block["inputs"])


def run_fixture(path: Path, config: Config, with_underwrite: bool) -> FixtureRun:
    """Screen the fixture, and underwrite it when asked and the block is there."""
    fixture = load_fixture(path)
    deal = deal_from_fixture(fixture) if is_team_entry(fixture) else None
    inputs = screen_inputs_for(fixture, deal)
    underwrite_inputs_used: UnderwriteInputs | None = None
    underwrite_result: UnderwriteResult | None = None
    if with_underwrite:
        if not has_underwrite(fixture):
            raise FixtureError(
                f"{path.name} has no 'underwrite' block: it carries no verified as-is value "
                "or ARV, and SPEC §8.1 requires both to underwrite"
            )
        underwrite_inputs_used = underwrite_inputs_for(fixture, deal, path)
        underwrite_result = underwrite(underwrite_inputs_used, config)
    return FixtureRun(
        name=str(fixture.get("name", path.stem)),
        description=str(fixture.get("description", "")),
        path=path,
        screen_inputs=inputs,
        screen_result=screen(inputs, config),
        underwrite_inputs=underwrite_inputs_used,
        underwrite_result=underwrite_result,
        deal=deal,
    )
