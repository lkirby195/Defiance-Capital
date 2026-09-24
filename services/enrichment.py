"""What enrichment has produced for a deal.  # SPEC §6

The adapters are Phase 3. Until they land this returns nothing, and the team's own entries
(SPEC §7.2, §8.1) are what the engine runs on. The seam exists now so the precedence rule
is written down and tested once rather than retrofitted: **an adapter value always wins over
a team value**, because the team is standing in for a source that does not exist yet, not
overriding one that does. The team's entry stays on the deal either way, so a later reader
can see what was entered by hand and what superseded it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from db.models import Deal
from schema.models import CourtRecordInputs


@dataclass(frozen=True)
class AdapterValues:
    """Enrichment results the engine can use directly; every field is optional."""

    estimated_sale_price: Decimal | None = None
    court_records: CourtRecordInputs | None = None


NO_ADAPTER_VALUES = AdapterValues()


def adapter_values(session: Session, deal: Deal) -> AdapterValues:
    """The latest enrichment results for a deal.

    Phase 3 reads ``enrichment_runs`` here (SPEC §5, §6). There is nothing to read yet, so
    every caller falls through to the team's own entries.
    """
    del session, deal  # Phase 3
    return NO_ADAPTER_VALUES
