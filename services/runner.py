"""Run the engine on a stored deal and record the result.  # SPEC §7, §8

The only I/O in a screen or an underwrite happens here: load the deal, hand typed inputs to
the pure engine, append a row, return the result. Neither function commits - the caller owns
the transaction boundary, so an API request or a script can run both in one unit of work.

Neither function touches ``deals.status`` either. Moving a deal through the lifecycle
(SPEC §4.5) is a team action in the review queue, not a side effect of running the math.
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
from services.errors import DealNotFound
from services.persistence import record_screen, record_underwrite
from services.requests import UnderwriteRequest


def load_deal(session: Session, deal_id: UUID) -> Deal:
    """The deal, or ``DealNotFound``."""
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise DealNotFound(deal_id)
    return deal


def run_screen(session: Session, deal_id: UUID, config: Config | None = None) -> ScreenResult:
    """Screen a stored deal and append the ``screens`` row.  # SPEC §7"""
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    inputs = screen_inputs(deal)
    result = screen(inputs, cfg)
    record_screen(session, deal.id, inputs, result)
    return result


def run_underwrite(
    session: Session,
    deal_id: UUID,
    request: UnderwriteRequest,
    config: Config | None = None,
) -> UnderwriteResult:
    """Underwrite a stored deal and append the ``underwrites`` row.  # SPEC §8"""
    cfg = config or get_config()
    deal = load_deal(session, deal_id)
    inputs = underwrite_inputs(deal, request)
    result = underwrite(inputs, cfg)
    record_underwrite(session, deal.id, inputs, result)
    return result
