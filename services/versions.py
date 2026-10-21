"""Versions: one analysis run each, its inputs whole, and which one is Working.  # SPEC §5, §9

A **version** is one analysis run - the screen and the underwrite that Run Analysis, Save &
Run, Reset all, a restore and every automatic run produce together (``services/autorun.py``)
- with a name, the person or ``system`` it ran for, and a **snapshot** of everything it read
off the deal:

    intake    the intake submission in force when it ran: its id, channel and payload
    deal      every ``deals`` column but the row's identity and lifecycle (NOT_SNAPSHOTTED):
              the intake's columns, the §8.1 economics, the loan split, the valuation, the
              rent, the five analysis assumptions, the toggles, the court search, the
              ``defaulted_fields`` tags, and the borrower and property the deal pointed at
    borrower  the borrower row's id, name, phone, email and entities, as they were
    property  the property row as it was, the state the screen reads included

The snapshot is what makes a version reproducible whatever is edited afterwards: **Restore
inputs** (``restore_version``) applies the kept intake payload as a new submission, exactly
as an intake restore does (``services/intake.reapply_intake``), then puts every snapshotted
column back and re-points the deal at the borrower and property it had, and the route runs a
new version on it. The new version's result is the old one's to the last digit, because the
engine is pure and every input it reads came back. The column list is derived from the table
rather than written out, so a column added later is snapshotted without anyone remembering
to; a snapshot from before it existed leaves that column as it is on restore.

**Exactly one version per deal is Working**; the rest are drafts. The first run on a deal is
Working, every later one a draft until a person moves the mark (``mark_working``), and the
database refuses a second Working row (``versions.uq_versions_one_working_per_deal``). The
deal page and Home show the Working version unless a person asks for another.

Every write here takes an actor and writes an ``audit_log`` row - ``VERSION_RECORDED``,
``VERSION_MARKED_WORKING``, ``VERSION_RESTORED`` - and nothing commits.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Borrower, Deal, Property, Screen, Underwrite, Version
from schema.models import (
    AuditAction,
    Channel,
    ScreenInputs,
    ScreenResult,
    UnderwriteInputs,
    UnderwriteResult,
)
from services.audit import DEALS, jsonable, record_audit
from services.errors import ActionNotAllowed, VersionNotFound
from services.intake import (
    EDIT_INTAKE_FROM,
    latest_submission,
    reapply_intake,
    record_from_payload,
)
from services.persistence import screen_result, underwrite_result
from services.runner import load_deal

SNAPSHOT_SCHEMA = 1
# The longest name a person may give a version; the column is String(100).
NAME_MAX = 100

# The ``deals`` columns a snapshot leaves out: the row's identity, the channel it came in on
# and its public-form provenance (none of which an edit changes), its place on the lifecycle
# (which a restore must not rewind), and its timestamps. Everything else is an input.
NOT_SNAPSHOTTED: frozenset[str] = frozenset(
    {"id", "channel", "status", "intake_source", "referral_note", "created_at", "updated_at"}
)
SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(
    column.name for column in Deal.__table__.columns if column.name not in NOT_SNAPSHOTTED
)
# The two that are links rather than values: restored by pointing the deal at the row.
LINK_COLUMNS: frozenset[str] = frozenset({"borrower_id", "property_id"})
PROPERTY_COLUMNS: tuple[str, ...] = tuple(column.name for column in Property.__table__.columns)


# --- the snapshot ---------------------------------------------------------------------------------


def _borrower(borrower: Borrower | None) -> dict[str, Any] | None:
    if borrower is None:
        return None
    return {
        "id": str(borrower.id),
        "name": borrower.name,
        "phone": borrower.phone,
        "email": borrower.email,
        "entities": sorted(entity.name for entity in borrower.entities),
    }


def _property(prop: Property | None) -> dict[str, Any] | None:
    if prop is None:
        return None
    return {column: jsonable(getattr(prop, column)) for column in PROPERTY_COLUMNS}


def take_snapshot(session: Session, deal: Deal) -> dict[str, Any]:
    """Everything a run on ``deal`` reads, as JSON.  # SPEC §5"""
    submission = latest_submission(session, deal.id)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "intake": (
            None
            if submission is None
            else {
                "submission_id": str(submission.id),
                "channel": submission.channel.value,
                "payload": submission.raw_payload,
            }
        ),
        "deal": {column: jsonable(getattr(deal, column)) for column in SNAPSHOT_COLUMNS},
        "borrower": _borrower(deal.borrower),
        "property": _property(deal.property),
    }


def snapshot_complete(snapshot: dict[str, Any]) -> bool:
    """True when a snapshot carries the inputs; False on a version the migration backfilled."""
    return snapshot.get("schema") == SNAPSHOT_SCHEMA and isinstance(snapshot.get("deal"), dict)


def snapshot_submission(snapshot: dict[str, Any]) -> str | None:
    """The id of the intake submission that was in force when the version ran."""
    intake = snapshot.get("intake")
    if isinstance(intake, dict) and isinstance(intake.get("submission_id"), str):
        return str(intake["submission_id"])
    return None


def decode(column: str, value: Any) -> Any:
    """A snapshotted value back in the type its ``deals`` column holds."""
    if value is None:
        return None
    kind = Deal.__table__.c[column].type
    if isinstance(kind, sa.Enum) and kind.enum_class is not None:
        return kind.enum_class(value)
    if isinstance(kind, sa.Numeric):
        return Decimal(value)
    if isinstance(kind, sa.Date):
        return date.fromisoformat(value)
    if isinstance(kind, sa.Uuid):
        return UUID(value)
    return value


# --- reading them ---------------------------------------------------------------------------------


def deal_versions(session: Session, deal_id: UUID) -> list[Version]:
    """Every version of a deal, newest first."""
    return list(
        session.scalars(
            select(Version).where(Version.deal_id == deal_id).order_by(Version.number.desc())
        )
    )


def working_version(session: Session, deal_id: UUID) -> Version | None:
    """The deal's Working version; None until it has been analyzed."""
    return session.scalar(select(Version).where(Version.deal_id == deal_id, Version.is_working))


def get_version(session: Session, deal_id: UUID, number: int) -> Version:
    """Version ``number`` of a deal, or ``VersionNotFound``."""
    version = session.scalar(
        select(Version).where(Version.deal_id == deal_id, Version.number == number)
    )
    if version is None:
        raise VersionNotFound(deal_id, str(number))
    return version


def find_version(session: Session, deal_id: UUID, wanted: str | None) -> Version:
    """The version a person named: its number, else its exact name, else the Working one.

    For the CLI (``glenwood deals run`` / ``export``). A name two versions share is refused
    with both numbers rather than guessed between.
    """
    if wanted is None:
        working = working_version(session, deal_id)
        if working is None:
            raise VersionNotFound(deal_id, "Working")
        return working
    if wanted.isdigit():
        return get_version(session, deal_id, int(wanted))
    named = [version for version in deal_versions(session, deal_id) if version.name == wanted]
    if len(named) == 1:
        return named[0]
    if named:
        numbers = ", ".join(str(version.number) for version in reversed(named))
        raise ValueError(f"{len(named)} versions are named {wanted!r} (numbers {numbers})")
    raise VersionNotFound(deal_id, wanted)


def version_rows(session: Session, version: Version) -> tuple[Screen | None, Underwrite | None]:
    """The screen and the underwrite a version wrote; either None when it has none."""
    screen = session.get(Screen, version.screen_id) if version.screen_id else None
    underwrite = session.get(Underwrite, version.underwrite_id) if version.underwrite_id else None
    return screen, underwrite


class VersionResults(NamedTuple):
    """A version's stored runs rebuilt: what each stage read and what it said."""

    version: Version
    screen_inputs: ScreenInputs | None
    screen: ScreenResult | None
    underwrite_inputs: UnderwriteInputs | None
    underwrite: UnderwriteResult | None


def version_results(session: Session, version: Version) -> VersionResults:
    """The inputs and results a version's two rows hold, rebuilt exactly as they were stored.

    For the CLI's report and workbook (``glenwood deals run`` / ``export``): nothing is run
    again, so what they print is what the version recorded, engine version and all.
    """
    screen_row, underwrite_row = version_rows(session, version)
    return VersionResults(
        version=version,
        screen_inputs=(
            ScreenInputs.model_validate(screen_row.inputs) if screen_row is not None else None
        ),
        screen=screen_result(screen_row) if screen_row is not None else None,
        underwrite_inputs=(
            UnderwriteInputs.model_validate(underwrite_row.inputs)
            if underwrite_row is not None
            else None
        ),
        underwrite=underwrite_result(underwrite_row) if underwrite_row is not None else None,
    )


# --- writing them ---------------------------------------------------------------------------------


def clean_name(raw: str | None) -> str | None:
    """A typed version name, trimmed; None when blank. ``ValueError`` when it is too long."""
    name = " ".join((raw or "").split())
    if len(name) > NAME_MAX:
        raise ValueError(f"A version name is at most {NAME_MAX} characters.")
    return name or None


def default_name(number: int, auto: bool) -> str:
    """``v3`` for a run a person asked for and left unnamed; ``Auto v3`` for an automatic one."""
    return f"Auto v{number}" if auto else f"v{number}"


def record_version(
    session: Session,
    deal: Deal,
    *,
    actor: str,
    snapshot: dict[str, Any],
    screen: Screen,
    underwrite: Underwrite | None,
    name: str | None = None,
    auto: bool = False,
) -> Version:
    """Append the version one analysis wrote; Working when the deal has none yet.  # SPEC §5

    The deal row is locked for the numbering, so two runs on one deal at once take numbers
    one after the other rather than the same one. Flushes; no commit.
    """
    session.execute(select(Deal.id).where(Deal.id == deal.id).with_for_update())
    number = (
        session.scalar(select(func.max(Version.number)).where(Version.deal_id == deal.id)) or 0
    ) + 1
    working = working_version(session, deal.id) is None
    version = Version(
        deal_id=deal.id,
        number=number,
        name=clean_name(name) or default_name(number, auto),
        actor=actor,
        engine_version=screen.engine_version,
        config_hash=screen.config_hash,
        inputs_snapshot=snapshot,
        screen_id=screen.id,
        underwrite_id=underwrite.id if underwrite is not None else None,
        is_working=working,
    )
    session.add(version)
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.VERSION_RECORDED,
        table_name=DEALS,
        row_id=deal.id,
        after={
            "version": number,
            "name": version.name,
            "working": working,
            "screen_id": str(screen.id),
            "underwrite_id": str(underwrite.id) if underwrite is not None else None,
        },
    )
    return version


def mark_working(session: Session, deal_id: UUID, number: int, *, actor: str) -> Version:
    """Make version ``number`` the deal's Working one; the one before it becomes a draft.

    Allowed from any status: it says which analysis the deal stands on, and changes no input.
    The old mark comes off and is flushed before the new one goes on, so the one-Working
    index never sees two. A version already Working is left alone with no audit row.
    """
    deal = load_deal(session, deal_id)
    version = get_version(session, deal.id, number)
    if version.is_working:
        return version
    current = working_version(session, deal.id)
    if current is not None:
        current.is_working = False
        session.flush()
    version.is_working = True
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.VERSION_MARKED_WORKING,
        table_name=DEALS,
        row_id=deal.id,
        before=({"version": current.number, "name": current.name} if current is not None else None),
        after={"version": version.number, "name": version.name},
    )
    return version


def apply_snapshot(
    session: Session, deal: Deal, snapshot: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Put every snapshotted column back on the deal; the before and after of what moved.

    The two links point the deal back at the borrower and the property it had, when those
    rows still exist. A column the snapshot does not name - one added after it was taken -
    keeps its value.
    """
    values = snapshot["deal"]
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for column in SNAPSHOT_COLUMNS:
        if column not in values:
            continue
        value = decode(column, values[column])
        current = getattr(deal, column)
        if current == value:
            continue
        if column == "borrower_id":
            borrower = session.get(Borrower, value) if value is not None else None
            if value is not None and borrower is None:
                continue
            deal.borrower = borrower
        elif column == "property_id":
            prop = session.get(Property, value) if value is not None else None
            if value is not None and prop is None:
                continue
            deal.property = prop
        else:
            setattr(deal, column, value)
        before[column] = jsonable(current)
        after[column] = values[column]
    return before, after


def restore_version(
    session: Session,
    deal_id: UUID,
    number: int,
    *,
    actor: str,
    config: Config | None = None,
) -> Deal:
    """Restore inputs: load version ``number``'s snapshot back into the deal.  # SPEC §9

    The intake payload it kept is applied as a new submission (``INTAKE_RESTORED``, naming
    the version), then every snapshotted column is put back (``VERSION_RESTORED``, naming the
    version and the columns that moved). Nothing is rewound - the versions since stay - and
    the route runs a new version on what was restored. Refused where an edit is refused, and
    on a version the migration backfilled, which kept no inputs to restore.
    """
    deal = load_deal(session, deal_id)
    if deal.status not in EDIT_INTAKE_FROM:
        raise ActionNotAllowed(deal.id, deal.status, "restored", set(EDIT_INTAKE_FROM))
    version = get_version(session, deal.id, number)
    snapshot = version.inputs_snapshot
    if not snapshot_complete(snapshot):
        raise ValueError(
            f"{version.name} was recorded before versions kept their inputs, so there is "
            "nothing to restore from it"
        )
    note = {"restored_from_version": version.number, "version_name": version.name}
    intake = snapshot.get("intake")
    if isinstance(intake, dict):
        record = record_from_payload(Channel(intake["channel"]), intake["payload"], deal)
        reapply_intake(session, deal, record, actor=actor, note=note, config=config)
    before, after = apply_snapshot(session, deal, snapshot)
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.VERSION_RESTORED,
        table_name=DEALS,
        row_id=deal.id,
        before=before,
        after={**note, **after},
    )
    return deal
