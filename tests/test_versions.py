"""Versions: one per analysis run, a snapshot of its inputs, one Working.  # SPEC §5, §9

The weight is on the snapshot. A version is only worth keeping if it can be reproduced, so
the central test runs a deal, then changes every input a person can reach - every box on the
intake form, all sixteen on the Underwriting Assumptions panel, the court search - runs it
again until nothing matches, restores the first version and checks that the run it makes is
the first run to the last digit: the whole ``UnderwriteResult``, ledger and pricing and all,
and the whole ``ScreenResult``. A structural test beside it holds the snapshot to the table,
so a column added to ``deals`` later is either snapshotted or named as deliberately not.

Around it: the naming defaults, the one-Working rule in the service and in the database, the
switcher and the badge on the deal page, Home reading the Working version rather than the
latest run, the History list, and the CLI's ``--version``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import pytest
from openpyxl import load_workbook
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import db.session as db_session_module
from api.intake_form import intake_form_values
from api.routes.queue import workbook_filename
from cli.main import main
from db.models import AuditLog, Deal, Property, Version
from engine.version import ENGINE_VERSION
from schema.models import AuditAction, State, Status
from services import (
    DEALS,
    NOT_SNAPSHOTTED,
    SNAPSHOT_COLUMNS,
    HistoryKind,
    Section,
    auto_run,
    deal_history,
    deal_versions,
    get_version,
    home_view,
    mark_working,
    run_analysis,
    version_results,
    working_version,
)
from tests.conftest import ACTOR, USER_EMAIL, QueueClient, form_body, requires_db, store_deal

pytestmark = requires_db

D = Decimal
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/synthetic/deals"
# The version the team-entry fixture arrives with off the New Deal form (SPEC §5): the
# borrower's last name, the street, the state and "Team" (``tests/test_version_names.py``).
ARRIVAL = "Ortiz; 3320 Meade St, CO - Team"


def notice(response: Any) -> str:
    return unquote(response.headers["location"].split("notice=", 1)[1])


def run(client: QueueClient, deal_id: UUID, name: str = "") -> Any:
    data = {"version_name": name} if name else {}
    return client.post(f"/queue/deals/{deal_id}/run", data=data, follow_redirects=False)


def save_and_run(client: QueueClient, deal_id: UUID, **boxes: str) -> Any:
    return client.post(f"/queue/deals/{deal_id}/assumptions", data=boxes, follow_redirects=False)


def names(session: Session, deal_id: UUID) -> list[str]:
    session.expire_all()
    return [version.name for version in reversed(deal_versions(session, deal_id))]


def working_count(session: Session, deal_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Version)
            .where(Version.deal_id == deal_id, Version.is_working)
        )
        or 0
    )


def audit(session: Session, deal_id: UUID, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.table_name == DEALS,
                AuditLog.row_id == str(deal_id),
                AuditLog.action == action.value,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


# --- naming -------------------------------------------------------------------------------------


def test_naming_defaults_auto_and_typed(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The deal's name on arrival, ``v2`` unnamed, a typed name trimmed, ``v4`` unnamed again."""
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303
    (deal,) = list(db_session.scalars(select(Deal)))
    assert notice(created).endswith(f"Saved as {ARRIVAL} (Working).")

    assert run(client, deal.id).status_code == 303
    named = run(client, deal.id, "  Base   case  ")
    assert named.status_code == 303
    assert named.headers["location"].startswith(f"/queue/deals/{deal.id}?version=3&")
    assert notice(named).endswith("Saved as Base case (draft).")
    assert run(client, deal.id).status_code == 303
    assert names(db_session, deal.id) == [ARRIVAL, "v2", "Base case", "v4"]
    actors = [version.actor for version in reversed(deal_versions(db_session, deal.id))]
    assert actors == ["system", USER_EMAIL, USER_EMAIL, USER_EMAIL]


def test_save_and_run_takes_a_name_and_a_long_one_is_refused(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    response = save_and_run(
        client, deal_id, estimated_sale_price_team="$210,000", version_name="Higher ARV"
    )
    assert response.status_code == 303, response.text
    assert names(db_session, deal_id) == ["Higher ARV"]

    too_long = save_and_run(client, deal_id, version_name="x" * 101)
    assert too_long.status_code == 422
    assert "at most 100 characters" in too_long.text
    refused = run(client, deal_id, "y" * 101)
    assert refused.status_code == 422
    assert names(db_session, deal_id) == ["Higher ARV"]


def test_a_run_that_records_nothing_is_no_version(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, {**team_entry, "purchase_price": None})
    response = run(client, deal.id)
    assert response.status_code == 422
    assert names(db_session, deal.id) == []


# --- one Working version (SPEC §5) ----------------------------------------------------------------


def test_the_first_run_is_working_and_every_later_one_a_draft(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal = deal_with_overrides
    first = run_analysis(db_session, deal, actor=ACTOR)
    second = run_analysis(db_session, deal, actor=ACTOR, name="Draft idea")
    db_session.commit()
    assert first.version is not None and first.version.working
    assert second.version is not None and not second.version.working
    assert working_count(db_session, deal.id) == 1
    working = working_version(db_session, deal.id)
    assert working is not None and working.number == 1


def test_mark_working_moves_the_mark_and_keeps_exactly_one(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    deal = deal_with_overrides
    for _ in range(3):
        run_analysis(db_session, deal, actor=ACTOR)
    db_session.commit()

    mark_working(db_session, deal.id, 3, actor=ACTOR)
    db_session.commit()
    assert working_count(db_session, deal.id) == 1
    assert [v.number for v in deal_versions(db_session, deal.id) if v.is_working] == [3]
    (row,) = audit(db_session, deal.id, AuditAction.VERSION_MARKED_WORKING)
    assert row.actor == ACTOR
    assert row.before == {"version": 1, "name": "v1"} and row.after == {"version": 3, "name": "v3"}

    mark_working(db_session, deal.id, 3, actor=ACTOR)  # already Working: nothing written
    db_session.commit()
    assert len(audit(db_session, deal.id, AuditAction.VERSION_MARKED_WORKING)) == 1
    mark_working(db_session, deal.id, 2, actor=ACTOR)
    db_session.commit()
    assert working_count(db_session, deal.id) == 1
    assert [v.number for v in deal_versions(db_session, deal.id) if v.is_working] == [2]


def test_the_database_refuses_a_second_working_version(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_analysis(db_session, deal_with_overrides, actor=ACTOR)
    run_analysis(db_session, deal_with_overrides, actor=ACTOR)
    db_session.commit()
    draft = get_version(db_session, deal_with_overrides.id, 2)
    draft.is_working = True
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_every_run_writes_a_version_recorded_row(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    run_analysis(db_session, deal_with_overrides, actor=ACTOR, name="First")
    db_session.commit()
    (row,) = audit(db_session, deal_with_overrides.id, AuditAction.VERSION_RECORDED)
    version = get_version(db_session, deal_with_overrides.id, 1)
    assert row.actor == ACTOR and row.after is not None
    assert row.after["version"] == 1 and row.after["name"] == "First" and row.after["working"]
    assert row.after["screen_id"] == str(version.screen_id)
    assert row.after["underwrite_id"] == str(version.underwrite_id)


# --- the deal page (SPEC §9) ----------------------------------------------------------------------


def test_the_page_shows_working_by_default_and_a_switcher_over_every_version(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id, "Base").status_code == 303
    assert (
        save_and_run(client, deal_id, interest_rate="15%", version_name="Rate up").status_code
        == 303
    )

    body = client.get(f"/queue/deals/{deal_id}").text
    # the switcher names the Working version; the header has no chip for it
    assert 'id="working-version"' not in body and "Working: Base" not in body
    assert 'id="draft-badge"' not in body
    section = body[body.index('<section class="panel" id="versions">') :]
    section = section[: section.index("</section>")]
    switcher = f'<form class="actions switcher" method="get" action="/queue/deals/{deal_id}">'
    assert switcher in section
    assert section.count("<option ") == 2
    assert '<option value="1" selected>Base · Working' in section
    assert '<option value="2">Rate up · Draft' in section
    assert ">Mark as Working</button>" not in section  # the one shown is already Working
    assert f'action="/queue/deals/{deal_id}/versions/1/restore"' in section
    # the switcher sits in the header, above every output and the panel
    assert body.index('id="versions"') < body.index('id="pricing"')
    assert body.index('id="pricing"') < body.index('id="assumptions"')

    draft = client.get(f"/queue/deals/{deal_id}?version=2").text
    assert 'id="draft-badge"' in draft and '<option value="1">Base · Working' in draft
    assert '<option value="2" selected>Rate up · Draft' in draft
    assert f'action="/queue/deals/{deal_id}/versions/2/working"' in draft
    assert ">Mark as Working</button>" in draft
    v1, v2 = (version_results(db_session, get_version(db_session, deal_id, n)) for n in (1, 2))
    assert v1.underwrite is not None and v2.underwrite is not None
    irr1, irr2 = v1.underwrite.return_overview.irr, v2.underwrite.return_overview.irr
    assert irr1 is not None and irr2 is not None and irr1 != irr2
    rate = f"{irr2 * 100:.2f}%"
    assert f"IRR <strong>{rate}</strong>" in draft
    assert f"{irr1 * 100:.2f}%" not in draft.split('id="history"')[0]


def test_an_unknown_version_goes_back_to_the_deal(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    response = client.get(
        f"/queue/deals/{deal_with_overrides.id}?version=9", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/queue/deals/{deal_with_overrides.id}?")
    assert notice(response) == "That version does not exist."


def test_mark_as_working_from_the_page(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    run(client, deal_id)
    run(client, deal_id, "Better")
    response = client.post(f"/queue/deals/{deal_id}/versions/2/working", follow_redirects=False)
    assert response.status_code == 303
    assert notice(response) == "Better is now the Working version."
    db_session.expire_all()
    working = working_version(db_session, deal_id)
    assert working is not None and working.name == "Better"
    (row,) = audit(db_session, deal_id, AuditAction.VERSION_MARKED_WORKING)
    assert row.actor == USER_EMAIL
    missing = client.post(f"/queue/deals/{deal_id}/versions/7/working", follow_redirects=False)
    assert missing.status_code == 303 and notice(missing) == "That version does not exist."


# --- Home reads the Working version (SPEC §9.1) ---------------------------------------------------


def test_home_shows_the_working_numbers_after_a_draft_run(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id).status_code == 303
    first = get_version(db_session, deal_id, 1)
    first_irr = version_results(db_session, first).underwrite
    assert first_irr is not None

    assert save_and_run(client, deal_id, interest_rate="16%").status_code == 303
    db_session.expire_all()
    draft = version_results(db_session, get_version(db_session, deal_id, 2)).underwrite
    assert draft is not None
    assert draft.return_overview.irr != first_irr.return_overview.irr

    [entry] = home_view(db_session).section(Section.DCF).entries
    assert entry.working is not None and entry.working.name == "v1"
    assert entry.irr == first_irr.return_overview.irr.quantize(D("0.00001"))  # type: ignore[union-attr]
    page = client.get("/queue").text
    assert f"{entry.irr * 100:.2f}%" in page  # type: ignore[operator]

    mark_working(db_session, deal_id, 2, actor=ACTOR)
    db_session.commit()
    [entry] = home_view(db_session).section(Section.DCF).entries
    assert entry.working is not None and entry.working.name == "v2"
    assert entry.irr == draft.return_overview.irr.quantize(D("0.00001"))  # type: ignore[union-attr]


# --- the snapshot (SPEC §5) -----------------------------------------------------------------------


def test_the_snapshot_covers_every_deal_column_but_the_rows_identity() -> None:
    """A new ``deals`` column is snapshotted unless it is named here as not an input."""
    columns = {column.name for column in Deal.__table__.columns}
    assert set(SNAPSHOT_COLUMNS) | NOT_SNAPSHOTTED == columns
    assert not set(SNAPSHOT_COLUMNS) & NOT_SNAPSHOTTED
    assert NOT_SNAPSHOTTED == {
        "id",
        "channel",
        "status",
        "intake_source",
        "referral_note",
        # the thank-you link and what it showed the borrower (SPEC 4.2): facts, not inputs
        "thanks_token",
        "thanks_token_expires_at",
        "indicative_terms_shown_at",
        "indicative_terms_shown",
        "created_at",
        "updated_at",
    }


# Every input the restore test changes, by the column it lands in. ``product_source`` stays
# ENTERED throughout and ``term_bucket`` is the borrower's answer, which the team form never
# sets; the rest of SNAPSHOT_COLUMNS is here.
CHANGED_BY_THE_TEST = set(SNAPSHOT_COLUMNS) - {
    "product_source",
    "term_bucket",
    "term_stub_days",
    "estimated_sale_price_borrower",
    "monthly_rent_borrower",
    "missing_fields",
    "credit_authorization_signed",
}


def test_restoring_a_version_after_every_input_changed_reproduces_it_exactly(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id, "Original").status_code == 303
    original = version_results(db_session, get_version(db_session, deal_id, 1))
    assert original.underwrite is not None and original.screen is not None
    before = dict(original.version.inputs_snapshot["deal"])

    # every box on the intake form: who, where, what, how much, how long (Auto v2)
    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None
    intake = {name: value for name, value in intake_form_values(deal).items() if value}
    intake.update(
        entity_name="Larimer Flips LLC",
        borrower_name="Casey Ortega",
        borrower_phone="303-555-0188",
        borrower_email="casey@larimer.example",
        credit_range="T2",
        experience_bucket="3_5",
        repeat_borrower="true",
        address="88 Larimer St, Denver, CO 80202",
        city="Denver",
        state="CO",
        units="2",
        structures="2",
        sf="1800",
        year_built="1961",
        year_renovated="2022",
        beds="4",
        baths="2.5",
        garage_spaces="2",
        purchase_price="$160,000",
        rehab_costs="$30,000",
        loan_requested="$140,000",
        loan_purpose="REFINANCE",
        product="SPLIT_DRAW",
        closing_date="2027-03-15",
        term_months="9",
    )
    edited = client.post(f"/queue/deals/{deal_id}/intake", data=intake, follow_redirects=False)
    assert edited.status_code == 303, edited.text

    # all sixteen boxes on the panel (v3)
    saved = save_and_run(
        client,
        deal_id,
        interest_rate="14%",
        origination_fee_pct="2.5%",
        contingency_pct="5%",
        closing_costs_usd="$1,500",
        holding_costs_pct_of_cost="3%",
        loan_purchase_portion="$115,000",
        loan_rehab_portion="$25,000",
        estimated_sale_price_team="$260,000",
        monthly_rent="$2,100",
        broker_selling_pct="5%",
        rental_expenses_pct_of_rent="40%",
        rental_takeout_rate="7%",
        take_back_legal_costs_usd="$6,000",
        take_back_lost_interest_months="4",
        flip_analysis="false",
        rental_analysis="true",
        version_name="Everything moved",
    )
    assert saved.status_code == 303, saved.text

    # and the court search (Auto v4)
    court = client.post(
        f"/queue/deals/{deal_id}/court",
        data={
            "court_records_status": "FLAGS",
            "court_records_as_of": "2026-10-01",
            "matter_code": "LANDLORD_TENANT_AS_LANDLORD",
        },
        follow_redirects=False,
    )
    assert court.status_code == 303, court.text
    db_session.expire_all()
    assert names(db_session, deal_id) == ["Original", "Auto v2", "Everything moved", "Auto v4"]
    moved = version_results(db_session, get_version(db_session, deal_id, 4))
    assert moved.underwrite is not None
    assert moved.underwrite.return_overview.irr != original.underwrite.return_overview.irr
    after = moved.version.inputs_snapshot["deal"]
    unchanged = {column for column in CHANGED_BY_THE_TEST if after[column] == before[column]}
    assert unchanged == set(), f"the test meant to change these and did not: {unchanged}"
    assert moved.version.inputs_snapshot["borrower"]["id"] != before["borrower_id"]
    assert moved.version.inputs_snapshot["property"]["state"] == "CO"

    # restore the first version: a new submission, every column back, a new version (v5)
    restored = client.post(f"/queue/deals/{deal_id}/versions/1/restore", follow_redirects=False)
    assert restored.status_code == 303, restored.text
    assert notice(restored).startswith("Inputs restored. Analysis recorded:")
    assert notice(restored).endswith("Saved as v5 (draft).")
    db_session.expire_all()
    again = version_results(db_session, get_version(db_session, deal_id, 5))
    assert again.version.actor == USER_EMAIL
    assert again.underwrite is not None and again.screen is not None
    assert again.underwrite.return_overview.irr == original.underwrite.return_overview.irr
    assert again.underwrite == original.underwrite
    assert again.screen == original.screen
    assert again.underwrite_inputs == original.underwrite_inputs
    assert again.version.inputs_snapshot["deal"] == before
    assert again.version.inputs_snapshot["borrower"] == original.version.inputs_snapshot["borrower"]
    assert again.version.inputs_snapshot["property"] == original.version.inputs_snapshot["property"]

    # ...and it said so: the intake re-applied and the columns put back, naming the version
    (intake_row,) = audit(db_session, deal_id, AuditAction.INTAKE_RESTORED)
    (restore_row,) = audit(db_session, deal_id, AuditAction.VERSION_RESTORED)
    for row in (intake_row, restore_row):
        assert row.actor == USER_EMAIL and row.after is not None
        assert row.after["restored_from_version"] == 1
        assert row.after["version_name"] == "Original"
    assert restore_row.after is not None and restore_row.before is not None
    assert D(restore_row.after["interest_rate"]) == D("0.13")
    assert D(restore_row.before["interest_rate"]) == D("0.14")


def test_restore_is_refused_on_a_closed_deal_and_a_backfilled_version(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id).status_code == 303
    version = get_version(db_session, deal_id, 1)
    version.inputs_snapshot = {"legacy": True, "intake": None}
    db_session.commit()
    legacy = client.post(f"/queue/deals/{deal_id}/versions/1/restore")
    assert legacy.status_code == 422
    assert "recorded before versions kept their inputs" in legacy.text
    page = client.get(f"/queue/deals/{deal_id}").text
    section = page[page.index('id="versions"') :]
    assert "disabled>Restore inputs</button>" in section

    run(client, deal_id)
    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None
    deal.status = Status.DEAD
    db_session.commit()
    closed = client.post(f"/queue/deals/{deal_id}/versions/2/restore")
    assert closed.status_code == 409 and "DEAD" in closed.text
    assert audit(db_session, deal_id, AuditAction.VERSION_RESTORED) == []


# --- History (SPEC §9) ----------------------------------------------------------------------------


def test_history_lists_versions_and_the_edits_no_run_picked_up(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    run(client, deal_id, "Base")
    run(client, deal_id)
    # an edit on a paused deal is stored and waits: no run, so it is an edit row
    client.post(f"/queue/deals/{deal_id}/pause", follow_redirects=False)
    db_session.expire_all()
    deal = db_session.get(Deal, deal_id)
    assert deal is not None
    intake = {name: value for name, value in intake_form_values(deal).items() if value}
    intake["purchase_price"] = "$151,000"
    edited = client.post(f"/queue/deals/{deal_id}/intake", data=intake, follow_redirects=False)
    assert edited.status_code == 303

    history = deal_history(db_session, deal_id)
    assert [entry.kind for entry in history] == [
        HistoryKind.EDIT,
        HistoryKind.VERSION,
        HistoryKind.VERSION,
    ]
    edit, second, first = history
    assert edit.note == "team edited" and edit.actor == USER_EMAIL and edit.restorable
    assert (second.name, second.working, second.actor) == ("v2", False, USER_EMAIL)
    assert (first.name, first.working) == ("Base", True)
    assert first.verdict is not None and first.irr is not None and first.restorable
    assert first.engine_version == second.engine_version

    body = client.get(f"/queue/deals/{deal_id}").text
    section = body[body.index('id="history"') :]
    assert "<th>When</th><th>Version</th><th>Who</th><th>Working</th><th>Engine</th>" in section
    assert f'href="/queue/deals/{deal_id}?version=1">View</a>' in section
    assert '<td class="small"><strong>edit</strong></td>' in section
    assert f'action="/queue/deals/{deal_id}/intake/{edit.id}/restore"' in section


# --- the CLI (SPEC §9.6) --------------------------------------------------------------------------


@pytest.fixture
def cli_database(
    monkeypatch: pytest.MonkeyPatch, test_engine: Engine, test_database_url: str
) -> Any:
    """Point the CLI at the test database for the duration of one test."""
    del test_engine
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    db_session_module.get_engine.cache_clear()
    yield
    db_session_module.get_engine.cache_clear()


def test_the_cli_prints_and_exports_the_working_version_unless_told(
    cli_database: None,
    db_session: Session,
    deal_with_overrides: Deal,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deal = deal_with_overrides
    run_analysis(db_session, deal, actor=ACTOR, name="Base")
    deal.interest_rate = D("0.16")
    run_analysis(db_session, deal, actor=ACTOR, name="Rate up")
    db_session.commit()
    base, rate_up = (
        version_results(db_session, get_version(db_session, deal.id, n)) for n in (1, 2)
    )
    assert base.underwrite is not None and rate_up.underwrite is not None

    assert main(["deals", "run", str(deal.id)]) == 0
    out = capsys.readouterr().out
    assert "Base (Working)" in out and "Rate up" not in out
    assert main(["deals", "run", str(deal.id), "--version", "2"]) == 0
    assert "Rate up (draft)" in capsys.readouterr().out
    assert main(["deals", "run", str(deal.id), "--version", "Rate up"]) == 0
    assert "Rate up (draft)" in capsys.readouterr().out

    working_book = tmp_path / "working.xlsx"
    draft_book = tmp_path / "draft.xlsx"
    assert main(["deals", "export", str(deal.id), str(working_book)]) == 0
    assert main(["deals", "export", str(deal.id), str(draft_book), "--version", "2"]) == 0
    assert "(Rate up)" in capsys.readouterr().out
    for path, result in ((working_book, base.underwrite), (draft_book, rate_up.underwrite)):
        sheet = load_workbook(path)["Pricing"]
        assert result.pricing is not None
        assert sheet.cell(row=5, column=9).value == result.pricing.rows[0].sentence

    assert main(["deals", "run", str(deal.id), "--version", "9"]) == 2
    assert "no version 9" in capsys.readouterr().err
    assert (
        main(["deals", "export", str(deal.id), str(tmp_path / "x.xlsx"), "--version", "Nope"]) == 2
    )


def test_the_cli_refuses_a_deal_with_no_working_version(
    cli_database: None, db_session: Session, team_entry: dict[str, Any], capsys: Any
) -> None:
    deal = store_deal(db_session, team_entry)
    assert main(["deals", "run", str(deal.id)]) == 2
    assert "no version Working" in capsys.readouterr().err


def test_a_fixture_deal_round_trips_through_a_version(db_session: Session) -> None:
    """Every team-entry fixture records a complete snapshot that names its submission."""
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if "team_entry" not in fixture:
            continue
        deal = store_deal(db_session, fixture["team_entry"])
        outcome = run_analysis(db_session, deal, actor=ACTOR)
        db_session.commit()
        assert outcome.version is not None, path.name
        version = get_version(db_session, deal.id, outcome.version.number)
        snapshot = version.inputs_snapshot
        assert snapshot["schema"] == 1
        assert set(snapshot["deal"]) == set(SNAPSHOT_COLUMNS)
        assert snapshot["intake"]["submission_id"] == str(deal.submissions[-1].id)
        assert snapshot["intake"]["payload"] == deal.submissions[-1].raw_payload


# --- the Working rule for automatic runs (SPEC §5, phase 8c prep) ---------------------------------


def edit_intake(client: QueueClient, session: Session, deal_id: UUID, **changes: str) -> Any:
    """Post Edit Intake with the deal's own values and ``changes``; it runs as system."""
    session.expire_all()
    deal = session.get(Deal, deal_id)
    assert deal is not None
    body = {name: value for name, value in intake_form_values(deal).items() if value}
    body.update(changes)
    return client.post(f"/queue/deals/{deal_id}/intake", data=body, follow_redirects=False)


def save_court(client: QueueClient, deal_id: UUID) -> Any:
    return client.post(
        f"/queue/deals/{deal_id}/court",
        data={"court_records_status": "CLEAN", "court_records_as_of": "2026-10-02"},
        follow_redirects=False,
    )


def marks(session: Session, deal_id: UUID) -> list[tuple[str, bool, bool]]:
    """(name, Working, human-selected) for every version, oldest first."""
    session.expire_all()
    return [
        (version.name, version.is_working, version.human_selected)
        for version in reversed(deal_versions(session, deal_id))
    ]


def test_an_automatic_run_takes_the_mark_from_a_version_nobody_chose(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The arrival run, a person's unnamed run (a draft), then each automatic run moves it."""
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303
    (deal,) = list(db_session.scalars(select(Deal)))
    assert run(client, deal.id).status_code == 303
    assert marks(db_session, deal.id) == [(ARRIVAL, True, False), ("v2", False, False)]

    edited = edit_intake(client, db_session, deal.id, purchase_price="$201,000")
    assert edited.status_code == 303, edited.text
    assert notice(edited).endswith("Saved as Auto v3 (Working).")
    assert marks(db_session, deal.id) == [
        (ARRIVAL, False, False),
        ("v2", False, False),
        ("Auto v3", True, False),
    ]
    assert save_court(client, deal.id).status_code == 303
    assert marks(db_session, deal.id)[-1] == ("Auto v4", True, False)
    assert working_count(db_session, deal.id) == 1
    rows = audit(db_session, deal.id, AuditAction.VERSION_RECORDED)
    assert [row.after["replaced_working"] for row in rows if row.after] == [None, None, 1, 3]
    assert [row.after["human_selected"] for row in rows if row.after] == [False] * 4


def test_an_automatic_run_is_a_draft_beside_a_version_a_person_named(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id, "Base case").status_code == 303
    assert marks(db_session, deal_id) == [("Base case", True, True)]

    edited = edit_intake(client, db_session, deal_id, purchase_price="$151,000")
    assert edited.status_code == 303, edited.text
    assert notice(edited).endswith("Saved as Auto v2 (draft).")
    assert save_court(client, deal_id).status_code == 303
    assert marks(db_session, deal_id) == [
        ("Base case", True, True),
        ("Auto v2", False, False),
        ("Auto v3", False, False),
    ]
    [entry] = home_view(db_session).section(Section.DCF).entries
    assert entry.working is not None and entry.working.name == "Base case"


def test_an_automatic_run_is_a_draft_beside_a_version_marked_working(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303
    (deal,) = list(db_session.scalars(select(Deal)))
    assert run(client, deal.id).status_code == 303
    marked = client.post(f"/queue/deals/{deal.id}/versions/2/working", follow_redirects=False)
    assert marked.status_code == 303
    assert marks(db_session, deal.id) == [(ARRIVAL, False, False), ("v2", True, True)]

    assert edit_intake(client, db_session, deal.id, rehab_costs="$50,000").status_code == 303
    assert marks(db_session, deal.id)[-1] == ("Auto v3", False, False)
    working = working_version(db_session, deal.id)
    assert working is not None and working.number == 2


def test_marking_the_working_version_records_the_choice(
    db_session: Session, deal_with_overrides: Deal
) -> None:
    """A Working version nobody chose becomes one a person chose: the next auto run is a draft."""
    deal = deal_with_overrides
    auto_run(db_session, deal)
    db_session.commit()
    assert marks(db_session, deal.id) == [("Auto v1", True, False)]

    mark_working(db_session, deal.id, 1, actor=ACTOR)
    db_session.commit()
    assert marks(db_session, deal.id) == [("Auto v1", True, True)]
    (row,) = audit(db_session, deal.id, AuditAction.VERSION_MARKED_WORKING)
    assert row.before == {"version": 1, "name": "Auto v1"} and row.after == row.before

    auto_run(db_session, deal)
    db_session.commit()
    assert marks(db_session, deal.id) == [("Auto v1", True, True), ("Auto v2", False, False)]


def test_the_version_facts_are_in_the_history_not_the_header(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """When, who and which engine are the History's; the header names none of them."""
    deal_id = deal_with_overrides.id
    run(client, deal_id, "Base")
    run(client, deal_id)
    for path in (f"/queue/deals/{deal_id}", f"/queue/deals/{deal_id}?version=2"):
        body = client.get(path).text
        head = body[: body.index("</header>", body.index('<header class="deal-head">'))]
        head = head[head.index('<header class="deal-head">') :]
        for gone in ("Chosen by a person", "<dt>Run</dt>", "<dt>Engine</dt>", ENGINE_VERSION):
            assert gone not in head, gone
        history = body[body.index('id="history"') :]
        for version in deal_versions(db_session, deal_id):
            row = history[history.index(f'data-version="{version.number}"') :]
            row = row[: row.index("</tr>")]
            assert version.actor in row and version.engine_version in row


# --- Download Excel (SPEC §9) ---------------------------------------------------------------------


def test_download_excel_exports_the_version_in_view(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_id = deal_with_overrides.id
    assert run(client, deal_id, "Base case").status_code == 303
    assert save_and_run(client, deal_id, interest_rate="15%").status_code == 303
    base, draft = (version_results(db_session, get_version(db_session, deal_id, n)) for n in (1, 2))
    assert base.underwrite is not None and draft.underwrite is not None
    assert base.underwrite.return_overview.irr != draft.underwrite.return_overview.irr

    working_page = client.get(f"/queue/deals/{deal_id}").text
    draft_page = client.get(f"/queue/deals/{deal_id}?version=2").text
    assert f'href="/queue/deals/{deal_id}/versions/1/workbook"' in working_page
    assert f'href="/queue/deals/{deal_id}/versions/2/workbook"' in draft_page
    switcher = working_page[working_page.index('class="actions switcher"') :]
    assert switcher.index(">Show</button>") < switcher.index(">Download Excel</a>")
    assert switcher.index(">Download Excel</a>") < switcher.index("</form>")

    for number, found, name in ((1, base, "Base-case"), (2, draft, "v2")):
        response = client.get(f"/queue/deals/{deal_id}/versions/{number}/workbook")
        assert response.status_code == 200
        assert response.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert response.headers["content-disposition"] == (
            f'attachment; filename="1412-S-Trenton-Ave-Tulsa-OK-74120-{name}.xlsx"'
        )
        book = load_workbook(BytesIO(response.content))
        assert found.underwrite is not None and found.underwrite.pricing is not None
        assert book["Pricing"].cell(row=5, column=9).value == (
            found.underwrite.pricing.rows[0].sentence
        )
        overview = book["Return Overview"]
        irr_cells = [
            row[1].value
            for row in overview.iter_rows(min_col=1, max_col=2)
            if row[0].value == "IRR (engine)"
        ]
        [irr] = irr_cells
        assert found.underwrite.return_overview.irr is not None
        assert abs(float(irr) - float(found.underwrite.return_overview.irr)) < 1e-12


def test_a_version_with_no_ledger_has_no_workbook(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    deal_with_overrides.term_months = None  # the ledger stands down; the screen still runs
    db_session.commit()
    deal_id = deal_with_overrides.id
    assert run(client, deal_id).status_code == 422
    version = get_version(db_session, deal_id, 1)
    assert version.underwrite_id is None
    page = client.get(f"/queue/deals/{deal_id}").text
    assert 'class="btn quiet off" id="download-excel"' in page
    response = client.get(f"/queue/deals/{deal_id}/versions/1/workbook", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/queue/deals/{deal_id}?version=1&")
    assert notice(response) == "v1 has no ledger to export: the underwrite did not run."
    missing = client.get(f"/queue/deals/{deal_id}/versions/9/workbook", follow_redirects=False)
    assert missing.status_code == 303 and notice(missing) == "That version does not exist."


def test_the_workbook_filename() -> None:
    deal = Deal(id=UUID("12345678-1234-5678-1234-567812345678"))
    assert workbook_filename(deal, "v1") == "deal-12345678-v1.xlsx"
    deal.property = Property(address_raw="88 Larimer St., Denver, CO 80202", state=State.CO)
    assert workbook_filename(deal, "Auto v3") == "88-Larimer-St-Denver-CO-80202-Auto-v3.xlsx"
    assert workbook_filename(deal, "Rate 15% / ARV!") == (
        "88-Larimer-St-Denver-CO-80202-Rate-15-ARV.xlsx"
    )
