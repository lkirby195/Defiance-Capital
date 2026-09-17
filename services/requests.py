"""The extra inputs an underwrite needs beyond what the deal already carries.  # SPEC §8.1

Everything here comes from paid pulls, the valuation, or the team at the moment they
advance a deal: it is not intake, so most of it is not on ``deals``. Each optional value
resolves the same way - this request, then what intake already stored on the deal, then the
engine default:

    as_is_value / arv     request -> deal.as_is_value_team / arv_team -> not ready (SPEC §8.1)
    annual taxes          request -> deal.actual_annual_taxes_usd -> % of as-is (SPEC §8.6)
    annual insurance      request -> deal.actual_annual_insurance_usd -> % of as-is
    asset type, exit      request -> deal -> unknown (SPEC §3)

An adapter value, when one exists, wins over every step of that (``services/enrichment.py``).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from schema.models import AssetType, RepeatBorrowerStatus, StatedExit


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
    # Holding costs and the DSCR takeout.
    market_rent_monthly: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    annual_taxes_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    annual_insurance_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    annual_utilities_usd: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    # Pricing and exit.
    extension_fee_pct: Decimal | None = Field(default=None, ge=0, le=1)
    exit_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # Overrides of what intake captured; None leaves the deal's own value in force.
    asset_type: AssetType | None = None
    stated_exit: StatedExit | None = None
    purchase_portion_override: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
