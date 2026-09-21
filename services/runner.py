"""Run the engine on a stored deal and record the result.  # SPEC §7, §8

The only I/O in a screen or an underwrite happens here: load the deal, hand typed inputs to
the pure engine, append a row, return the result. Neither function commits - the caller owns
the transaction boundary, so an API request or a script can run both in one unit of work.

Both take an actor and record an ``audit_log`` row beside the run (SPEC §5, §11). The
``screens`` / ``underwrites`` row says what the engine decided; the audit row says who asked
it to, which is the question an auditor has about a system holding credit and court data.
The audit row names the run it belongs to rather than repeating it.

Each function also moves the deal through the one lifecycle step its stage owns
(``services/lifecycle.py``): a screen leaves a never-screened deal SCREENED or DECLINED, an
underwrite leaves a screened or in-review deal UNDERWRITING, and a declined or dead deal is
refused rather than priced. Every other move on SPEC §4.5 stays a team action in the review
queue.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from engine.screen import screen
from engine.underwrite import underwrite
from schema.models import AuditAction, ScreenResult, Status, UnderwriteResult, Verdict
from services.assemble import screen_inputs, underwrite_inputs
from services.audit import DEALS, record_audit
from services.enrichment import AdapterValues, adapter_values
from services.errors import DealNotFound, DealNotUnderwritable
from services.lifecycle import (
    advance_after_screen,
    advance_for_underwrite,
    check_intake_complete,
    check_underwritable,
)
from services.persistence import record_screen, record_underwrite
from services.requests import UnderwriteRequest


def load_deal(session: Session, deal_id: UUID) -> Deal:
    """The deal, or ``DealNotFound``."""
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise DealNotFound(deal_id)
    return deal


def run_screen(
    session: Session,
    deal_id: UUID,
    config: Config | None = None,
    adapters: AdapterValues | None = None,
    *,
    actor: str,
) -> ScreenResult:
    """Screen a stored deal, append the ``screens`` row, advance the status.  # SPEC §7

    ``adapters`` is what enrichment produced; leaving it None reads it from the deal, which
    is nothing until Phase 3, so the team's own entries are what the engine runs on.
    """
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    before = deal.status
    inputs = screen_inputs(deal, adapters or adapter_values(session, deal))
    result = screen(inputs, cfg)
    row = record_screen(session, deal.id, inputs, result)
    advance_after_screen(deal, result.verdict)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.SCREEN_RUN,
        table_name=DEALS,
        row_id=deal.id,
        before={"status": before.value},
        after={
            "status": deal.status.value,
            "screen_id": str(row.id),
            "verdict": result.verdict.value,
            "engine_version": result.engine_version,
        },
    )
    return result


def run_underwrite(
    session: Session,
    deal_id: UUID,
    request: UnderwriteRequest,
    config: Config | None = None,
    adapters: AdapterValues | None = None,
    *,
    actor: str,
) -> UnderwriteResult:
    """Underwrite a stored deal, append the ``underwrites`` row, advance the status.  # SPEC §8

    An unscreened deal is screened first. The underwrite is Stage 2 (SPEC §8): it runs on
    deals that cleared Stage 1, and pricing one nobody has screened would skip the gate
    rather than pass it. The screen it runs is a real one - its row is recorded and it moves
    the status like any other - and a Decline stops the underwrite there.

    The status is otherwise checked before any work is done and nothing is written when it
    refuses: a declined or dead deal raises ``DealNotUnderwritable``, and one still short of
    the minimum viable intake raises ``DealNotReady`` naming the fields the team has yet to
    collect (SPEC §4.1).
    """
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    check_intake_complete(deal)
    resolved = adapters or adapter_values(session, deal)
    if deal.status is Status.NEW:
        if run_screen(session, deal_id, cfg, resolved, actor=actor).verdict is Verdict.DECLINE:
            raise DealNotUnderwritable(
                deal.id, deal.status, detail="the screen it just ran declined it"
            )
    check_underwritable(deal)
    before = deal.status
    inputs = underwrite_inputs(deal, request, resolved)
    result = underwrite(inputs, cfg)
    row = record_underwrite(session, deal.id, inputs, result)
    advance_for_underwrite(deal)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.UNDERWRITE_RUN,
        table_name=DEALS,
        row_id=deal.id,
        before={"status": before.value},
        after={
            "status": deal.status.value,
            "underwrite_id": str(row.id),
            "solved_rate": str(result.solved_rate),
            "engine_version": result.engine_version,
        },
    )
    return result
