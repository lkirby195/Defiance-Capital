"""A deal's History: its versions, and the intake edits no run has picked up.  # SPEC §5, §9

The History section lists **versions** (``services/versions.py``), newest first: one row per
analysis run, with its name, when it ran, who it ran for, whether it is the Working version
or a draft, the engine that ran it and its verdict and IRR. The screens and underwrites a
version wrote are not listed beside it - they are the version.

An intake submission that no version has run on yet - an edit on a paused deal, an
incomplete one, one whose analysis could not start - is listed as an **edit** row in its
place, so nothing that changed the deal goes unlisted. A submission is picked up by the
version whose snapshot names it (the submission in force when that version ran); every other
is an edit. Its actor is found through its audit row, which names the submission
(``after.submission_id``) - or, for a row written before that key existed, shares its
``created_at``, because both read ``now()`` inside the one transaction that wrote them. An
entry nobody can be found for says so with None rather than guessing.

An edit row is **restorable** when its stored payload is one the intake parsers read back -
a team-entry or public-form submission - which is every version written by this queue; the
restore itself is ``services.intake.restore_intake``. A version is restorable when it kept
its inputs, which every version but a backfilled one did (``services.versions.restore_version``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog, IntakeSubmission, Screen, Underwrite, Version
from schema.models import AuditAction, Channel, Verdict
from services.audit import DEALS
from services.versions import snapshot_complete, snapshot_submission

# The intake channels whose stored payload the parsers read back (``services/intake.py``).
RESTORABLE_CHANNELS: frozenset[Channel] = frozenset({Channel.TEAM, Channel.WEB})

# What an edit row's "what" column says, by the audit action written beside it.
INTAKE_WORDS: dict[str, str] = {
    AuditAction.INTAKE_CREATED.value: "created",
    AuditAction.INTAKE_EDITED.value: "edited",
    AuditAction.INTAKE_RESTORED.value: "restored",
}


class HistoryKind(StrEnum):
    """What a history entry is a record of."""

    VERSION = "VERSION"
    EDIT = "EDIT"


KIND_LABELS: dict[HistoryKind, str] = {
    HistoryKind.VERSION: "Version",
    HistoryKind.EDIT: "Edit",
}


@dataclass(frozen=True)
class HistoryEntry:
    """One row of the History section: a version, or an edit no version has run on."""

    kind: HistoryKind
    id: UUID  # the version's id, or the submission's
    at: datetime
    actor: str | None
    engine_version: str | None = None
    # An edit's: how the intake version arrived ("team edited").
    note: str = ""
    # A version's: its number, name, Working mark, verdict and IRR.
    number: int | None = None
    name: str = ""
    working: bool = False
    verdict: Verdict | None = None
    irr: Decimal | None = None
    restorable: bool = False

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass
class _Actors:
    """Who wrote which intake submission, read off the deal's audit rows once."""

    by_submission: dict[str, tuple[str, str]] = field(default_factory=dict)  # actor, action
    # (created_at, actor, action) for every intake row: the fallback for a row written
    # before ``submission_id`` was recorded on it.
    intake_rows: list[tuple[datetime, str, str]] = field(default_factory=list)

    def for_submission(self, submission: IntakeSubmission) -> tuple[str | None, str | None]:
        """The actor and the audit action behind one submission; None for either when unknown."""
        found = self.by_submission.get(str(submission.id))
        if found is not None:
            return found
        for created_at, actor, action in self.intake_rows:
            if created_at == submission.received_at:
                return actor, action
        return None, None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _actors(session: Session, deal_id: UUID) -> _Actors:
    found = _Actors()
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.table_name == DEALS,
            AuditLog.row_id == str(deal_id),
            AuditLog.action.in_(list(INTAKE_WORDS)),
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    for row in rows:
        after = row.after if isinstance(row.after, dict) else {}
        found.intake_rows.append((row.created_at, row.actor, row.action))
        if key := _text(after.get("submission_id")):
            found.by_submission[key] = (row.actor, row.action)
    return found


def restorable(submission: IntakeSubmission) -> bool:
    """Whether a submission's stored payload is one the intake parsers read back."""
    return submission.channel in RESTORABLE_CHANNELS and isinstance(submission.raw_payload, dict)


def deal_history(session: Session, deal_id: UUID) -> list[HistoryEntry]:
    """Every version of one deal and every edit no version ran on, newest first.  # SPEC §9"""
    entries: list[HistoryEntry] = []
    picked_up: set[str] = set()
    rows = session.execute(
        select(Version, Screen.verdict, Underwrite.irr)
        .outerjoin(Screen, Screen.id == Version.screen_id)
        .outerjoin(Underwrite, Underwrite.id == Version.underwrite_id)
        .where(Version.deal_id == deal_id)
    )
    for version, verdict, irr in rows:
        if submission_id := snapshot_submission(version.inputs_snapshot):
            picked_up.add(submission_id)
        entries.append(
            HistoryEntry(
                kind=HistoryKind.VERSION,
                id=version.id,
                at=version.created_at,
                actor=version.actor,
                engine_version=version.engine_version,
                number=version.number,
                name=version.name,
                working=version.is_working,
                verdict=verdict,
                irr=irr,
                restorable=snapshot_complete(version.inputs_snapshot),
            )
        )
    actors = _actors(session, deal_id)
    for submission in session.scalars(
        select(IntakeSubmission).where(IntakeSubmission.deal_id == deal_id)
    ):
        if str(submission.id) in picked_up:
            continue
        actor, action = actors.for_submission(submission)
        word = INTAKE_WORDS.get(action or "", "")
        entries.append(
            HistoryEntry(
                kind=HistoryKind.EDIT,
                id=submission.id,
                at=submission.received_at,
                actor=actor,
                note=" ".join(part for part in (submission.channel.value.lower(), word) if part),
                restorable=restorable(submission),
            )
        )
    # Newest first. An edit and the version that ran on the edit before it can share a
    # transaction, so the kind breaks a tie the way it happened: the edit, then the run.
    order = {HistoryKind.EDIT: 0, HistoryKind.VERSION: 1}
    entries.sort(key=lambda entry: (entry.at, order[entry.kind]), reverse=True)
    return entries
