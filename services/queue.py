"""What the review-queue list shows.  # SPEC §9.1, §12

Deals grouped by status in lifecycle order, most recently touched first inside each group,
with one exception jumping the whole thing: a deal that picked up a Hard flag *after* it
reached LOI_SENT or HANDED_OFF is pinned to the top.

"Most recently touched" is the latest of everything that has happened to the deal, not the
day it arrived: the intake that created it and any submission since, its latest screen, its
latest underwrite, and its latest ``audit_log`` row - which is every team action and every
note. A queue ordered by arrival buries the deal somebody answered an hour ago under the one
nobody has opened in a fortnight, which is backwards: the deal being worked is the deal worth
seeing.

That exception is the reason this module is not a single ``ORDER BY``. Those two statuses
are past the point where SPEC §4.6 lets a Decline close a deal - the deal is being worked,
a person owns it, and the flags are recorded for them to read rather than acted on - so
nothing about the deal's own status says a Hard flag has landed on it since. The queue is
where that gets noticed, or it does not get noticed.

"After" is measured against the ``audit_log`` row that recorded the move into that status
(SPEC §12). Those rows are written by the Phase 5 and 6 actions, which do not exist yet; a
deal sitting in one of those statuses with no such row got there some other way, and any
Hard flag on its latest run pins it, because there is no "before" to compare against and a
missed Hard flag on a deal at LOI is the worse mistake.

A pinned deal appears once, at the top, and not again in its status group: the groups are
for scanning, and the same deal in two places makes both harder to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite
from schema.models import PINNED_AFTER_ACTIONS, Flag, Severity, Status
from services.audit import DEALS, last_action_at
from services.persistence import screen_result, underwrite_result

# The statuses the pin rule watches. Past these a Decline no longer closes a deal (SPEC
# §4.6), so a Hard flag raised here has nothing else to surface it.
PINNED_FROM: frozenset[Status] = frozenset({Status.LOI_SENT, Status.HANDED_OFF})


@dataclass(frozen=True)
class RunSummary:
    """The latest screen or underwrite on a deal, reduced to what the list shows."""

    id: UUID
    created_at: datetime
    hard_flags: list[Flag]


@dataclass(frozen=True)
class QueueEntry:
    """One deal on the list, with why it is where it is."""

    deal: Deal
    screen: RunSummary | None
    underwrite: RunSummary | None
    pinned: bool
    pin_reason: str | None
    last_activity: datetime

    @property
    def hard_flags(self) -> list[Flag]:
        """Hard flags on the latest run of each stage, screen first."""
        return [flag for run in (self.screen, self.underwrite) if run for flag in run.hard_flags]


@dataclass(frozen=True)
class QueueGroup:
    """One status heading and the deals under it."""

    status: Status
    entries: list[QueueEntry]


@dataclass(frozen=True)
class QueueView:
    """The whole list: the pinned deals, then every non-empty status group."""

    pinned: list[QueueEntry]
    groups: list[QueueGroup]

    @property
    def total(self) -> int:
        return len(self.pinned) + sum(len(group.entries) for group in self.groups)


def _hard(flags: list[Flag]) -> list[Flag]:
    return [flag for flag in flags if flag.severity is Severity.HARD]


def _latest_screens(session: Session, deal_ids: list[UUID]) -> dict[UUID, RunSummary]:
    """One ``RunSummary`` per deal that has ever been screened."""
    if not deal_ids:
        return {}
    rows = session.scalars(
        select(Screen)
        .where(Screen.deal_id.in_(deal_ids))
        .distinct(Screen.deal_id)
        .order_by(Screen.deal_id, Screen.created_at.desc(), Screen.id.desc())
    )
    return {
        row.deal_id: RunSummary(row.id, row.created_at, _hard(screen_result(row).flags))
        for row in rows
    }


def _latest_underwrites(session: Session, deal_ids: list[UUID]) -> dict[UUID, RunSummary]:
    """One ``RunSummary`` per deal that has ever been underwritten."""
    if not deal_ids:
        return {}
    rows = session.scalars(
        select(Underwrite)
        .where(Underwrite.deal_id.in_(deal_ids))
        .distinct(Underwrite.deal_id)
        .order_by(Underwrite.deal_id, Underwrite.created_at.desc(), Underwrite.id.desc())
    )
    return {
        row.deal_id: RunSummary(row.id, row.created_at, _hard(underwrite_result(row).flags))
        for row in rows
    }


def _latest_submissions(session: Session, deal_ids: list[UUID]) -> dict[UUID, datetime]:
    """When each deal last had something come in on it.  # SPEC §4.2

    Today that is the team entry that created it. When SMS ingestion lands a reply from the
    borrower will be another row here, and a deal the borrower has just answered is exactly
    the one that should move up the queue - so this reads the table rather than the deal.
    """
    if not deal_ids:
        return {}
    rows = session.execute(
        select(IntakeSubmission.deal_id, IntakeSubmission.received_at)
        .where(IntakeSubmission.deal_id.in_(deal_ids))
        .distinct(IntakeSubmission.deal_id)
        .order_by(IntakeSubmission.deal_id, IntakeSubmission.received_at.desc())
    )
    return {deal_id: received_at for deal_id, received_at in rows if deal_id is not None}


def _latest_audit(session: Session, deal_ids: list[UUID]) -> dict[UUID, datetime]:
    """When each deal was last acted on: any team action, any note, any run.  # SPEC §5"""
    if not deal_ids:
        return {}
    ids = {str(deal_id) for deal_id in deal_ids}
    rows = session.execute(
        select(AuditLog.row_id, AuditLog.created_at)
        .where(AuditLog.table_name == DEALS, AuditLog.row_id.in_(ids))
        .distinct(AuditLog.row_id)
        .order_by(AuditLog.row_id, AuditLog.created_at.desc())
    )
    return {UUID(row_id): created_at for row_id, created_at in rows}


def last_activity(
    deal: Deal,
    screen: RunSummary | None,
    underwrite: RunSummary | None,
    submitted_at: datetime | None,
    acted_at: datetime | None,
) -> datetime:
    """The latest of everything that has happened to the deal.

    ``deal.created_at`` is the floor rather than one candidate among equals: every deal has
    one, and a deal with no runs and no audit rows - one stored before Phase 4, or by a
    script - still has to sort somewhere rather than raising.
    """
    moments = [deal.created_at, submitted_at, acted_at]
    moments += [run.created_at for run in (screen, underwrite) if run is not None]
    return max(moment for moment in moments if moment is not None)


def _entered_status_at(session: Session, deal: Deal) -> AuditLog | None:
    """The audit row recording the deal reaching LOI_SENT or HANDED_OFF, if there is one."""
    return last_action_at(session, DEALS, deal.id, *PINNED_AFTER_ACTIONS)


def pin_state(
    session: Session,
    deal: Deal,
    screen: RunSummary | None,
    underwrite: RunSummary | None,
) -> tuple[bool, str | None]:
    """Whether the deal is pinned, and the sentence the banner shows.  # SPEC §12"""
    if deal.status not in PINNED_FROM:
        return False, None
    runs = [(name, run) for name, run in (("screen", screen), ("underwrite", underwrite)) if run]
    flagged = [(name, run) for name, run in runs if run.hard_flags]
    if not flagged:
        return False, None
    marker = _entered_status_at(session, deal)
    if marker is not None:
        flagged = [(name, run) for name, run in flagged if run.created_at >= marker.created_at]
        if not flagged:
            return False, None
    count = sum(len(run.hard_flags) for _, run in flagged)
    where = " and ".join(name for name, _ in flagged)
    since = (
        f"since it reached {deal.status.value} on {marker.created_at:%Y-%m-%d}"
        if marker is not None
        else f"and the deal is {deal.status.value}"
    )
    return True, (
        f"{count} Hard flag(s) on the latest {where} {since}. "
        "A re-screen no longer closes a deal this far along (SPEC §4.6), so this is here to "
        "be read rather than acted on automatically."
    )


def queue_view(session: Session) -> QueueView:
    """Every deal, pinned first and then grouped by status.  # SPEC §9.1

    Most recently touched first throughout (``last_activity``), so the deal somebody is in
    the middle of is the deal at the top of its group. ``id`` breaks a tie, so the order is
    stable across page loads and two deals touched in the same transaction do not swap.

    The sort is done here rather than in the ``ORDER BY`` because the value being sorted on
    comes from four tables and the deal's own column; one query per source and a max in
    Python is both clearer and, on a queue this size, not slower.
    """
    deals = list(
        session.scalars(
            select(Deal).options(selectinload(Deal.borrower), selectinload(Deal.property))
        )
    )
    deal_ids = [deal.id for deal in deals]
    screens = _latest_screens(session, deal_ids)
    underwrites = _latest_underwrites(session, deal_ids)
    submissions = _latest_submissions(session, deal_ids)
    actions = _latest_audit(session, deal_ids)

    entries: list[QueueEntry] = []
    for deal in deals:
        screen = screens.get(deal.id)
        underwrite = underwrites.get(deal.id)
        is_pinned, reason = pin_state(session, deal, screen, underwrite)
        entries.append(
            QueueEntry(
                deal=deal,
                screen=screen,
                underwrite=underwrite,
                pinned=is_pinned,
                pin_reason=reason,
                last_activity=last_activity(
                    deal, screen, underwrite, submissions.get(deal.id), actions.get(deal.id)
                ),
            )
        )
    entries.sort(key=lambda entry: (entry.last_activity, entry.deal.id), reverse=True)

    pinned = [entry for entry in entries if entry.pinned]
    by_status: dict[Status, list[QueueEntry]] = {status: [] for status in Status}
    for entry in entries:
        if not entry.pinned:
            by_status[entry.deal.status].append(entry)
    groups = [QueueGroup(status, by_status[status]) for status in Status if by_status[status]]
    return QueueView(pinned=pinned, groups=groups)
