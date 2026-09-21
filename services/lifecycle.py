"""Deal status transitions the engine runs drive.  # SPEC §4.5

Only these moves are automatic. Everything else on the lifecycle - advancing to IN_REVIEW,
sending an LOI, handing off, marking a deal dead - is a team action in the review queue.

    run_screen       NEW -> SCREENED
                     NEW, SCREENED or IN_REVIEW -> DECLINED on a Decline verdict
    run_underwrite   SCREENED or IN_REVIEW -> UNDERWRITING

A Decline closes a deal the team has not started pricing, wherever in those three states it
sits: a re-screen that turns up a Hard flag on a deal sitting in review is exactly the case
worth acting on. From UNDERWRITING onwards it does not: the deal is being worked, a person
owns it, and the Hard flags are recorded on the screens row for them to read rather than
yanked out from under them.

Everything else keeps the status it has: a non-Decline re-screen never drags a deal
backwards, and re-underwriting one already in UNDERWRITING is a no-op. A DECLINED or DEAD
deal is refused outright rather than quietly priced, and so is a NEEDS_INFO one: its intake
is not finished, so there is nothing to price yet.
"""

from __future__ import annotations

from db.models import Deal
from schema.models import Status, Verdict
from services.errors import DealNotReady, DealNotUnderwritable

# A clean screen only moves a deal that has never been screened.
SCREEN_ADVANCES_FROM: frozenset[Status] = frozenset({Status.NEW})
# A Decline closes a deal anywhere up to the point where a person is pricing it.
DECLINE_CLOSES_FROM: frozenset[Status] = frozenset({Status.NEW, Status.SCREENED, Status.IN_REVIEW})
# The underwrite moves a deal the team has screened or pulled into review.
UNDERWRITE_ADVANCES_FROM: frozenset[Status] = frozenset({Status.SCREENED, Status.IN_REVIEW})
# A dead or declined deal is not priced until a person re-opens it.
UNDERWRITE_REFUSED_FROM: frozenset[Status] = frozenset({Status.DECLINED, Status.DEAD})
# An intake the team is still chasing is not priced at all.
UNDERWRITE_REFUSED_UNTIL_COMPLETE: frozenset[Status] = frozenset({Status.NEEDS_INFO})


def status_after_screen(current: Status, verdict: Verdict) -> Status:
    """Where a screen leaves the deal.  # SPEC §4.6, §7.5"""
    if verdict is Verdict.DECLINE:
        return Status.DECLINED if current in DECLINE_CLOSES_FROM else current
    return Status.SCREENED if current in SCREEN_ADVANCES_FROM else current


def status_after_underwrite(current: Status) -> Status:
    """Where an underwrite leaves the deal.  # SPEC §4.5, §8"""
    return Status.UNDERWRITING if current in UNDERWRITE_ADVANCES_FROM else current


def check_underwritable(deal: Deal) -> None:
    """Refuse to underwrite a deal the team has already closed out.  # SPEC §4.5"""
    if deal.status in UNDERWRITE_REFUSED_FROM:
        raise DealNotUnderwritable(deal.id, deal.status)


def check_intake_complete(deal: Deal) -> None:
    """Refuse to underwrite a deal whose intake is still short.  # SPEC §4.1, §4.6

    NEEDS_INFO means the minimum viable intake is incomplete and the team is still asking
    the borrower for it. Pricing one would either fail deeper in on the first absent value
    or, worse, succeed on the handful that happen to be there and leave an ``underwrites``
    row on a deal nobody could act on - the status does not advance out of NEEDS_INFO, so
    nothing on the deal would say the price was struck on a part-filled intake.

    ``DealNotReady`` carries ``deal.missing_fields`` as the normalizer wrote it, so the
    refusal names what to go and ask for rather than only that the deal is not ready.
    """
    if deal.status not in UNDERWRITE_REFUSED_UNTIL_COMPLETE:
        return
    raise DealNotReady(deal.id, list(deal.missing_fields) or ["the minimum viable intake"])


def advance_after_screen(deal: Deal, verdict: Verdict) -> Status:
    """Apply the post-screen transition in place; returns the status the deal now holds."""
    deal.status = status_after_screen(deal.status, verdict)
    return deal.status


def advance_for_underwrite(deal: Deal) -> Status:
    """Apply the post-underwrite transition in place; returns the status the deal now holds."""
    deal.status = status_after_underwrite(deal.status)
    return deal.status
