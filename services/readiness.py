"""What the underwrite has to run on, and where each of it came from.  # SPEC §8.1

The queue's Run underwrite button takes no form: every SPEC §8.1 input lives on the deal by
the time somebody presses it (``api/routes/queue.py``). That is convenient and it used to be
opaque - the button either worked or came back with a refusal naming values the page had
never mentioned. This module is the page's answer to "what is it going to run on", one row
per §8.1 input, each with the value in force and where it came from:

    ADAPTER   an enrichment adapter or a paid pull produced it (SPEC §6)
    TEAM      somebody entered it by hand, on the intake form or the override block
    DEFAULT   nobody entered it and config has a stand-in (SPEC §8.6)
    MISSING   nobody entered it and there is no stand-in

``required`` marks the rows the run cannot proceed without, and ``missing`` is exactly those
of them that are MISSING. It is derived from the same rules the assembly raises
``DealNotReady`` on (``services/assemble.py``) rather than a second list beside them, so the
disabled button and the refusal behind it can never name different things.

An optional row that is MISSING is not a problem to fix before running - it is a thing the
underwrite will do without, and the note says what that costs: no DSCR takeout without a
market rent, the self-reported tranche without a verified score, UNKNOWN without a stated
exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from config.config import Config, get_config
from db.models import Deal
from schema.models import SPLIT_PRODUCTS, CourtRecordsStatus, Product
from services.assemble import TERM_BUCKET_MONTHS, intake_gaps
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues


class InputSource(StrEnum):
    """Where the value an underwrite would run on came from.  # SPEC §6, §8.1"""

    ADAPTER = "ADAPTER"
    TEAM = "TEAM"
    DEFAULT = "DEFAULT"
    MISSING = "MISSING"


@dataclass(frozen=True)
class InputRow:
    """One SPEC §8.1 input as the deal currently holds it."""

    key: str  # the name a refusal uses, so the page and the error agree
    label: str  # what the page calls it
    value: Any  # Decimal, int, an enum, or None
    money: bool  # render through the money filter
    source: InputSource
    required: bool
    note: str  # what happens without it

    @property
    def satisfied(self) -> bool:
        return self.source is not InputSource.MISSING


@dataclass(frozen=True)
class UnderwriteReadiness:
    """Every §8.1 input, and whether the run can go ahead."""

    rows: list[InputRow]
    intake_missing: list[str]  # minimum-viable intake the engine needs (SPEC §4.1)

    @property
    def missing(self) -> list[str]:
        """Everything that has to be there and is not, intake gaps first."""
        return self.intake_missing + [
            row.label for row in self.rows if row.required and not row.satisfied
        ]

    @property
    def ready(self) -> bool:
        return not self.missing


def _money_row(
    key: str,
    label: str,
    *,
    adapter: Decimal | None = None,
    team: Decimal | None = None,
    default: Decimal | None = None,
    required: bool = False,
    note: str = "",
) -> InputRow:
    """One money input, resolved adapter over team over config default.  # SPEC §6.1, §8.6"""
    if adapter is not None:
        value, source = adapter, InputSource.ADAPTER
    elif team is not None:
        value, source = team, InputSource.TEAM
    elif default is not None:
        value, source = default, InputSource.DEFAULT
    else:
        value, source = None, InputSource.MISSING
    return InputRow(
        key=key, label=label, value=value, money=True, source=source, required=required, note=note
    )


def _opex_default(as_is_value: Decimal | None, pct: Decimal) -> Decimal | None:
    """The config stand-in for an opex line; None while the as-is value it rests on is."""
    return None if as_is_value is None else as_is_value * pct


def _term_row(deal: Deal) -> InputRow:
    """Months from the bucket. 12_PLUS names no number, so somebody has to set one."""
    months = TERM_BUCKET_MONTHS.get(deal.term_bucket) if deal.term_bucket is not None else None
    return InputRow(
        key="term_months",
        label="Term (months)",
        value=months,
        money=False,
        source=InputSource.TEAM if months is not None else InputSource.MISSING,
        required=True,
        note=(
            ""
            if months is not None
            else "the 12+ bucket names no number of months; a run needs one (SPEC §8.1)"
        ),
    )


def _court_row(deal: Deal, adapters: AdapterValues) -> InputRow:
    """The court record in force, adapter over team.  # SPEC §6.1, §7.2"""
    value: Any = None
    source = InputSource.MISSING
    if adapters.court_records is not None:
        value, source = adapters.court_records.source, InputSource.ADAPTER
    elif (
        deal.court_records_status is not None
        and deal.court_records_status is not CourtRecordsStatus.NOT_CHECKED
    ):
        value, source = deal.court_records_status, InputSource.TEAM
    return InputRow(
        key="court_records",
        label="Court and filing search",
        value=value,
        money=False,
        source=source,
        required=False,
        note="" if source is not InputSource.MISSING else "not checked is not clean (SPEC §7.2)",
    )


def _split_rows(deal: Deal) -> list[InputRow]:
    """The two halves of a split loan; nothing at all on a product that has no split."""
    if deal.product not in SPLIT_PRODUCTS:
        return []
    names = (
        ("Principal Note", "Tranche A")
        if deal.product is Product.SPLIT_PRINCIPAL
        else ("Purchase portion", "Rehab holdback")
    )
    note = f"a {deal.product.value} loan is advanced in two parts (SPEC §8.2)"
    return [
        _money_row(
            "deal.loan_purchase_portion",
            names[0],
            team=deal.loan_purchase_portion,
            required=True,
            note=note,
        ),
        _money_row(
            "deal.loan_rehab_portion",
            names[1],
            team=deal.loan_rehab_portion,
            required=True,
            note=note,
        ),
    ]


def underwrite_readiness(
    deal: Deal,
    adapters: AdapterValues = NO_ADAPTER_VALUES,
    config: Config | None = None,
) -> UnderwriteReadiness:
    """Every SPEC §8.1 input on one deal, with its value, its source, and whether it is needed."""
    settings = config if config is not None else get_config()
    opex = settings.takeout.opex_defaults
    as_is = adapters.as_is_value if adapters.as_is_value is not None else deal.as_is_value_team
    rows = [
        _money_row(
            "as_is_value",
            "As-is value",
            adapter=adapters.as_is_value,
            team=deal.as_is_value_team,
            required=True,
            note="SPEC §8.1 requires a valuation before a deal is priced",
        ),
        _money_row(
            "arv",
            "ARV",
            adapter=adapters.arv,
            team=deal.arv_team,
            required=True,
            note="SPEC §8.1 requires a valuation before a deal is priced",
        ),
        *_split_rows(deal),
        _term_row(deal),
        _money_row(
            "market_rent_monthly",
            "Market rent (monthly)",
            team=deal.market_rent_monthly,
            note="without it the DSCR takeout is not evaluated (SPEC §8.6)",
        ),
        _money_row(
            "annual_taxes_usd",
            "Annual taxes",
            team=deal.actual_annual_taxes_usd,
            default=_opex_default(as_is, opex.taxes_pct_of_as_is_value),
            note=f"config default: {opex.taxes_pct_of_as_is_value:.2%} of the as-is value",
        ),
        _money_row(
            "annual_insurance_usd",
            "Annual insurance",
            team=deal.actual_annual_insurance_usd,
            default=_opex_default(as_is, opex.insurance_pct_of_as_is_value),
            note=f"config default: {opex.insurance_pct_of_as_is_value:.2%} of the as-is value",
        ),
        _money_row(
            "annual_utilities_usd",
            "Annual utilities",
            team=deal.actual_annual_utilities_usd,
            default=_opex_default(as_is, opex.utilities_pct_of_as_is_value),
            note=f"config default: {opex.utilities_pct_of_as_is_value:.2%} of the as-is value",
        ),
        InputRow(
            key="verified_credit_score",
            label="Verified credit score",
            value=None,
            money=False,
            source=InputSource.MISSING,
            required=False,
            note="no credit adapter yet; the self-reported tranche stands (SPEC §7.1)",
        ),
        _court_row(deal, adapters),
        InputRow(
            key="asset_type",
            label="Asset type",
            value=deal.asset_type,
            money=False,
            source=InputSource.TEAM if deal.asset_type is not None else InputSource.MISSING,
            required=False,
            note="with the term it infers the exit (SPEC §3)",
        ),
        InputRow(
            key="stated_exit",
            label="Stated exit",
            value=deal.stated_exit,
            money=False,
            source=InputSource.TEAM if deal.stated_exit is not None else InputSource.MISSING,
            required=False,
            note="blank leaves the exit to the SPEC §3 inference",
        ),
    ]
    return UnderwriteReadiness(rows=rows, intake_missing=intake_gaps(deal))
