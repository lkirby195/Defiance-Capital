"""One borrower's page: who they are and every deal they have brought.  # SPEC §9.1, v0.20

The rows are the Home page's rows (``services/home.py``): the same ``HomeEntry`` with the
Working version's verdict and IRR, the deal's loan amount and the day it last moved, in the
same order. The totals at the foot are the whole list's - every deal, whatever its status -
because the question the page answers is "how much business has this person brought and how
did it go", and a dead deal is part of the answer. The average IRR is over the deals that
have one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import Borrower, Deal
from services.errors import ServiceError
from services.home import DEAD_STATUSES, HomeEntry, entries_for


class BorrowerNotFound(ServiceError):
    """No ``borrowers`` row with that id."""

    def __init__(self, borrower_id: UUID) -> None:
        super().__init__(f"no borrower {borrower_id}")
        self.borrower_id = borrower_id


@dataclass(frozen=True)
class BorrowerView:
    """The page: the borrower, their entities, their deals, and the totals."""

    borrower: Borrower
    entities: list[str]
    entries: list[HomeEntry]

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def live(self) -> int:
        """How many of the deals are neither dead nor declined."""
        return sum(1 for entry in self.entries if entry.deal.status not in DEAD_STATUSES)

    @property
    def total_loan(self) -> Decimal:
        """Every loan amount asked for, added up; a deal with none counts nothing."""
        return sum(
            (entry.deal.loan_requested for entry in self.entries if entry.deal.loan_requested),
            Decimal(0),
        )

    @property
    def average_irr(self) -> Decimal | None:
        """The plain mean of the IRRs on the deals that have one; None when none does."""
        rates = [entry.irr for entry in self.entries if entry.irr is not None]
        if not rates:
            return None
        return sum(rates, Decimal(0)) / Decimal(len(rates))


def load_borrower(session: Session, borrower_id: UUID) -> Borrower:
    """The borrower, or ``BorrowerNotFound``."""
    borrower = session.get(Borrower, borrower_id)
    if borrower is None:
        raise BorrowerNotFound(borrower_id)
    return borrower


def borrower_view(session: Session, borrower_id: UUID) -> BorrowerView:
    """Every deal on the borrower's row, most recently touched first.  # SPEC §9.1"""
    borrower = load_borrower(session, borrower_id)
    deals = list(
        session.scalars(
            select(Deal)
            .where(Deal.borrower_id == borrower.id)
            .options(selectinload(Deal.borrower), selectinload(Deal.property))
        )
    )
    return BorrowerView(
        borrower=borrower,
        entities=sorted(entity.name for entity in borrower.entities),
        entries=entries_for(session, deals),
    )
