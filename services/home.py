"""What the Home page shows.  # SPEC §9.1, §12

Six sections, in this order, and every deal is in exactly one of them (v0.25):

    Borrower Submissions   live deals the borrower typed in on the public form (channel WEB)
    DCF New Deals          live deals the team entered (every other channel)
    Under LOI              status LOI_SENT or HANDED_OFF, whichever channel it came in on
    Closed                 status CLOSED: the loan closed and funded
    On Pause               status PAUSED, whichever channel it came in on
    Dead                   status DEAD or DECLINED, whichever channel it came in on

The first two are the work still being sized and leave out everything past it; Under LOI and
Closed are the road a deal is meant to take, Progress walking it along (SPEC §4.6); the last
two are where the rest went. Within each section the most recently touched deal is first.

"Most recently touched" is the latest of everything that has happened to the deal, not the
day it arrived: the intake that created it and any submission since, its latest screen, its
latest underwrite, and its latest ``audit_log`` row - which is every team action and every
note. A list ordered by arrival buries the deal somebody answered an hour ago under the one
nobody has opened in a fortnight, which is backwards: the deal being worked is the deal worth
seeing.

Each row carries what a person scans a list for - who, where, how much, which product, the
Working version's verdict and IRR, where it came from, when it last moved - and the four
controls (``services/actions.py``, ``services/delete.py``): Progress, Pause, Kill and Delete,
the first three live only from the statuses they apply from, so a button that would be
refused is off rather than a surprise. Progress names where it goes, and the move to Closed,
Kill and Delete each go through a confirmation page first.

**The verdict and the IRR are the Working version's** (``services/versions.py``), not the
latest run's: a draft somebody ran to try a number does not change what the list says the
deal is until they mark it Working. A deal with runs and no version at all - one priced
only through the JSON stage routes - shows its latest screen and underwrite instead, so a
result is never hidden for want of a version. The runs still count as activity either way.

**The search bar** (v0.25) narrows every section by three selects: **Borrower** - every
borrower with a deal on the list, by name - and two of the borrower's "About your investing"
answers (SPEC §4.1), **Commitment Level** (the full-time question) and **Years investing**.
``HomeFilter`` carries the borrower and, beside it, the **chips**: one ``(field, value)`` per
answer picked, each read off the borrower's row in ``matches``; a chip naming a ``deals``
column narrows by that column instead. The borrower's own page (``services/borrowers.py``)
lists the same rows for one borrower.
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

from db.models import AuditLog, Borrower, Deal, IntakeSubmission, Screen, Underwrite, Version
from schema.labels import enum_label, home_filter_caption, home_filter_label
from schema.masks import phone_display
from schema.models import BORROWER_PROFILE_FIELDS, PROFILE_ENUMS, Channel, Status, Verdict
from services.actions import MARK_DEAD_FROM, PAUSE_FROM, PROGRESS_FROM, progress_target
from services.audit import DEALS


class Section(StrEnum):
    """The six headings, in the order the page shows them."""

    BORROWER = "BORROWER"
    DCF = "DCF"
    LOI = "LOI"
    CLOSED = "CLOSED"
    PAUSED = "PAUSED"
    DEAD = "DEAD"


SECTION_TITLES: dict[Section, str] = {
    Section.BORROWER: "Borrower Submissions",
    Section.DCF: "DCF New Deals",
    Section.LOI: "Under LOI",
    Section.CLOSED: "Closed",
    Section.PAUSED: "On Pause",
    Section.DEAD: "Dead",
}

# The statuses that put a deal in the Dead section, whichever channel it came in on.
DEAD_STATUSES: frozenset[Status] = frozenset({Status.DEAD, Status.DECLINED})
# ...in Under LOI: terms went out, and handed off is the step after that (Phase 6).
LOI_STATUSES: frozenset[Status] = frozenset({Status.LOI_SENT, Status.HANDED_OFF})

# The profile questions Home filters by (v0.25): the full-time question, which Home calls
# Commitment Level, and years investing. The other three are read on the deal page and the
# borrower's page and not filtered on.
HOME_FILTER_FIELDS: tuple[str, ...] = ("work_status", "years_investing")
assert set(HOME_FILTER_FIELDS) <= set(BORROWER_PROFILE_FIELDS)

# The query-string key the Borrower select posts, and the chip it makes.
BORROWER_PARAM = "borrower"
BORROWER_CAPTION = "Borrower"
NO_NAME = "(no guarantor name)"


@dataclass(frozen=True)
class BorrowerChoice:
    """One option in Home's Borrower select: the borrower's id and the words it reads as."""

    id: UUID
    label: str


@dataclass(frozen=True)
class HomeFilter:
    """What narrows the list: the Borrower select, and the profile chips.  # SPEC §4.1, §9.1

    ``borrower_id`` is the Borrower select's answer: one person's deals and nobody else's.
    ``chips`` is ``(field, value)`` pairs, one per profile select set beside it -
    ``("years_investing", "2_5")`` - read off the borrower's row in ``matches``; a pair
    naming a ``deals`` column (``("status", "PAUSED")``) narrows by that column instead.
    """

    borrower_id: UUID | None = None
    chips: tuple[tuple[str, str], ...] = ()

    @property
    def active(self) -> bool:
        return self.borrower_id is not None or bool(self.chips)

    def selected(self, field: str, value: str) -> bool:
        """Whether a select's option is the one picked: the page marks it ``selected``."""
        if field == BORROWER_PARAM:
            return self.borrower_id is not None and str(self.borrower_id) == value
        return (field, value) in self.chips

    def without(self, field: str) -> HomeFilter:
        """The same filter less ``field``: the link a chip removes itself with."""
        if field == BORROWER_PARAM:
            return replace(self, borrower_id=None)
        return replace(self, chips=tuple(chip for chip in self.chips if chip[0] != field))

    @property
    def query_string(self) -> str:
        """The filter as Home's own query string, the borrower first; empty when unfiltered."""
        params = [(BORROWER_PARAM, str(self.borrower_id))] if self.borrower_id else []
        return urlencode([*params, *self.chips])


NO_FILTER = HomeFilter()


def borrower_from(params: Mapping[str, str]) -> UUID | None:
    """The Borrower select's answer off the query string; anything not an id is no answer,
    so a mangled bookmark shows the whole list rather than an error."""
    value = (params.get(BORROWER_PARAM) or "").strip()
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def chips_from(params: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The profile chips a query string carries: each Home filter field named with one of
    its answers, in field order. An unknown answer - or a field Home no longer filters by -
    is dropped rather than refused: the page still works, it just does not narrow by it.
    # SPEC §4.1, §9.1"""
    chips: list[tuple[str, str]] = []
    for field in HOME_FILTER_FIELDS:
        value = (params.get(field) or "").strip()
        if value and value in {member.value for member in PROFILE_ENUMS[field]}:
            chips.append((field, value))
    return tuple(chips)


def filter_from(params: Mapping[str, str]) -> HomeFilter:
    """Home's whole filter off its query string: the borrower, then the chips."""
    return HomeFilter(borrower_id=borrower_from(params), chips=chips_from(params))


def chip_caption(field: str) -> str:
    """The words a chip leads with: the filter's caption, else the column's name."""
    return home_filter_caption(field) if field in HOME_FILTER_FIELDS else enum_label(field)


def chip_label(field: str, value: str) -> str:
    """The words a chip's answer reads as: the filter's label, else the value title-cased."""
    return home_filter_label(value) if field in HOME_FILTER_FIELDS else enum_label(value)


def profile_filter_choices() -> list[tuple[str, str, list[tuple[str, str]]]]:
    """The two profile selects beside the Borrower one: ``(field, caption, [(value, label)])``."""
    return [
        (
            field,
            home_filter_caption(field),
            [(member.value, home_filter_label(member)) for member in PROFILE_ENUMS[field]],
        )
        for field in HOME_FILTER_FIELDS
    ]


def borrower_label(borrower: Borrower) -> str:
    """What a borrower reads as in the select: the name, else an entity, else the phone."""
    name = (borrower.name or "").strip()
    if name:
        return name
    entity = next((e.name for e in borrower.entities if (e.name or "").strip()), None)
    if entity:
        return entity.strip()
    return phone_display(borrower.phone) if borrower.phone else NO_NAME


def borrower_choices(deals: list[Deal]) -> list[BorrowerChoice]:
    """The Borrower select: every borrower with a deal, A to Z.  # SPEC §9.1 (v0.25)

    Built off the deals, so a borrower whose last deal was deleted is not offered. Two
    people who read the same carry their phone after the name, so the select never offers
    two options a person cannot tell apart.
    """
    borrowers = {deal.borrower.id: deal.borrower for deal in deals if deal.borrower is not None}
    labelled = [(borrower, borrower_label(borrower)) for borrower in borrowers.values()]
    seen: dict[str, int] = {}
    for _, text in labelled:
        seen[text.casefold()] = seen.get(text.casefold(), 0) + 1
    choices = [
        BorrowerChoice(
            borrower.id,
            f"{text} · {phone_display(borrower.phone)}"
            if seen[text.casefold()] > 1 and borrower.phone
            else text,
        )
        for borrower, text in labelled
    ]
    choices.sort(key=lambda choice: (choice.label.casefold(), str(choice.id)))
    return choices


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
    """Whether a row stays on the list under ``filter``: the borrower, then every chip."""
    if filter.borrower_id is not None and entry.deal.borrower_id != filter.borrower_id:
        return False
    return all(chip_matches(entry.deal, field, value) for field, value in filter.chips)


def section_of(deal: Deal) -> Section:
    """Which section a deal belongs in: its status first, then where it came from."""
    if deal.status is Status.PAUSED:
        return Section.PAUSED
    if deal.status in DEAD_STATUSES:
        return Section.DEAD
    if deal.status in LOI_STATUSES:
        return Section.LOI
    if deal.status is Status.CLOSED:
        return Section.CLOSED
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
    def progress_to(self) -> Status | None:
        """Where Progress takes this deal: ``PAUSED`` for back where it was, None for nowhere."""
        return progress_target(self.deal.status)

    @property
    def progress_closes(self) -> bool:
        """Whether Progress would close the loan: the button is a link to its confirmation."""
        return self.progress_to is Status.CLOSED

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
class FilterChip:
    """One chip under the search bar: what it reads and the query string without it."""

    field: str
    text: str
    rest: str


@dataclass(frozen=True)
class HomeView:
    """The whole page: the six sections, every one of them present even when empty,
    the filter they were narrowed by, the Borrower select's options, and how many deals
    there were before the filter."""

    sections: list[HomeSection]
    filter: HomeFilter = NO_FILTER
    unfiltered_total: int = 0
    borrowers: tuple[BorrowerChoice, ...] = ()

    @property
    def chips(self) -> list[FilterChip]:
        """The chips the filter makes, the borrower first: each a link that removes itself."""
        out: list[FilterChip] = []
        if self.filter.borrower_id is not None:
            name = next((c.label for c in self.borrowers if c.id == self.filter.borrower_id), None)
            out.append(
                FilterChip(
                    BORROWER_PARAM,
                    f"{BORROWER_CAPTION}: {name or NO_NAME}",
                    self.filter.without(BORROWER_PARAM).query_string,
                )
            )
        out.extend(
            FilterChip(
                name,
                f"{chip_caption(name)}: {chip_label(name, value)}",
                self.filter.without(name).query_string,
            )
            for name, value in self.filter.chips
        )
        return out

    @property
    def chip_summary(self) -> str:
        """The chips as words, comma-separated: what the result line says it narrowed by."""
        return ", ".join(chip.text for chip in self.chips)

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
            select(Deal).options(
                selectinload(Deal.borrower).selectinload(Borrower.entities),
                selectinload(Deal.property),
            )
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
        borrowers=tuple(borrower_choices(deals)),
    )
