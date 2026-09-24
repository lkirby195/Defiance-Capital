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


def test_the_team_entry_form_labels_the_outcome_and_the_matter_columns(
    client: QueueClient,
) -> None:
    body = client.get("/queue/new").text
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


def test_a_flags_severity_reads_as_a_word_and_is_still_styled_by_its_code(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The class the stylesheet is keyed on stays the code; the text beside it is the word."""
    deal = store_deal(db_session, {**team_entry, "estimated_sale_price_team": "220000.00"})
    assert client.post(f"/queue/deals/{deal.id}/screen", follow_redirects=False).status_code == 303

    body = client.get(f"/queue/deals/{deal.id}").text
    assert '<span class="tag HARD">Hard</span>' in body
    assert '<span class="tag HARD">HARD</span>' not in body
    # ...and the flag's own code is still printed, because a code is what it is.
    assert "LTV_OVER_CAP" in body


def test_the_readiness_checklist_shows_the_court_search_as_words(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    row = re.search(r"<td>Court and filing search</td>\s*<td[^>]*>\s*([^<\s]+)", body)
    assert row is not None
    assert row.group(1) == "Clean"
