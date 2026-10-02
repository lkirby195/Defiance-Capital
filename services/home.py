"""What the Home page shows.  # SPEC §9.1, §12

Four sections, in this order, and every deal is in exactly one of them:

    Borrower Submissions   deals the borrower typed in on the public form (channel WEB)
    DCF New Deals          deals the team entered (every other channel)
    On Pause               status PAUSED, whichever channel it came in on
    Dead                   status DEAD or DECLINED, whichever channel it came in on

The first two are the live work and leave out the paused and the dead; the last two are
where those went. Within each section the most recently touched deal is first.

"Most recently touched" is the latest of everything that has happened to the deal, not the
day it arrived: the intake that created it and any submission since, its latest screen, its
latest underwrite, and its latest ``audit_log`` row - which is every team action and every
note. A list ordered by arrival buries the deal somebody answered an hour ago under the one
nobody has opened in a fortnight, which is backwards: the deal being worked is the deal worth
seeing.

Each row carries what a person scans a list for - who, where, how much, which product, the
latest verdict and IRR, where it came from, when it last moved - and the three controls
(``services/actions.py``): Progress, Pause and Kill, each live only from the statuses it
applies from, so a button that would be refused is off rather than a surprise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite
from schema.models import Channel, Status, Verdict
from services.actions import MARK_DEAD_FROM, PAUSE_FROM, PROGRESS_FROM
from services.audit import DEALS


class Section(StrEnum):
    """The four headings, in the order the page shows them."""

    BORROWER = "BORROWER"
    DCF = "DCF"
    PAUSED = "PAUSED"
    DEAD = "DEAD"


SECTION_TITLES: dict[Section, str] = {
    Section.BORROWER: "Borrower Submissions",
    Section.DCF: "DCF New Deals",
    Section.PAUSED: "On Pause",
    Section.DEAD: "Dead",
}

# The statuses that put a deal in the Dead section, whichever channel it came in on.
DEAD_STATUSES: frozenset[Status] = frozenset({Status.DEAD, Status.DECLINED})


def section_of(deal: Deal) -> Section:
    """Which section a deal belongs in: its status first, then where it came from."""
    if deal.status is Status.PAUSED:
        return Section.PAUSED
    if deal.status in DEAD_STATUSES:
        return Section.DEAD
    if deal.channel is Channel.WEB:
        return Section.BORROWER
    return Section.DCF


@dataclass(frozen=True)
class RunSummary:
    """The latest screen or underwrite on a deal, reduced to what the list shows."""

    id: UUID
    created_at: datetime
    verdict: Verdict | None = None  # the screen's
    irr: Decimal | None = None  # the underwrite's


@dataclass(frozen=True)
class HomeEntry:
    """One deal on the list, with what the row shows and which controls are live."""

    deal: Deal
    screen: RunSummary | None
    underwrite: RunSummary | None
    last_activity: datetime

    @property
    def verdict(self) -> Verdict | None:
        return self.screen.verdict if self.screen is not None else None

    @property
    def irr(self) -> Decimal | None:
        return self.underwrite.irr if self.underwrite is not None else None

    @property
    def can_progress(self) -> bool:
        return self.deal.status in PROGRESS_FROM

    @property
    def can_pause(self) -> bool:
        return self.deal.status in PAUSE_FROM

    @property
    def can_kill(self) -> bool:
        return self.deal.status in MARK_DEAD_FROM


@dataclass(frozen=True)
class HomeSection:
    """One heading and the deals under it."""

    key: Section
    entries: list[HomeEntry]

    @property
    def title(self) -> str:
        return SECTION_TITLES[self.key]


@dataclass(frozen=True)
class HomeView:
    """The whole page: the four sections, every one of them present even when empty."""

    sections: list[HomeSection]

    @property
    def total(self) -> int:
        return sum(len(section.entries) for section in self.sections)

    def section(self, key: Section) -> HomeSection:
        return next(section for section in self.sections if section.key is key)


def _latest_screens(session: Session, deal_ids: list[UUID]) -> dict[UUID, RunSummary]:
    """One ``RunSummary`` per deal that has ever been screened: when, and the verdict."""
    if not deal_ids:
        return {}
    rows = session.execute(
        select(Screen.deal_id, Screen.id, Screen.created_at, Screen.verdict)
        .where(Screen.deal_id.in_(deal_ids))
        .distinct(Screen.deal_id)
        .order_by(Screen.deal_id, Screen.created_at.desc(), Screen.id.desc())
    )
    return {
        deal_id: RunSummary(row_id, created_at, verdict=verdict)
        for deal_id, row_id, created_at, verdict in rows
    }


def _latest_underwrites(session: Session, deal_ids: list[UUID]) -> dict[UUID, RunSummary]:
    """One ``RunSummary`` per deal that has ever been underwritten: when, and the IRR.

    The IRR is the ``underwrites.irr`` column, the NUMERIC(7,5) copy kept for exactly this
    kind of read (``services/persistence.py``); the list does not rebuild a ledger per row.
    """
    if not deal_ids:
        return {}
    rows = session.execute(
        select(Underwrite.deal_id, Underwrite.id, Underwrite.created_at, Underwrite.irr)
        .where(Underwrite.deal_id.in_(deal_ids))
        .distinct(Underwrite.deal_id)
        .order_by(Underwrite.deal_id, Underwrite.created_at.desc(), Underwrite.id.desc())
    )
    return {
        deal_id: RunSummary(row_id, created_at, irr=irr)
        for deal_id, row_id, created_at, irr in rows
    }


def _latest_submissions(session: Session, deal_ids: list[UUID]) -> dict[UUID, datetime]:
    """When each deal last had something come in on it.  # SPEC §4.2

    Today that is the entry that created it. When SMS ingestion lands a reply from the
    borrower will be another row here, and a deal the borrower has just answered is exactly
    the one that should move up the list - so this reads the table rather than the deal.
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
    one, and a deal with no runs and no audit rows - one stored by a script - still has to
    sort somewhere rather than raising.
    """
    moments = [deal.created_at, submitted_at, acted_at]
    moments += [run.created_at for run in (screen, underwrite) if run is not None]
    return max(moment for moment in moments if moment is not None)


def home_view(session: Session) -> HomeView:
    """Every deal, in its section, most recently touched first.  # SPEC §9.1

    ``id`` breaks a tie, so the order is stable across page loads and two deals touched in
    the same transaction do not swap. The sort is done here rather than in the ``ORDER BY``
    because the value being sorted on comes from four tables and the deal's own column; one
    query per source and a max in Python is both clearer and, on a list this size, not
    slower.
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

    entries = [
        HomeEntry(
            deal=deal,
            screen=screens.get(deal.id),
            underwrite=underwrites.get(deal.id),
            last_activity=last_activity(
                deal,
                screens.get(deal.id),
                underwrites.get(deal.id),
                submissions.get(deal.id),
                actions.get(deal.id),
            ),
        )
        for deal in deals
    ]
    entries.sort(key=lambda entry: (entry.last_activity, entry.deal.id), reverse=True)

    by_section: dict[Section, list[HomeEntry]] = {key: [] for key in Section}
    for entry in entries:
        by_section[section_of(entry.deal)].append(entry)
    return HomeView(sections=[HomeSection(key, by_section[key]) for key in Section])
