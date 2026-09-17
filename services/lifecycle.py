"""Deal status transitions the engine runs drive.  # SPEC §4.5

Only two moves are automatic. Everything else on the lifecycle - advancing to IN_REVIEW,
sending an LOI, handing off, marking a deal dead - is a team action in the review queue.

    run_screen       NEW -> SCREENED, or NEW -> DECLINED on a Decline verdict
    run_underwrite   SCREENED or IN_REVIEW -> UNDERWRITING

A deal outside those starting states keeps the status it has: re-screening a deal the team
has already moved on does not drag it backwards, and re-underwriting one already in
UNDERWRITING is a no-op. A DECLINED or DEAD deal is refused outright rather than quietly
priced.
"""

from __future__ import annotations

from db.models import Deal
from schema.models import Status, Verdict
from services.errors import DealNotUnderwritable

# The screen only moves a deal that has never been screened.
SCREEN_ADVANCES_FROM: frozenset[Status] = frozenset({Status.NEW})
# The underwrite moves a deal the team has screened or pulled into review.
UNDERWRITE_ADVANCES_FROM: frozenset[Status] = frozenset({Status.SCREENED, Status.IN_REVIEW})
# A dead or declined deal is not priced until a person re-opens it.
UNDERWRITE_REFUSED_FROM: frozenset[Status] = frozenset({Status.DECLINED, Status.DEAD})


def status_after_screen(current: Status, verdict: Verdict) -> Status:
    """Where a screen leaves the deal.  # SPEC §4.5, §7.5"""
    if current not in SCREEN_ADVANCES_FROM:
        return current
    return Status.DECLINED if verdict is Verdict.DECLINE else Status.SCREENED


def status_after_underwrite(current: Status) -> Status:
    """Where an underwrite leaves the deal.  # SPEC §4.5, §8"""
    return Status.UNDERWRITING if current in UNDERWRITE_ADVANCES_FROM else current


def check_underwritable(deal: Deal) -> None:
    """Refuse to underwrite a deal the team has already closed out.  # SPEC §4.5"""
    if deal.status in UNDERWRITE_REFUSED_FROM:
        raise DealNotUnderwritable(deal.id, deal.status)


def advance_after_screen(deal: Deal, verdict: Verdict) -> Status:
    """Apply the post-screen transition in place; returns the status the deal now holds."""
    deal.status = status_after_screen(deal.status, verdict)
    return deal.status


def advance_for_underwrite(deal: Deal) -> Status:
    """Apply the post-underwrite transition in place; returns the status the deal now holds."""
    deal.status = status_after_underwrite(deal.status)
    return deal.status
