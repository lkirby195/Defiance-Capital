"""Service layer: the seam between the pure engine and the database.  # SPEC §7, §8

``engine/`` never touches I/O; ``api/`` never touches the engine directly. Everything that
loads a deal, assembles typed inputs, runs the math, and records the result lives here.
"""

from __future__ import annotations

from services.enrichment import NO_ADAPTER_VALUES, AdapterValues, adapter_values
from services.errors import (
    DealNotFound,
    DealNotReady,
    DealNotUnderwritable,
    ServiceError,
)
from services.lifecycle import (
    advance_after_screen,
    advance_for_underwrite,
    check_intake_complete,
    check_underwritable,
    status_after_screen,
    status_after_underwrite,
)
from services.persistence import (
    latest_screen,
    latest_underwrite,
    record_screen,
    record_underwrite,
    screen_result,
    underwrite_result,
)
from services.requests import UnderwriteRequest
from services.runner import load_deal, run_screen, run_underwrite

__all__ = [
    "NO_ADAPTER_VALUES",
    "AdapterValues",
    "DealNotFound",
    "DealNotReady",
    "DealNotUnderwritable",
    "ServiceError",
    "UnderwriteRequest",
    "adapter_values",
    "advance_after_screen",
    "advance_for_underwrite",
    "check_intake_complete",
    "check_underwritable",
    "latest_screen",
    "latest_underwrite",
    "load_deal",
    "record_screen",
    "record_underwrite",
    "run_screen",
    "run_underwrite",
    "screen_result",
    "status_after_screen",
    "status_after_underwrite",
    "underwrite_result",
]
