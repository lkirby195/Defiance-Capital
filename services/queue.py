"""What the review-queue list shows.  # SPEC §9.1, §12

Deals grouped by status in lifecycle order, newest first inside each group, with one
exception jumping the whole thing: a deal that picked up a Hard flag *after* it reached
LOI_SENT or HANDED_OFF is pinned to the top.

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

from db.models import AuditLog, Deal, Screen, Underwrite
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

    Newest first throughout, by the deal's own creation time: the queue is a list of
    inquiries to work, and the oldest untouched inquiry is not the one a person should be
    looking at first. ``id`` breaks a tie so the order is stable across page loads.
    """
    deals = list(
        session.scalars(
            select(Deal)
            .options(selectinload(Deal.borrower), selectinload(Deal.property))
            .order_by(Deal.created_at.desc(), Deal.id.desc())
        )
    )
    deal_ids = [deal.id for deal in deals]
    screens = _latest_screens(session, deal_ids)
    underwrites = _latest_underwrites(session, deal_ids)

    pinned: list[QueueEntry] = []
    by_status: dict[Status, list[QueueEntry]] = {status: [] for status in Status}
    for deal in deals:
        screen = screens.get(deal.id)
        underwrite = underwrites.get(deal.id)
        is_pinned, reason = pin_state(session, deal, screen, underwrite)
        entry = QueueEntry(deal, screen, underwrite, is_pinned, reason)
        if is_pinned:
            pinned.append(entry)
        else:
            by_status[deal.status].append(entry)
    groups = [QueueGroup(status, by_status[status]) for status in Status if by_status[status]]
    return QueueView(pinned=pinned, groups=groups)
