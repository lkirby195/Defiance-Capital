"""Everything that has ever been recorded about one deal, as one list.  # SPEC §5, §9.1

Three append-only tables hold a deal's versions and its runs - ``intake_submissions``,
``screens`` and ``underwrites`` - and none of them carries an actor, because the actor is
on the ``audit_log`` row written beside each in the same transaction (CLAUDE.md, "Every
service write takes an actor"). This module reads the four together and hands the page one
list, newest first, with each entry's kind, time, actor and engine version, so the History
section is one table rather than three.

The actor is found through the audit row. A run's row names the run it belongs to
(``after.screen_id`` / ``after.underwrite_id``) and an intake row names its submission
(``after.submission_id``) - and, for an intake row written before that key existed, the two
rows share a ``created_at``, because both read ``now()`` inside the one transaction that
wrote them. An entry nobody can be found for says so with None rather than guessing.

An intake version is **restorable** when its stored payload is one the intake parsers can
read back - a team-entry or public-form submission - which is every version written by this
queue. The restore itself is ``services.intake.restore_intake``; this module only says which
rows offer the button.
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

from db.models import AuditLog, IntakeSubmission, Screen, Underwrite
from schema.models import AuditAction, Channel, Verdict
from services.audit import DEALS

# The intake channels whose stored payload the parsers read back (``services/intake.py``).
RESTORABLE_CHANNELS: frozenset[Channel] = frozenset({Channel.TEAM, Channel.WEB})

# What an intake entry's "what" column says, by the audit action written beside it.
INTAKE_WORDS: dict[str, str] = {
    AuditAction.INTAKE_CREATED.value: "created",
    AuditAction.INTAKE_EDITED.value: "edited",
    AuditAction.INTAKE_RESTORED.value: "restored",
}


class HistoryKind(StrEnum):
    """What a history entry is a record of, in the order one run produces them."""

    INTAKE = "INTAKE"
    SCREEN = "SCREEN"
    UNDERWRITE = "UNDERWRITE"


KIND_LABELS: dict[HistoryKind, str] = {
    HistoryKind.INTAKE: "Intake",
    HistoryKind.SCREEN: "Screen",
    HistoryKind.UNDERWRITE: "Underwrite",
}


@dataclass(frozen=True)
class HistoryEntry:
    """One row of the History section."""

    kind: HistoryKind
    id: UUID
    at: datetime
    actor: str | None
    engine_version: str | None
    # One of these per kind: how the intake version arrived, the screen's verdict, the
    # underwrite's IRR. The template formats each with its own filter.
    note: str = ""
    verdict: Verdict | None = None
    irr: Decimal | None = None
    restorable: bool = False

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass
class _Actors:
    """Who wrote what, read off the deal's audit rows once."""

    by_screen: dict[str, str] = field(default_factory=dict)
    by_underwrite: dict[str, str] = field(default_factory=dict)
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
        .where(AuditLog.table_name == DEALS, AuditLog.row_id == str(deal_id))
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    for row in rows:
        after = row.after if isinstance(row.after, dict) else {}
        if row.action == AuditAction.SCREEN_RUN.value:
            if key := _text(after.get("screen_id")):
                found.by_screen[key] = row.actor
        elif row.action == AuditAction.UNDERWRITE_RUN.value:
            if key := _text(after.get("underwrite_id")):
                found.by_underwrite[key] = row.actor
        elif row.action in INTAKE_WORDS:
            found.intake_rows.append((row.created_at, row.actor, row.action))
            if key := _text(after.get("submission_id")):
                found.by_submission[key] = (row.actor, row.action)
    return found


def restorable(submission: IntakeSubmission) -> bool:
    """Whether a submission's stored payload is one the intake parsers read back."""
    return submission.channel in RESTORABLE_CHANNELS and isinstance(submission.raw_payload, dict)


def deal_history(session: Session, deal_id: UUID) -> list[HistoryEntry]:
    """Every intake version, screen and underwrite on one deal, newest first.  # SPEC §9.1"""
    actors = _actors(session, deal_id)
    entries: list[HistoryEntry] = []
    for submission in session.scalars(
        select(IntakeSubmission).where(IntakeSubmission.deal_id == deal_id)
    ):
        actor, action = actors.for_submission(submission)
        word = INTAKE_WORDS.get(action or "", "")
        entries.append(
            HistoryEntry(
                kind=HistoryKind.INTAKE,
                id=submission.id,
                at=submission.received_at,
                actor=actor,
                engine_version=None,
                note=" ".join(part for part in (submission.channel.value.lower(), word) if part),
                restorable=restorable(submission),
            )
        )
    for screen in session.scalars(select(Screen).where(Screen.deal_id == deal_id)):
        entries.append(
            HistoryEntry(
                kind=HistoryKind.SCREEN,
                id=screen.id,
                at=screen.created_at,
                actor=actors.by_screen.get(str(screen.id)),
                engine_version=screen.engine_version,
                verdict=screen.verdict,
            )
        )
    for underwrite in session.scalars(select(Underwrite).where(Underwrite.deal_id == deal_id)):
        entries.append(
            HistoryEntry(
                kind=HistoryKind.UNDERWRITE,
                id=underwrite.id,
                at=underwrite.created_at,
                actor=actors.by_underwrite.get(str(underwrite.id)),
                engine_version=underwrite.engine_version,
                irr=underwrite.irr,
            )
        )
    # Newest first. A screen and an underwrite from one run can share a timestamp, so the
    # kind breaks the tie the way the run happened: the intake, then the screen, then the
    # ledger - reversed with the rest.
    order = {kind: index for index, kind in enumerate(HistoryKind)}
    entries.sort(key=lambda entry: (entry.at, order[entry.kind]), reverse=True)
    return entries
