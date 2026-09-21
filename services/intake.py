"""Store an ``IntakeRecord`` and record who entered it.  # SPEC §4.2, §5, §11

``db/repository.py`` knows how to turn a record into rows. This is the seam that gives that
write an actor, so a deal that appears in the queue can be traced to the person who typed it
in - the same rule every other service write follows.

It exists as its own thin function rather than as an argument to the repository because the
repository is also used by the CLI, which has no database and no actor: a fixture run builds
the same rows in memory and must not need a name to do it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Deal
from db.repository import create_deal_from_intake
from schema.models import AuditAction, IntakeRecord
from services.audit import DEALS, record_audit


def create_deal(session: Session, record: IntakeRecord, *, actor: str) -> Deal:
    """Create the deal and its immutable submission row, with an audit row. No commit."""
    deal = create_deal_from_intake(session, record)
    record_audit(
        session,
        actor=actor,
        action=AuditAction.INTAKE_CREATED,
        table_name=DEALS,
        row_id=deal.id,
        after={
            "channel": deal.channel.value,
            "status": deal.status.value,
            "missing_fields": list(deal.missing_fields),
        },
    )
    return deal
