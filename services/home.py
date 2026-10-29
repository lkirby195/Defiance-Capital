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
Working version's verdict and IRR, where it came from, when it last moved - and the three
controls
(``services/actions.py``): Progress, Pause and Kill, each live only from the statuses it
applies from, so a button that would be refused is off rather than a surprise.

**The verdict and the IRR are the Working version's** (``services/versions.py``), not the
latest run's: a draft somebody ran to try a number does not change what the list says the
deal is until they mark it Working. A deal with runs and no version at all - one priced
only through the JSON stage routes - shows its latest screen and underwrite instead, so a
result is never hidden for want of a version. The runs still count as activity either way.

**The search box** (v0.20) narrows every section to the deals whose borrower matches: the
guarantor's name, an entity's name, or the phone by its digits. ``HomeFilter`` carries
the query and, beside it, the **profile-field chips** (10c, SPEC §4.1): one ``(field, value)``
per "About your investing" answer picked in the selects under the box, each read off the
borrower's row and applied in ``matches``; a chip naming a ``deals`` column narrows by that
column instead. The borrower's own page (``services/borrowers.py``) lists the same rows for
one borrower.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import AuditLog, Deal, IntakeSubmission, Screen, Underwrite, Version
from schema.labels import enum_label, profile_caption, profile_label
from schema.models import BORROWER_PROFILE_FIELDS, PROFILE_ENUMS, Channel, Status, Verdict
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

# A phone is searched by its digits once the query holds this many of them; fewer and a
# query like "3rd" is a name, not a number.
PHONE_SEARCH_DIGITS = 3


@dataclass(frozen=True)
class HomeFilter:
    """What narrows the list: the search box's query, and the profile chips.  # SPEC §4.1, §9.1

    ``chips`` is ``(field, value)`` pairs, one per "About your investing" select set under
    the search box - ``("years_investing", "2_5")`` - read off the borrower's row in
    ``matches``; a pair naming a ``deals`` column (``("status", "PAUSED")``) narrows by that
    column instead. Each is rendered as a chip under the box that removes itself.
    """

    query: str = ""
    chips: tuple[tuple[str, str], ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.query.strip()) or bool(self.chips)

    @property
    def chip_summary(self) -> str:
        """The chips as words: each one's caption and answer, comma-separated."""
        return ", ".join(
            f"{chip_caption(field)}: {chip_label(field, value)}" for field, value in self.chips
        )

    @property
    def summary(self) -> str:
        """What the result line says the list was narrowed by: the query in quotes, then the
        chips. The page composes the same line itself so the query is escaped on its own."""
        parts = [f'"{self.query.strip()}"'] if self.query.strip() else []
        if self.chips:
            parts.append(self.chip_summary)
        return ", ".join(parts)

    def without(self, field: str) -> HomeFilter:
        """The same filter less every chip on ``field``: the link a chip removes itself with."""
        return replace(self, chips=tuple(chip for chip in self.chips if chip[0] != field))

    @property
    def query_string(self) -> str:
        """The filter as Home's own query string, ``q`` first, empty when nothing narrows."""
        params = [("q", self.query.strip())] if self.query.strip() else []
        return urlencode([*params, *self.chips])


NO_FILTER = HomeFilter()


def chips_from(params: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The profile chips a query string carries: each profile field named with one of its
    answers, in field order. An unknown answer is dropped rather than refused - the page
    still works, it just does not narrow by it.  # SPEC §4.1 (10c)"""
    chips: list[tuple[str, str]] = []
    for field in BORROWER_PROFILE_FIELDS:
        value = (params.get(field) or "").strip()
        if value and value in {member.value for member in PROFILE_ENUMS[field]}:
            chips.append((field, value))
    return tuple(chips)


def chip_caption(field: str) -> str:
    """The words a chip leads with: the profile question's caption, else the column's name."""
    return profile_caption(field) if field in BORROWER_PROFILE_FIELDS else enum_label(field)


def chip_label(field: str, value: str) -> str:
    """The words a chip's answer reads as: the profile label, else the value title-cased."""
    return profile_label(value) if field in BORROWER_PROFILE_FIELDS else enum_label(value)


def profile_filter_choices() -> list[tuple[str, str, list[tuple[str, str]]]]:
    """The five selects under Home's search box: ``(field, caption, [(value, label), ...])``."""
    return [
        (
            field,
            profile_caption(field),
            [(member.value, profile_label(member)) for member in PROFILE_ENUMS[field]],
        )
        for field in BORROWER_PROFILE_FIELDS
    ]


def digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def query_matches(deal: Deal, query: str) -> bool:
    """Whether the deal's borrower answers to ``query``: name, entity or phone.  # SPEC §9.1

    Names and entities by case-blind substring; the phone by its digits, so ``555-0147``
    and ``5550147`` find the same person, once the query carries enough digits to be one.
    A blank query matches everything. A deal with no borrower row matches nothing but a
    blank query.
    """
    needle = query.strip().casefold()
    if not needle:
        return True
    borrower = deal.borrower
    if borrower is None:
        return False
    haystacks = [borrower.name or "", *(entity.name for entity in borrower.entities)]
    if any(needle in text.casefold() for text in haystacks):
        return True
    wanted = digits(needle)
    return len(wanted) >= PHONE_SEARCH_DIGITS and wanted in digits(borrower.phone or "")


def chip_matches(deal: Deal, field: str, value: str) -> bool:
    """One chip against one deal: a profile field is the borrower's row's answer (SPEC §4.1),
    anything else the deal's own column. A deal with no borrower row matches no profile chip,
    and an unanswered question matches nothing."""
    if field in BORROWER_PROFILE_FIELDS:
        if deal.borrower is None:
            return False
        held = getattr(deal.borrower, field)
    else:
        held = getattr(deal, field, None)
    return held is not None and str(held) == value


def matches(entry: HomeEntry, filter: HomeFilter) -> bool:
    """Whether a row stays on the list under ``filter``: the query, then every chip."""
    if not query_matches(entry.deal, filter.query):
        return False
    return all(chip_matches(entry.deal, field, value) for field, value in filter.chips)


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
class WorkingSummary:
    """The deal's Working version, reduced to what the list shows."""

    number: int
    name: str
    verdict: Verdict | None
    irr: Decimal | None


@dataclass(frozen=True)
class HomeEntry:
    """One deal on the list, with what the row shows and which controls are live."""

    deal: Deal
    screen: RunSummary | None
    underwrite: RunSummary | None
    last_activity: datetime
    working: WorkingSummary | None = None

    @property
    def verdict(self) -> Verdict | None:
        if self.working is not None:
            return self.working.verdict
        return self.screen.verdict if self.screen is not None else None

    @property
    def irr(self) -> Decimal | None:
        if self.working is not None:
            return self.working.irr
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
    """The whole page: the four sections, every one of them present even when empty,
    the filter they were narrowed by, and how many deals there were before it."""

    sections: list[HomeSection]
    filter: HomeFilter = NO_FILTER
    unfiltered_total: int = 0

    @property
    def total(self) -> int:
        """How many deals are on the page: after the filter, when there is one."""
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


def _working(session: Session, deal_ids: list[UUID]) -> dict[UUID, WorkingSummary]:
    """Each analyzed deal's Working version: its name, its screen's verdict, its IRR."""
    if not deal_ids:
        return {}
    rows = session.execute(
        select(Version.deal_id, Version.number, Version.name, Screen.verdict, Underwrite.irr)
        .outerjoin(Screen, Screen.id == Version.screen_id)
        .outerjoin(Underwrite, Underwrite.id == Version.underwrite_id)
        .where(Version.deal_id.in_(deal_ids), Version.is_working)
    )
    return {
        deal_id: WorkingSummary(number, name, verdict, irr)
        for deal_id, number, name, verdict, irr in rows
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


def entries_for(session: Session, deals: list[Deal]) -> list[HomeEntry]:
    """The given deals as rows, most recently touched first.  # SPEC §9.1

    ``id`` breaks a tie, so the order is stable across page loads and two deals touched in
    the same transaction do not swap. The sort is done here rather than in the ``ORDER BY``
    because the value being sorted on comes from four tables and the deal's own column;
    one query per source and a max in Python is both clearer and, on a list this size, not
    slower. Home and the borrower's page (``services/borrowers.py``) both build on it.
    """
    deal_ids = [deal.id for deal in deals]
    screens = _latest_screens(session, deal_ids)
    underwrites = _latest_underwrites(session, deal_ids)
    working = _working(session, deal_ids)
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
            working=working.get(deal.id),
        )
        for deal in deals
    ]
    entries.sort(key=lambda entry: (entry.last_activity, entry.deal.id), reverse=True)
    return entries


def home_view(session: Session, filter: HomeFilter = NO_FILTER) -> HomeView:
    """Every deal, in its section, most recently touched first; narrowed by ``filter``."""
    deals = list(
        session.scalars(
            select(Deal).options(selectinload(Deal.borrower), selectinload(Deal.property))
        )
    )
    entries = entries_for(session, deals)
    by_section: dict[Section, list[HomeEntry]] = {key: [] for key in Section}
    for entry in entries:
        if matches(entry, filter):
            by_section[section_of(entry.deal)].append(entry)
    return HomeView(
        sections=[HomeSection(key, by_section[key]) for key in Section],
        filter=filter,
        unfiltered_total=len(entries),
    )
