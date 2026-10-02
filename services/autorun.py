"""Run the screen and the underwrite without anyone pressing a button.  # SPEC §4.6, §7, §8

A deal that arrives complete is screened and priced on the way in, on whatever it carries:
the borrower's own numbers where the team has none, the config defaults where nobody has
entered an economic (``services/defaults.py``), and no court record at all. The deal page
then opens on a verdict and a ledger rather than two empty panels, with the flags that say
how much of it rests on a default or on the applicant's claim. The same two runs happen
again after every team edit - the override block and Edit Intake - so the results on the
page are never older than the inputs beside them.

Best effort, in a fixed order. The screen runs first and is kept whatever happens next; the
underwrite runs on a deal the screen did not close, and stands down - by name - when the
deal is short of something the ledger cannot do without (``DealNotReady``: today that is the
closing date and the term, since the rate has a default), when the engine will not price
what is there (``DealNotPriceable``), or when the screen declined it. Nothing here raises
for any of those: the edit or the intake that triggered the run has already been applied,
and refusing to store it because its underwrite could not run would be refusing the wrong
thing. What stopped is reported back, so the page can say so.

Every row an automatic run writes is recorded against the actor ``system``: nobody asked for
it, and a trail that named the borrower or the team member who saved an override as the
person who priced the deal would be wrong about who decided (SPEC §11).

Nothing here commits; the caller owns the transaction, as every service does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from schema.models import ScreenResult, Status, UnderwriteResult
from services.errors import (
    DealNotPriceable,
    DealNotReady,
    DealNotUnderwritable,
)
from services.lifecycle import UNDERWRITE_REFUSED_FROM, UNDERWRITE_REFUSED_UNTIL_COMPLETE
from services.requests import UnderwriteRequest
from services.runner import run_screen, run_underwrite

# The actor every automatic run is recorded as (SPEC §11): not a person, on purpose.
SYSTEM_ACTOR = "system"

# A deal in one of these is not run on at all: its intake is not finished, or it is closed.
NOT_RUN_FROM: frozenset[Status] = UNDERWRITE_REFUSED_UNTIL_COMPLETE | UNDERWRITE_REFUSED_FROM


@dataclass(frozen=True)
class AutoRun:
    """What an automatic run did, for the page to say.

    ``screen`` and ``underwrite`` are the results that were recorded, None where that stage
    did not run; ``skipped`` is one line per thing that stopped, in the order it stopped.
    """

    screen: ScreenResult | None = None
    underwrite: UnderwriteResult | None = None
    skipped: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.screen is not None or self.underwrite is not None

    def summary(self) -> str:
        """One sentence for a redirect notice: what was recorded and what stood down."""
        parts: list[str] = []
        if self.screen is not None:
            parts.append(f"Screen recorded: {self.screen.verdict.value}.")
        if self.underwrite is not None:
            irr = self.underwrite.return_overview.irr
            parts.append(
                "Underwrite recorded"
                + ("; the ledger has no IRR." if irr is None else f" at an IRR of {irr:.4%}.")
            )
        parts.extend(self.skipped)
        return " ".join(parts)


NOTHING_RAN = AutoRun()


def auto_run(session: Session, deal: Deal, config: Config | None = None) -> AutoRun:
    """Screen the deal, then underwrite it, each as far as the deal allows.  # SPEC §4.6

    The screen's own refusals - an intake gap the status did not catch, values the engine
    will not size - end the run before anything is written. Flushes; no commit.
    """
    cfg = config or get_config()
    if deal.status in NOT_RUN_FROM:
        return NOTHING_RAN
    try:
        screened = run_screen(session, deal.id, cfg, actor=SYSTEM_ACTOR)
    except DealNotReady as exc:
        return AutoRun(skipped=[f"The screen did not run: missing {', '.join(exc.missing)}."])
    except DealNotPriceable as exc:
        return AutoRun(skipped=[f"The screen did not run: {'; '.join(exc.reasons)}."])
    try:
        priced = run_underwrite(session, deal.id, UnderwriteRequest(), cfg, actor=SYSTEM_ACTOR)
    except DealNotUnderwritable:
        return AutoRun(
            screen=screened,
            skipped=[f"The underwrite did not run: the deal is {deal.status.value}."],
        )
    except DealNotReady as exc:
        return AutoRun(
            screen=screened,
            skipped=[f"The underwrite did not run: it still needs {', '.join(exc.missing)}."],
        )
    except DealNotPriceable as exc:
        return AutoRun(
            screen=screened,
            skipped=[f"The underwrite did not run: {'; '.join(exc.reasons)}."],
        )
    return AutoRun(screen=screened, underwrite=priced)
