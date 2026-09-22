"""What the team supplies by hand, in the two shapes the queue collects it.  # SPEC §6.1, §8.1

``UnderwriteRequest`` is the SPEC §8.1 inputs at the moment of a run; ``TeamOverrides`` is
the standing block on the deal that the review queue edits.

Everything here comes from paid pulls, the valuation, or the team at the moment they
advance a deal: it is not intake, so most of it is not on ``deals``. Each optional value
resolves the same way - this request, then what intake already stored on the deal, then the
engine default:

    as_is_value / arv     request -> deal.as_is_value_team / arv_team -> not ready (SPEC §8.1)
    annual taxes          request -> deal.actual_annual_taxes_usd -> % of as-is (SPEC §8.6)
    annual insurance      request -> deal.actual_annual_insurance_usd -> % of as-is
    annual utilities      request -> deal.actual_annual_utilities_usd -> % of as-is (SPEC §8.6)
    market rent           request -> deal.market_rent_monthly -> no DSCR takeout (SPEC §8.6)
    asset type, exit      request -> deal -> unknown (SPEC §3)
    term months          request -> deal.term_months -> not ready (SPEC §8.1)

Market rent is the one with no third step. A percentage of a value stands in for a cost the
property incurs whatever it is worth - taxes, insurance, the utilities on a vacant house -
but nothing stands in for what it lets for, and a zero rent would not be neutral: it would
fabricate a DSCR shortfall on every deal whose rent nobody happened to look up. So a deal
without one is underwritten with the takeout NOT_EVALUATED and an INFO flag saying so,
rather than priced on a guess or refused outright.

The loan split is not here at all. It used to be (``purchase_portion_override``, a single
number typed at run time); it is now two columns on the deal, entered on the team-entry form
(SPEC §8.2), so there is one place it lives and one place it is edited.

An adapter value, when one exists, wins over every step of that (``services/enrichment.py``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schema.models import (
    AssetType,
    CourtRecordsStatus,
    Product,
    RepeatBorrowerStatus,
    StatedExit,
    TeamCourtRecord,
    validate_court_records,
)


class UnderwriteRequest(BaseModel):
    """SPEC §8.1 inputs supplied at underwrite time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Valuation (RicherValues or team override). Optional here because the team may
    # already have entered one on the deal; the underwrite still needs both halves from
    # somewhere (SPEC §8.1) and says so by name when it has neither.
    as_is_value: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    arv: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # Verified borrower facts; they replace the self-reported tranche and bucket.
    verified_credit_score: int | None = Field(default=None, ge=300, le=850)
    verified_deals_36mo: int | None = Field(default=None, ge=0)
    repeat_borrower_verified: RepeatBorrowerStatus | None = None
    # Term: from the bucket when the bucket names a number; the team sets it for 12_PLUS.
    term_months: int | None = Field(default=None, ge=1, le=60)
    # Holding costs and the DSCR takeout. Optional here because the team may already have
    # entered them on the deal; the underwrite still needs both from somewhere.
    market_rent_monthly: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    annual_taxes_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    annual_utilities_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    # Pricing and exit.
    extension_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    exit_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # Overrides of what intake captured; None leaves the deal's own value in force.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None


class TeamOverrides(BaseModel):
    """What the review queue lets a team member enter by hand on a deal.  # SPEC §6.1, §8.1

    The interim source. Until the Phase 3 adapters land the team is where the valuation and
    the court search come from, and utilities and market rent have no adapter planned at all;
    the queue's override block is where all of it is typed.

    A submission replaces the whole block rather than patching it: the form is rendered with
    the deal's current values in it, so what comes back is the state the team means the deal
    to be in, and a field left blank means the deal should not carry that value. The audit row
    records the before and after of every field that actually moved.

    ``product`` is the exception. It can be inferred (SPEC §3), and ``product_source`` records
    which - so a product equal to the one already on the deal leaves both columns untouched
    rather than relabelling an inferred product as entered, and a blank leaves the inferred
    product in place. The form cannot un-set a product; nothing about a deal makes one stop
    being known.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Valuation (SPEC §6.1); an adapter value still wins over either of these.
    as_is_value_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    arv_team: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # Opex and the DSCR takeout (SPEC §8.1, §8.6), all annual USD but the rent.
    actual_annual_taxes_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    actual_annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    actual_annual_utilities_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    market_rent_monthly: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    # Structure (SPEC §3): asset type and the stated exit drive the exit inference.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None
    product: Product | None = None
    # The term the deal is priced on (SPEC §8.1). Read-only on the page for every bucket
    # that names a number, and ``save_overrides`` re-derives it there rather than trusting
    # what came back; on 12_PLUS it is the one place the team can set one from the queue.
    term_months: int | None = Field(default=None, ge=1, le=60)
    # The team's own court search (SPEC §7.2), one typed matter per entry.
    court_records_status: CourtRecordsStatus | None = None
    court_records_as_of: date | None = None
    court_records_team: list[TeamCourtRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _court_records_are_coherent(self) -> TeamOverrides:
        """The same check ``DealInfo`` makes, so the queue answers with a message, not a 500."""
        validate_court_records(
            self.court_records_status, self.court_records_as_of, self.court_records_team
        )
        return self
