"""What the ledger has to run on, and where each of it came from.  # SPEC §8.1, §9.2

The queue's Run Analysis button takes no form: every SPEC §8.1 input lives on the deal by
the time somebody presses it (``api/routes/queue.py``). That is convenient and it used to be
opaque - the button either worked or came back with a refusal naming values the page had
never mentioned. This module is the page's answer to "what is it going to run on", one row
per §8.1 input, each with the value in force and where it came from:

    ADAPTER   an enrichment adapter or a paid pull produced it (SPEC §6)
    TEAM      somebody entered it by hand, on the intake form or the override block
    BORROWER  the borrower's own estimate, typed on the public form (SPEC §4.2); a team
              entry replaces it and the borrower's figure stays on the deal
    DEFAULT   nobody entered it and config has a stand-in (SPEC §8.1)
    MISSING   nobody entered it and there is no stand-in

``required`` marks the rows the run cannot proceed without, and ``missing`` is exactly those
of them that are MISSING. It is derived from the same rules the assembly raises
``DealNotReady`` on (``services/assemble.py``) rather than a second list beside them, so the
banner on the page and the refusal behind it can never name different things. A DEFAULT is
present: the rate, the closing date and the loan split are required rows, and a deal nobody
has entered them on runs on the config rate, the month end two weeks after it came in and
the SPEC §8.2 formula split (``services/defaults.py``). What turns the ledger off is the one
input nothing stands in for - the term.

An optional row that is MISSING is not a problem to fix before running - it is a thing the
ledger will do without, and the note says what that costs: no DSCR at all without a
monthly rent, no LTV and no flip without an estimated sale price, the self-reported tranche
without a verified score. The five §8.4-§8.6 analysis assumptions are rows too, DEFAULT
until the team types over them on the Underwriting Assumptions panel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from config.config import Config, get_config
from db.models import Deal
from schema.dates import Term, describe, payoff_date_for
from schema.labels import enum_label
from schema.models import (
    SPLIT_PRODUCTS,
    TWELVE_PLUS_SEED_MONTHS,
    CourtRecordsStatus,
    Product,
    TermBucket,
    months_for_bucket,
)
from services.assemble import flip_is_on, intake_gaps, rental_is_on
from services.defaults import defaults_for, is_defaulted
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues
from services.requests import UnderwriteRequest

ZERO = Decimal(0)

# How the page renders a row's value. Not every §8.1 input is money any more: the rate, the
# contingency, the holding costs and the origination fee are percentages, the two dates are
# dates, and a percentage rendered through the money filter reads as two cents. ``label`` is
# a stored enum shown as the words a person reads (``schema/labels.py``).
RowFormat = Literal["money", "pct", "plain", "label"]


class InputSource(StrEnum):
    """Where the value an underwrite would run on came from.  # SPEC §6, §8.1"""

    ADAPTER = "ADAPTER"
    TEAM = "TEAM"
    BORROWER = "BORROWER"
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
    adapter: Any = None,
    team: Any = None,
    borrower: Any = None,
    default: Any = None,
    required: bool = False,
    note: str = "",
) -> InputRow:
    """One input resolved adapter over team over borrower over config default.

    # SPEC §4.2, §6.1, §8.1. The same order ``services/assemble.py`` runs on.
    """
    if adapter is not None:
        value, source = adapter, InputSource.ADAPTER
    elif team is not None:
        value, source = team, InputSource.TEAM
    elif borrower is not None:
        value, source = borrower, InputSource.BORROWER
    elif default is not None:
        value, source = default, InputSource.DEFAULT
    else:
        value, source = None, InputSource.MISSING
    return InputRow(
        key=key, label=label, value=value, fmt=fmt, source=source, required=required, note=note
    )


def deal_term(deal: Deal) -> Term | None:
    """The term the deal carries: whole months, then the stub days after them.  # SPEC §8.1"""
    if deal.term_months is None:
        return None
    return Term(deal.term_months, deal.term_stub_days or 0)


def _term_row(deal: Deal) -> InputRow:
    """The term on the deal, and where it came from.  # SPEC §8.1

    DEFAULT rather than TEAM while the value is still the one the bucket seeded: nobody chose
    9 months on a 9-month bucket, the bucket did. A term that differs from the bucket, or one
    with a stub, which no bucket names, is a person's own.

    ``12_PLUS`` seeds 12 on every channel (SPEC §4.1): the floor of what the borrower asked
    for. So a 12-month term on a 12+ bucket is the seed, not a choice, and is reported as
    DEFAULT with a note saying the borrower asked for more - the team sets the real number.
    """
    term = deal_term(deal)
    named = months_for_bucket(deal.term_bucket)
    seeded = named is not None and term == Term(named, 0)
    if term is None:
        source = InputSource.MISSING
    elif seeded:
        source = InputSource.DEFAULT
    else:
        source = InputSource.TEAM
    bucket = deal.term_bucket
    if bucket is None:
        note = "no term bucket on the deal; the team's own number is all there is"
    elif bucket is TermBucket.M12_PLUS and seeded:
        note = (
            f"seeded at {TWELVE_PLUS_SEED_MONTHS} months by the 12+ bucket; the borrower asked "
            "for more than a year, so set the real term"
        )
    elif seeded:
        note = f"seeded by the {bucket.value}-month bucket"
    else:
        note = f"the team's own; the {bucket.value}-month bucket was the ask"
    if term is not None and term.has_stub:
        note = f"{note}; the last period is a {term.stub_days}-day stub (SPEC 8.3)"
    return _row(
        "deal.term_months",
        "Term",
        None if term is None else describe(term),
        source=source,
        required=True,
        note=note,
    )


def _payoff_row(deal: Deal) -> InputRow:
    """The payoff date, derived from the closing date and the term.  # SPEC §8.1

    Never required and never a reason the button is off: it is not a column, it is the last
    day of the month that is the closing month plus the term (plus any stub), and the two
    rows above turn the button off. It is on the checklist because it is the date the
    ledger's last row carries, and a person wants to see it before they press anything.
    """
    term = deal_term(deal)
    payoff = None
    if deal.closing_date is not None and term is not None and term.is_positive:
        payoff = payoff_date_for(deal.closing_date, term.full_months, term.stub_days)
    return _row(
        "payoff_date",
        "Payoff date",
        payoff,
        source=InputSource.DEFAULT if payoff is not None else InputSource.MISSING,
        note=(
            "derived: the last day of the month that is the closing month plus the term (SPEC §8.1)"
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
    return _row(
        "court_records",
        "Court and filing search",
        value,
        fmt="label",
        source=source,
        note="" if source is not InputSource.MISSING else "not checked is not clean (SPEC §7.2)",
    )


def _defaulted_row(
    deal: Deal,
    column: str,
    key: str,
    label: str,
    defaults: Mapping[str, object],
    *,
    fmt: RowFormat = "money",
    required: bool = False,
    note: str = "",
) -> InputRow:
    """One §8.1 economic with a stand-in: the deal's own number, else the default.  # SPEC §8.1

    DEFAULT when the value on the deal is the one config or the §8.2 formula put there
    (``deals.defaulted_fields``), or when nobody has populated the deal yet and the engine
    will read the default in its place; TEAM when a person typed over it.
    """
    value = getattr(deal, column)
    if is_defaulted(deal, column):
        return _resolved_row(
            key,
            label,
            fmt=fmt,
            default=value if value is not None else defaults.get(column),
            required=required,
            note=note,
        )
    return _resolved_row(key, label, fmt=fmt, team=value, required=required, note=note)


def _split_rows(deal: Deal, defaults: Mapping[str, object]) -> list[InputRow]:
    """The two halves of a split loan; nothing at all on a product that has no split.

    Required, and present on every split product whose loan amount and rehab are known: a
    deal nobody has divided carries the SPEC §8.2 formula split as its DEFAULT.
    """
    if deal.product not in SPLIT_PRODUCTS:
        return []
    names = (
        ("Principal Note", "Tranche A")
        if deal.product is Product.SPLIT_PRINCIPAL
        else ("Advance at closing", "Rehab holdback")
    )
    note = (
        f"a {enum_label(deal.product)} loan is advanced in two parts (SPEC §8.2); the default "
        "rehab portion is the contingency-adjusted rehab budget capped at the loan amount, and "
        "the advance is the rest"
    )
    return [
        _defaulted_row(
            deal,
            "loan_purchase_portion",
            "deal.loan_purchase_portion",
            names[0],
            defaults,
            required=True,
            note=note,
        ),
        _defaulted_row(
            deal,
            "loan_rehab_portion",
            "deal.loan_rehab_portion",
            names[1],
            defaults,
            required=True,
            note=note,
        ),
    ]


def holding_costs_dollars(deal: Deal, default_pct: Decimal) -> Decimal | None:
    """What the holding-cost percentage in force comes to in dollars.  # SPEC §8.1

    None until the price and the rehab are both known: a percentage of nothing is not a
    dollar figure, it is zero pretending to be one.
    """
    if deal.purchase_price is None or deal.rehab_costs is None:
        return None
    cost = deal.purchase_price + deal.rehab_costs
    if cost <= ZERO:
        return None
    pct = deal.holding_costs_pct_of_cost
    return cost * (pct if pct is not None else default_pct)


def holding_costs_note(deal: Deal, default_pct: Decimal) -> str:
    """The row's note: what the percentage comes to in dollars, and the default behind it."""
    dollars = holding_costs_dollars(deal, default_pct)
    amount = (
        "the price and the rehab are not both known yet, so there is no dollar figure"
        if dollars is None
        else f"${dollars:,.2f} over the whole hold"
    )
    return f"{amount}; config default {default_pct:.2%} of price plus rehab"


def _toggle_row(key: str, label: str, on: bool, chosen: bool | None, default_note: str) -> InputRow:
    """One analysis toggle: the state it is in, and whether a person or the default set it."""
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
    defaults = defaults_for(deal, settings)
    empty = UnderwriteRequest()
    flip_on = flip_is_on(deal, empty, adapters)
    rental_on = rental_is_on(deal, empty, adapters)
    rows = [
        _defaulted_row(
            deal,
            "closing_date",
            "deal.closing_date",
            "Closing date",
            defaults,
            fmt="plain",
            required=True,
            note=(
                "any date; the ledger's month 0 is the last day of its month (SPEC §8.3). "
                f"Default: the last day of the month {settings.closing.default_lead_days} days "
                "after the deal came in (SPEC §8.1)"
            ),
        ),
        _term_row(deal),
        _payoff_row(deal),
        _defaulted_row(
            deal,
            "interest_rate",
            "deal.interest_rate",
            "Interest rate",
            defaults,
            fmt="pct",
            required=True,
            note=(
                f"annual; config default {settings.interest.default_annual_rate:.2%} until the "
                "team enters the deal's own rate (SPEC §8.1)"
            ),
        ),
        *_split_rows(deal, defaults),
        _resolved_row(
            "estimated_sale_price",
            "Estimated sale price",
            adapter=adapters.estimated_sale_price,
            team=deal.estimated_sale_price_team,
            borrower=deal.estimated_sale_price_borrower,
            note=(
                "the Flip analysis sells at it and LTV is computed on it; without one the "
                "flip is not evaluated and LTV is not available (SPEC §7.4, §8.4)"
                if flip_on
                else "LTV is computed on it; without one LTV is not available (SPEC §7.4)"
            ),
        ),
        _resolved_row(
            "monthly_rent",
            "Monthly rent",
            adapter=adapters.monthly_rent,
            team=deal.monthly_rent,
            borrower=deal.monthly_rent_borrower,
            note="without it the Rental and Take-Back analyses are not evaluated (SPEC §8.5)",
        ),
        _defaulted_row(
            deal,
            "contingency_pct",
            "contingency_pct",
            "Contingency",
            defaults,
            fmt="pct",
            note=f"config default: {fees.contingency_default_pct:.2%} of the rehab costs",
        ),
        _defaulted_row(
            deal,
            "closing_costs_usd",
            "closing_costs_usd",
            "Closing costs",
            defaults,
            note="the lender's own, inside the LTC denominator (SPEC §8.2)",
        ),
        _defaulted_row(
            deal,
            "holding_costs_pct_of_cost",
            "holding_costs_pct_of_cost",
            "Holding costs (% of price + rehab)",
            defaults,
            fmt="pct",
            note=holding_costs_note(deal, fees.holding_costs_default_pct_of_cost),
        ),
        _defaulted_row(
            deal,
            "origination_fee_pct",
            "origination_fee_pct",
            "Origination fee",
            defaults,
            fmt="pct",
            note=(
                f"config default: {fees.origination_default_pct:.2%}, half at close and half "
                "at payoff"
            ),
        ),
        _defaulted_row(
            deal,
            "broker_selling_pct",
            "broker_selling_pct",
            "Broker selling costs",
            defaults,
            fmt="pct",
            note=(
                f"of the sale price, the flip's cost of selling (SPEC §8.4); config "
                f"{fees.broker_selling_pct:.2%}"
            ),
        ),
        _defaulted_row(
            deal,
            "rental_expenses_pct_of_rent",
            "rental_expenses_pct_of_rent",
            "Rental expenses",
            defaults,
            fmt="pct",
            note=(
                f"operating expenses as a share of the rent (SPEC §8.5); config "
                f"{settings.rental.expenses_pct_of_rent:.2%}"
            ),
        ),
        _defaulted_row(
            deal,
            "rental_takeout_rate",
            "rental_takeout_rate",
            "Rental takeout rate",
            defaults,
            fmt="pct",
            note=(
                f"the rate a takeout lender amortizes at over "
                f"{settings.rental.amortization_years} years (SPEC §8.5); config "
                f"{settings.rental.takeout_rate:.2%}"
            ),
        ),
        _defaulted_row(
            deal,
            "take_back_legal_costs_usd",
            "take_back_legal_costs_usd",
            "Take-back legal costs",
            defaults,
            note=(
                f"the legal bill in the take-back's total cost (SPEC §8.6); config "
                f"${settings.take_back.legal_costs_usd:,.2f}"
            ),
        ),
        _defaulted_row(
            deal,
            "take_back_lost_interest_months",
            "take_back_lost_interest_months",
            "Take-back lost interest (months)",
            defaults,
            fmt="plain",
            note=(
                "months of interest GLENWOOD stops collecting while it takes the property "
                f"back (SPEC §8.6); config {settings.take_back.lost_interest_months}"
            ),
        ),
        _toggle_row(
            "flip_analysis",
            "Flip analysis",
            flip_on,
            deal.flip_analysis,
            "on by default when there is a sale price to sell at (SPEC §8.1)",
        ),
        _toggle_row(
            "rental_analysis",
            "Rental analysis",
            rental_on,
            deal.rental_analysis,
            "on by default when there is a rent to carry a loan with (SPEC §8.1)",
        ),
        _row(
            "verified_credit_score",
            "Verified credit score",
            None,
            note="no credit adapter yet; the self-reported tranche stands (SPEC §7.1)",
        ),
        _court_row(deal, adapters),
    ]
    return UnderwriteReadiness(rows=rows, intake_missing=intake_gaps(deal))
