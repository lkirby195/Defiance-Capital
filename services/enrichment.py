"""What enrichment has produced for a deal.  # SPEC §6

The first adapter is RentCast (SPEC §6.2, ``services/market_data.py``): its AVM is the as-is
value, its rent estimate the market rent, and its range reaches the engine for the two Info
flags. The court record and the estimated sale price are still the team's: no adapter
produces either, and the AVM is deliberately not a sale price - it values the property as it
stands, and the sale price is after the repair. The precedence rule is written down and
tested once here: **an adapter value always wins over a team value**, because the team is
standing in for a source that did not exist, not overriding one that does - and a team value
wins over the borrower's own estimate off the public form (SPEC §4.2), for the same reason.
Every superseded value stays on the deal in its own column, so a later reader can see what
was entered by hand, what the borrower claimed, and what superseded each.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from db.models import Deal
from schema.models import CourtRecordInputs, MarketDataInputs
from services.market_data import read_market_data


@dataclass(frozen=True)
class AdapterValues:
    """Enrichment results the engine can use directly; every field is optional."""

    # No adapter produces a sale price: the AVM is as-is (SPEC §6.2). The field stays so the
    # precedence rule - adapter, team, borrower - is written once for every value.
    estimated_sale_price: Decimal | None = None
    monthly_rent: Decimal | None = None  # RentCast's long-term rent estimate
    court_records: CourtRecordInputs | None = None
    as_is_value: Decimal | None = None  # RentCast's AVM
    # The AVM and rent ranges and whether the pull was skipped (SPEC §6.2); None when no
    # pull was ever attempted for the deal, which raises nothing.
    market_data: MarketDataInputs | None = None


NO_ADAPTER_VALUES = AdapterValues()


def adapter_values(session: Session, deal: Deal) -> AdapterValues:
    """The latest enrichment results for a deal, off its ``enrichment_runs`` rows.  # SPEC §5, §6

    Nothing until a pull was attempted for the deal, so a deal from before the adapter
    existed is assembled exactly as it was.
    """
    data = read_market_data(session, deal)
    if data is None:
        return NO_ADAPTER_VALUES
    return AdapterValues(
        monthly_rent=data.monthly_rent,
        as_is_value=data.as_is_value,
        market_data=MarketDataInputs(
            as_is_value_low=data.as_is_value_low,
            as_is_value_high=data.as_is_value_high,
            monthly_rent_low=data.monthly_rent_low,
            monthly_rent_high=data.monthly_rent_high,
            budget_exhausted=data.budget_exhausted,
        ),
    )
