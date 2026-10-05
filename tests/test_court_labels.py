"""The court search reads as words, not as stored codes.  # SPEC §7.2, and CLAUDE.md

``BANKRUPTCY_IN_LOOKBACK`` is a stable flag code: it is what a ``screens`` row stores, what a
config severity is keyed on, and what a later reader greps for. It is not a thing anybody
picked off a form. Everywhere the court block is *read* - the outcome select, the matter
table's code and lien-kind columns, the severity on a flag, the readiness checklist's row -
it goes through ``schema/labels.py`` and comes out title case with spaces, exactly as
``SPLIT_DRAW`` comes out "Split Draw" under the Loan Type box.

The stored value is untouched by any of it. Every ``<option>`` below still posts its code, and
every flag tag is still styled by the code in its class - only the words a person reads change.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy.orm import Session

from db.models import Deal
from schema.labels import enum_label
from schema.models import CourtFlag, CourtRecordsStatus, LienKind, Severity
from services import latest_screen, screen_result
from tests.conftest import QueueClient, requires_db, store_deal

pytestmark = requires_db

# The option text, by its stored value, as the page renders it.
OPTION = re.compile(r'<option value="([^"]*)"[^>]*>([^<]*)</option>')


def options(body: str, name: str) -> dict[str, str]:
    """One ``<select>``'s choices: stored value -> the words beside it."""
    block = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', body, re.S)
    assert block is not None, name
    return {value: text.strip() for value, text in OPTION.findall(block.group(1)) if value}


# --- the rule itself -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "label"),
    [
        (CourtRecordsStatus.NOT_CHECKED, "Not Checked"),
        (CourtRecordsStatus.CLEAN, "Clean"),
        (CourtRecordsStatus.FLAGS, "Flags"),
        (CourtFlag.BANKRUPTCY_IN_LOOKBACK, "Bankruptcy In Lookback"),
        (CourtFlag.ACTIVE_FORECLOSURE_AS_OWNER, "Active Foreclosure As Owner"),
        (CourtFlag.UNSATISFIED_JUDGMENT_OVER_THRESHOLD, "Unsatisfied Judgment Over Threshold"),
        (CourtFlag.OPEN_TAX_LIEN, "Open Tax Lien"),
        (CourtFlag.SUBJECT_PROPERTY_LIEN_OR_LIS_PENDENS, "Subject Property Lien Or Lis Pendens"),
        (Severity.HARD, "Hard"),
        (Severity.SOFT, "Soft"),
        (Severity.INFO, "Info"),
    ],
)
def test_every_court_enum_reads_as_title_case_with_spaces(code: Any, label: str) -> None:
    assert enum_label(code) == label


def test_every_member_of_every_court_enum_has_a_label_and_keeps_its_code() -> None:
    """No member is left rendering as its own SCREAMING_SNAKE code."""
    for member in (*CourtRecordsStatus, *CourtFlag, *LienKind, *Severity):
        label = enum_label(member)
        assert label and label != member.value, member
        assert "_" not in label, member
        assert member.value == member.name  # the stored code is untouched


# --- and on the two pages that render it ---------------------------------------------------------


def test_the_court_search_is_the_deal_page_s_and_labels_the_outcome_and_the_matter_columns(
    client: QueueClient, stored_deal: Deal
) -> None:
    """The team form has no court search on it (SPEC §8.1); the override block does."""
    form = client.get("/queue/new").text
    assert 'name="court_records_status"' not in form and 'name="matter_code"' not in form
    body = client.get(f"/queue/deals/{stored_deal.id}").text
    assert options(body, "court_records_status") == {
        member.value: enum_label(member) for member in CourtRecordsStatus
    }
    assert options(body, "matter_code") == {
        member.value: enum_label(member) for member in CourtFlag
    }
    assert options(body, "matter_lien_kind") == {
        member.value: enum_label(member) for member in LienKind
    }
    # The caption stopped shouting the codes at people too.
    assert "Not Checked is not Clean" in body
    assert "NOT_CHECKED is not CLEAN" not in body


def test_the_deal_pages_override_block_labels_the_same_three(
    client: QueueClient, stored_deal: Deal
) -> None:
    body = client.get(f"/queue/deals/{stored_deal.id}").text
    assert options(body, "court_records_status")["NOT_CHECKED"] == "Not Checked"
    assert options(body, "matter_code")["OPEN_TAX_LIEN"] == "Open Tax Lien"
    assert options(body, "matter_lien_kind") == {
        member.value: enum_label(member) for member in LienKind
    }


def test_a_verdict_is_styled_by_its_code_and_a_severity_reads_as_a_word(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The class the stylesheet is keyed on stays the code; the words a person reads are the
    label. The flags themselves are on the stored row, not the page (SPEC §9)."""
    deal = store_deal(db_session, {**team_entry, "estimated_sale_price_team": "220000.00"})
    # the leverage declines it, so the ledger stands down: a conflict, with the verdict kept
    assert client.post(f"/queue/deals/{deal.id}/run").status_code == 409

    body = client.get(f"/queue/deals/{deal.id}").text
    assert '<span class="tag DECLINE">DECLINE</span>' in body
    assert "LTV_OVER_CAP" not in body
    row = latest_screen(db_session, deal.id)
    assert row is not None
    (flag,) = [flag for flag in screen_result(row).flags if flag.code.value == "LTV_OVER_CAP"]
    assert flag.severity is Severity.HARD and enum_label(flag.severity) == "Hard"


def test_the_readiness_checklist_shows_the_court_search_as_words(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    row = re.search(r"<td>Court and filing search</td>\s*<td[^>]*>\s*([^<\s]+)", body)
    assert row is not None
    assert row.group(1) == "Clean"
