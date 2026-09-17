"""Run the engine on a stored deal and record the result.  # SPEC §7, §8

The only I/O in a screen or an underwrite happens here: load the deal, hand typed inputs to
the pure engine, append a row, return the result. Neither function commits - the caller owns
the transaction boundary, so an API request or a script can run both in one unit of work.

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
from schema.models import ScreenResult, UnderwriteResult
from services.assemble import screen_inputs, underwrite_inputs
from services.enrichment import AdapterValues, adapter_values
from services.errors import DealNotFound
from services.lifecycle import (
    advance_after_screen,
    advance_for_underwrite,
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
) -> ScreenResult:
    """Screen a stored deal, append the ``screens`` row, advance the status.  # SPEC §7

    ``adapters`` is what enrichment produced; leaving it None reads it from the deal, which
    is nothing until Phase 3, so the team's own entries are what the engine runs on.
    """
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    inputs = screen_inputs(deal, adapters or adapter_values(session, deal))
    result = screen(inputs, cfg)
    record_screen(session, deal.id, inputs, result)
    advance_after_screen(deal, result.verdict)
    return result


def run_underwrite(
    session: Session,
    deal_id: UUID,
    request: UnderwriteRequest,
    config: Config | None = None,
    adapters: AdapterValues | None = None,
) -> UnderwriteResult:
    """Underwrite a stored deal, append the ``underwrites`` row, advance the status.  # SPEC §8

    The status is checked before any work is done: a declined or dead deal raises
    ``DealNotUnderwritable`` and nothing is written.
    """
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    check_underwritable(deal)
    inputs = underwrite_inputs(deal, request, adapters or adapter_values(session, deal))
    result = underwrite(inputs, cfg)
    record_underwrite(session, deal.id, inputs, result)
    advance_for_underwrite(deal)
    return result
