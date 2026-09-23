"""What the underwrite has to run on, and where each of it came from.  # SPEC §8.1, §9.2

The queue's Run underwrite button takes no form: every SPEC §8.1 input lives on the deal by
the time somebody presses it (``api/routes/queue.py``). That is convenient and it used to be
opaque - the button either worked or came back with a refusal naming values the page had
never mentioned. This module is the page's answer to "what is it going to run on", one row
per §8.1 input, each with the value in force and where it came from:

    ADAPTER   an enrichment adapter or a paid pull produced it (SPEC §6)
    TEAM      somebody entered it by hand, on the intake form or the override block
    DEFAULT   nobody entered it and config has a stand-in (SPEC §8.1)
    MISSING   nobody entered it and there is no stand-in

``required`` marks the rows the run cannot proceed without, and ``missing`` is exactly those
of them that are MISSING. It is derived from the same rules the assembly raises
``DealNotReady`` on (``services/assemble.py``) rather than a second list beside them - the
required set, and the Flip toggle that decides whether the estimated sale price is in it,
are both read from there - so the disabled button and the refusal behind it can never name
different things.

An optional row that is MISSING is not a problem to fix before running - it is a thing the
underwrite will do without, and the note says what that costs: no DSCR at all without a
monthly rent, LTV on the purchase price without an as-is value, the self-reported tranche
without a verified score, UNKNOWN without a stated exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from config.config import Config, get_config
from db.models import Deal
from schema.dates import payoff_date_for
from schema.models import SPLIT_PRODUCTS, CourtRecordsStatus, Product, months_for_bucket
from services.assemble import flip_is_on, intake_gaps, rental_is_on
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues
from services.requests import UnderwriteRequest

ZERO = Decimal(0)

# How the page renders a row's value. Not every §8.1 input is money any more: the rate, the
# contingency and the origination fee are percentages and the two dates are dates, and a
# percentage rendered through the money filter reads as two cents.
RowFormat = Literal["money", "pct", "plain"]


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
    value: Any  # Decimal, int, a date, an enum, or None
    fmt: RowFormat  # how the page renders it
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


def _row(
    key: str,
    label: str,
    value: Any,
    *,
    fmt: RowFormat = "plain",
    source: InputSource | None = None,
    required: bool = False,
    note: str = "",
) -> InputRow:
    """One row whose value is simply on the deal: TEAM when set, MISSING when not."""
    if source is None:
        source = InputSource.TEAM if value is not None else InputSource.MISSING
    return InputRow(
        key=key, label=label, value=value, fmt=fmt, source=source, required=required, note=note
    )


def _resolved_row(
    key: str,
    label: str,
    *,
    fmt: RowFormat = "money",
    adapter: Decimal | None = None,
    team: Decimal | None = None,
    default: Decimal | None = None,
    required: bool = False,
    note: str = "",
) -> InputRow:
    """One input resolved adapter over team over config default.  # SPEC §6.1, §8.1"""
    if adapter is not None:
        value, source = adapter, InputSource.ADAPTER
    elif team is not None:
        value, source = team, InputSource.TEAM
    elif default is not None:
        value, source = default, InputSource.DEFAULT
    else:
        value, source = None, InputSource.MISSING
    return InputRow(
        key=key, label=label, value=value, fmt=fmt, source=source, required=required, note=note
    )


def _term_row(deal: Deal) -> InputRow:
    """The term on the deal, and where it came from.  # SPEC §8.1

    DEFAULT rather than TEAM while the value is still the one the bucket seeded: nobody chose
    9 months on a 9-month bucket, the bucket did. A term that differs from the bucket - or one
    on a ``12_PLUS`` bucket, which names none - is a person's own.
    """
    named = months_for_bucket(deal.term_bucket)
    if deal.term_months is None:
        source = InputSource.MISSING
    elif deal.term_months == named:
        source = InputSource.DEFAULT
    else:
        source = InputSource.TEAM
    bucket = deal.term_bucket
    if bucket is None:
        note = "no term bucket on the deal; the team's own number is all there is"
    elif named is None:
        note = "the 12+ bucket names no months; enter a term, or a payoff date to imply one"
    elif deal.term_months == named:
        note = f"seeded by the {bucket.value}-month bucket"
    else:
        note = f"the team's own; the {bucket.value}-month bucket was the ask"
    return _row(
        "deal.term_months",
        "Term (months)",
        deal.term_months,
        source=source,
        required=True,
        note=note,
    )


def _payoff_row(deal: Deal) -> InputRow:
    """The payoff date, derived from the closing date and the term.  # SPEC §8.1

    Never required and never a reason the button is off: it is not a column, it is
    ``closing_date + term_months``, and the two rows above it are what turn the button off.
    It is on the checklist because it is the date the ledger's last row carries, and a person
    wants to see it before they press anything.
    """
    payoff = None
    if deal.closing_date is not None and deal.term_months is not None:
        payoff = payoff_date_for(deal.closing_date, deal.term_months)
    return _row(
        "payoff_date",
        "Payoff date",
        payoff,
        source=InputSource.DEFAULT if payoff is not None else InputSource.MISSING,
        note="derived: the closing date plus the term, to the day (SPEC §8.1)",
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
    return _row(
        "court_records",
        "Court and filing search",
        value,
        source=source,
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
        _resolved_row(
            "deal.loan_purchase_portion",
            names[0],
            team=deal.loan_purchase_portion,
            required=True,
            note=note,
        ),
        _resolved_row(
            "deal.loan_rehab_portion",
            names[1],
            team=deal.loan_rehab_portion,
            required=True,
            note=note,
        ),
    ]


def _toggle_row(key: str, label: str, on: bool, chosen: bool | None, default_note: str) -> InputRow:
    """One analysis toggle: the state it is in, and whether a person or the §3 exit set it."""
    return _row(
        key,
        label,
        "on" if on else "off",
        source=InputSource.TEAM if chosen is not None else InputSource.DEFAULT,
        note="set by hand" if chosen is not None else default_note,
    )


def underwrite_readiness(
    deal: Deal,
    adapters: AdapterValues = NO_ADAPTER_VALUES,
    config: Config | None = None,
) -> UnderwriteReadiness:
    """Every SPEC §8.1 input on one deal, with its value, its source, and whether it is needed."""
    settings = config if config is not None else get_config()
    fees = settings.fees
    empty = UnderwriteRequest()
    flip_on = flip_is_on(deal, empty, deal.term_months, settings)
    rental_on = rental_is_on(deal, empty, deal.term_months, settings)
    cost = (deal.purchase_price or ZERO) + (deal.rehab_costs or ZERO)
    rows = [
        _row(
            "deal.closing_date",
            "Closing date",
            deal.closing_date,
            required=True,
            note="month 0 of the ledger (SPEC §8.3)",
        ),
        _term_row(deal),
        _payoff_row(deal),
        _row(
            "deal.interest_rate",
            "Interest rate",
            deal.interest_rate,
            fmt="pct",
            required=True,
            note="annual; nothing stands in for a rate nobody chose (SPEC §8.1)",
        ),
        *_split_rows(deal),
        _resolved_row(
            "estimated_sale_price",
            "Estimated sale price",
            adapter=adapters.estimated_sale_price,
            team=deal.estimated_sale_price_team,
            required=flip_on,
            note=(
                "the Flip analysis sells at it (SPEC §8.4)"
                if flip_on
                else "the Flip analysis is off; without one, LTARV is not computed (SPEC §7.4)"
            ),
        ),
        _resolved_row(
            "as_is_value",
            "As-is value",
            adapter=adapters.as_is_value,
            team=deal.as_is_value_team,
            note="LTV only; without one it falls back to the purchase price (SPEC §7.4)",
        ),
        _resolved_row(
            "monthly_rent",
            "Monthly rent",
            team=deal.monthly_rent,
            note="without it the Rental and Take-Back analyses are not evaluated (SPEC §8.5)",
        ),
        _resolved_row(
            "contingency_pct",
            "Contingency",
            fmt="pct",
            team=deal.contingency_pct,
            default=fees.contingency_default_pct,
            note=f"config default: {fees.contingency_default_pct:.2%} of the rehab costs",
        ),
        _resolved_row(
            "closing_costs_usd",
            "Closing costs",
            team=deal.closing_costs_usd,
            default=fees.closing_costs_default_usd,
            note="the lender's own, inside the LTC denominator (SPEC §8.2)",
        ),
        _resolved_row(
            "holding_costs_total_usd",
            "Holding costs (total)",
            team=deal.holding_costs_total_usd,
            default=cost * fees.holding_costs_default_pct_of_cost if cost > ZERO else None,
            note=(
                f"config default: {fees.holding_costs_default_pct_of_cost:.2%} of price plus "
                "rehab, over the whole hold"
            ),
        ),
        _resolved_row(
            "origination_fee_pct",
            "Origination fee",
            fmt="pct",
            team=deal.origination_fee_pct,
            default=fees.origination_default_pct,
            note=(
                f"config default: {fees.origination_default_pct:.2%}, half at close and half "
                "at payoff"
            ),
        ),
        _toggle_row(
            "flip_analysis",
            "Flip analysis",
            flip_on,
            deal.flip_analysis,
            "on by default for a resale exit (SPEC §8.1)",
        ),
        _toggle_row(
            "rental_analysis",
            "Rental analysis",
            rental_on,
            deal.rental_analysis,
            "on by default for a hold exit, or when a rent is entered (SPEC §8.1)",
        ),
        _row(
            "verified_credit_score",
            "Verified credit score",
            None,
            note="no credit adapter yet; the self-reported tranche stands (SPEC §7.1)",
        ),
        _court_row(deal, adapters),
        _row(
            "asset_type",
            "Asset type",
            deal.asset_type,
            note="with the term it infers the exit (SPEC §3)",
        ),
        _row(
            "stated_exit",
            "Stated exit",
            deal.stated_exit,
            note="blank leaves the exit to the SPEC §3 inference",
        ),
    ]
    return UnderwriteReadiness(rows=rows, intake_missing=intake_gaps(deal))
