"""The analysis: the screen, then the underwrite, in one run.  # SPEC §4.6, §7, §8

The queue has one button for the two engine stages and runs them automatically besides. A
deal that arrives complete is screened and priced on the way in, on whatever it carries:
the borrower's own numbers where the team has none, the config defaults where nobody has
entered an economic (``services/defaults.py``), and no court record at all. The deal page
then opens on a verdict and a ledger rather than two empty panels, with the flags on the
stored results that say how much of it rests on a default or on the applicant's claim. The
same run happens again after every team edit - the Inputs block, Edit Intake, a restored
intake version - so the results on the page are never older than the inputs beside them,
and whenever a person presses Run Analysis.

Best effort, in a fixed order. The screen runs first and is kept whatever happens next; the
underwrite runs on a deal the screen did not close, and stands down - by name - when the
deal is short of something the ledger cannot do without (``DealNotReady``: today that is the
term, since the rate and the closing date have defaults), when the engine will not price
what is there (``DealNotPriceable``), or when the screen declined it. Nothing here raises
for any of those: the edit or the intake that triggered the run has already been applied,
and refusing to store it because its ledger could not run would be refusing the wrong
thing. What stopped is reported back, so the page can say so.

Two callers, two actors. ``run_analysis`` is the button: it runs on any deal, a paused one
included - a person pressing it has not left the deal alone - and records every row against
that person. ``auto_run`` is the automatic one: it leaves a paused, closed or incomplete deal
alone (SPEC §4.6) and records every row against the actor ``system``, because nobody asked
for it, and a trail that named the borrower or the team member who saved an edit as the
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

# A deal in one of these is not run on automatically: its intake is not finished, it is
# closed, or a person has set it aside (SPEC §4.6) - a paused deal is left exactly as it was
# paused. The button runs on all of them.
NOT_RUN_FROM: frozenset[Status] = (
    UNDERWRITE_REFUSED_UNTIL_COMPLETE | UNDERWRITE_REFUSED_FROM | {Status.PAUSED}
)


@dataclass(frozen=True)
class AutoRun:
    """What one analysis did, for the page to say.

    ``screen`` and ``underwrite`` are the results that were recorded, None where that stage
    did not run; ``skipped`` is one line per thing that stopped, in the order it stopped;
    ``refused`` is true when what stopped the ledger was the deal's status rather than its
    inputs, which the page answers with a conflict rather than a complaint.
    """

    screen: ScreenResult | None = None
    underwrite: UnderwriteResult | None = None
    skipped: list[str] = field(default_factory=list)
    refused: bool = False

    @property
    def ran(self) -> bool:
        return self.screen is not None or self.underwrite is not None

    def summary(self) -> str:
        """One sentence for a redirect notice: what was recorded and what stood down.

        The page knows one analysis, not two stages: the verdict and the IRR are its two
        numbers, and a line that said which engine stage produced which would be naming
        machinery the person never pressed a button for.
        """
        parts: list[str] = []
        if self.screen is not None:
            recorded = f"Analysis recorded: {self.screen.verdict.value}"
            if self.underwrite is not None:
                irr = self.underwrite.return_overview.irr
                recorded += (
                    "; the ledger has no IRR." if irr is None else f" at an IRR of {irr:.4%}."
                )
            else:
                recorded += "."
            parts.append(recorded)
        parts.extend(self.skipped)
        return " ".join(parts)


NOTHING_RAN = AutoRun()


def run_analysis(
    session: Session, deal: Deal, *, actor: str, config: Config | None = None
) -> AutoRun:
    """Screen the deal, then underwrite it, each as far as the deal allows.  # SPEC §4.6, §7, §8

    The screen's own refusals - an intake gap the status did not catch, values the engine
    will not size - end the run before anything is written. The underwrite's leave the
    screen in place and are reported. Flushes; no commit.
    """
    cfg = config or get_config()
    try:
        screened = run_screen(session, deal.id, cfg, actor=actor)
    except DealNotReady as exc:
        return AutoRun(skipped=[f"The analysis did not run: missing {', '.join(exc.missing)}."])
    except DealNotPriceable as exc:
        return AutoRun(skipped=[f"The analysis did not run: {'; '.join(exc.reasons)}."])
    try:
        priced = run_underwrite(session, deal.id, UnderwriteRequest(), cfg, actor=actor)
    except DealNotUnderwritable:
        return AutoRun(
            screen=screened,
            skipped=[f"The ledger did not run: the deal is {deal.status.value}."],
            refused=True,
        )
    except DealNotReady as exc:
        return AutoRun(
            screen=screened,
            skipped=[f"The ledger did not run: it still needs {', '.join(exc.missing)}."],
        )
    except DealNotPriceable as exc:
        return AutoRun(
            screen=screened,
            skipped=[f"The ledger did not run: {'; '.join(exc.reasons)}."],
        )
    return AutoRun(screen=screened, underwrite=priced)


def auto_run(session: Session, deal: Deal, config: Config | None = None) -> AutoRun:
    """The automatic analysis, as ``system``, on a deal that is live and complete.  # SPEC §4.6"""
    if deal.status in NOT_RUN_FROM:
        return NOTHING_RAN
    return run_analysis(session, deal, actor=SYSTEM_ACTOR, config=config)
