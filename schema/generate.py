"""Generate ``schema/intake.json`` from ``IntakeRecord``.  # SPEC §4.5

Run ``uv run python -m schema.generate`` after changing the model and commit
the result. ``tests/test_schema.py`` asserts the committed file is current.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from schema.models import IntakeRecord

INTAKE_JSON = Path(__file__).with_name("intake.json")


def intake_json_schema() -> dict[str, Any]:
    """Return the JSON schema for ``IntakeRecord`` in validation mode."""
    return IntakeRecord.model_json_schema()


def render() -> str:
    """Canonical text form of the schema: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(intake_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    INTAKE_JSON.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {INTAKE_JSON}")


if __name__ == "__main__":
    main()
