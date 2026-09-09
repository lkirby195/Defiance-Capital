"""Every synthetic deal in fixtures/synthetic/deals screens to its expected verdict.  # SPEC §7, §12

Each fixture holds ``inputs`` (a ``ScreenInputs`` document) and ``expected`` (verdict,
flag codes with severities, components, sizing numbers, substrings the reasons and the
suggested reply must contain). Adding a fixture adds a test case.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.config import Config
from engine.screen import screen
from engine.version import ENGINE_VERSION
from schema.models import CapStatus, LeverageMetric, Product, ScreenInputs, ScreenResult, Verdict

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))
CONFIG = Config.load()


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def run(path: Path) -> tuple[dict[str, Any], ScreenResult]:
    fixture = load(path)
    return fixture, screen(ScreenInputs.model_validate(fixture["inputs"]), CONFIG)


def test_fixture_set_covers_every_product_and_verdict() -> None:
    assert len(FIXTURE_FILES) >= 6
    products = {load(p)["inputs"]["deal"]["product"] for p in FIXTURE_FILES}
    verdicts = {load(p)["expected"]["verdict"] for p in FIXTURE_FILES}
    assert products == {p.value for p in Product}
    assert verdicts == {v.value for v in Verdict}


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_verdict_flags_and_reasons(path: Path) -> None:
    fixture, result = run(path)
    expected = fixture["expected"]
    assert fixture["name"] == path.stem

    assert result.verdict.value == expected["verdict"]
    got = Counter((f.code.value, f.severity.value) for f in result.flags)
    want = Counter((f["code"], f["severity"]) for f in expected["flags"])
    assert got == want, f"flags differ: got {got}, want {want}"
    assert len(result.reasons) == len(result.flags)
    for needle in expected["reasons_contain"]:
        assert any(needle in reason for reason in result.reasons), needle
    for needle in expected["reply_contains"]:
        assert needle in result.suggested_reply, needle


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_components_and_sizing(path: Path) -> None:
    fixture, result = run(path)
    expected = fixture["expected"]
    components, sizing = result.components, result.sizing

    assert components.credit_tranche.value == expected["credit_tranche"]
    assert components.experience_tier.value == expected["experience_tier"]
    assert (
        components.repeat_borrower_override_applied is expected["repeat_borrower_override_applied"]
    )
    assert sizing.all_pass is expected["all_pass"]
    assert sizing.commitment == Decimal(expected["commitment"])
    assert sizing.funded_at_close == Decimal(expected["funded_at_close"])
    if "split" in expected:
        assert sizing.split is not None
        assert sizing.split.purchase_portion == Decimal(expected["split"]["purchase_portion"])
        assert sizing.split.rehab_portion == Decimal(expected["split"]["rehab_portion"])
    else:
        assert sizing.split is None
    for name, status in expected["metrics"].items():
        assert sizing.metrics[LeverageMetric(name)].status is CapStatus(status), name


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_fixture_result_is_recorded_and_serializable(path: Path) -> None:
    _, result = run(path)
    assert result.engine_version == ENGINE_VERSION
    assert result.config_hash == CONFIG.config_hash
    for flag in result.flags:
        assert flag.message.strip(), flag.code
    again = ScreenResult.model_validate_json(result.model_dump_json())
    assert again == result
    for check in again.sizing.metrics.values():
        assert isinstance(check.cap, Decimal)
        assert check.actual is None or isinstance(check.actual, Decimal)
