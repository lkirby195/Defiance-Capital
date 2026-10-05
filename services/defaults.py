"""The SPEC §8.1 inputs a deal is populated with when nobody has entered them.  # SPEC §8.1, §8.2

Twelve inputs have a defensible stand-in and every deal gets it at intake: the interest rate
(``interest.default_annual_rate``), the four fees (``fees.*_default_*``), the five analysis
assumptions (the broker's selling percentage, the rental's expense ratio and takeout rate,
the take-back's legal costs and lost-interest months - SPEC §8.4-§8.6, each off its config
section), the closing date (``closing.default_lead_days``) and - on the two split products -
the loan split, by the SPEC §8.2 formula:

    rehab portion   = min(rehab_adj, loan amount)     rehab_adj = rehab x (1 + contingency)
    advance         = loan amount - rehab portion
    closing date    = the last day of the month that is default_lead_days after the deal came in

A ``NO_DRAW`` or ``WHOLETAIL`` loan is one advance and carries no split; its advance *is*
the loan amount and nothing is stored for it (SPEC §8.2).

The value is stored on the deal, so what the engine runs on is what the page shows, and
``deals.defaulted_fields`` names which of them are stand-ins rather than a person's choice.
That list is the whole of the DEFAULT / TEAM distinction the readiness checklist (SPEC
§9.2) and the Inputs block's "default" tag read. **A value equal to the default is the
default**: a person who leaves a box holding the number it was pre-filled with has not
chosen it, and the box says so; a person who wants that very number on purpose gets the
same page and the same price, because the number is the same.

The closing date's default is anchored to the day the deal was first submitted
(``deals.created_at``), not to the latest edit: a default that moved every time somebody
saved the page would relabel an untouched date as a choice. A row never stored - the CLI's
fixture deal - has no such day and is anchored to today; every fixture carries its own date,
so nothing the CLI prints depends on the clock.

``populate`` is idempotent and is the only writer of the list. It runs at intake on every
channel, after every Inputs save and intake edit, and when an existing deal is opened -
so a deal stored before this existed is populated the first time somebody looks at it, with
a team value already on it left exactly as it was.

Nothing here commits; the caller owns the transaction, as every service does.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from engine.sizing import rehab_adjusted
from schema.dates import month_end
from schema.models import SPLIT_PRODUCTS, AuditAction, Product
from services.audit import DEALS, jsonable, record_audit
from services.errors import DealNotFound

# The five flat config defaults of the §8.1 economics, by the ``deals`` column each stands
# in for.
ECONOMICS: tuple[str, ...] = (
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
)
# The five flat config defaults of the §8.4-§8.6 analysis assumptions, the same way.
ASSUMPTIONS: tuple[str, ...] = (
    "broker_selling_pct",
    "rental_expenses_pct_of_rent",
    "rental_takeout_rate",
    "take_back_legal_costs_usd",
    "take_back_lost_interest_months",
)
# Every flat config default: a column, a number in the yaml, nothing else on the deal read.
FLAT: tuple[str, ...] = (*ECONOMICS, *ASSUMPTIONS)
# The two halves of the loan split (SPEC §8.2), defaulted together or not at all.
SPLIT: tuple[str, str] = ("loan_purchase_portion", "loan_rehab_portion")
# The closing date (SPEC §8.1): a month end counted from the day the deal came in.
CLOSING = "closing_date"
# Everything ``defaulted_fields`` may name.
DEFAULTABLE: tuple[str, ...] = (*FLAT, *SPLIT, CLOSING)

# What a default is: a number for the economics, the assumptions and the split (the
# lost-interest months are a whole number), a date for the closing.
DefaultValue = Decimal | int | date

CENTS = Decimal("0.01")
ZERO = Decimal(0)


def economics_defaults(config: Config) -> dict[str, Decimal]:
    """The five §8.1 config defaults, keyed by the column each stands in for.  # SPEC §8.1"""
    fees = config.fees
    return {
        "interest_rate": config.interest.default_annual_rate,
        "contingency_pct": fees.contingency_default_pct,
        "closing_costs_usd": fees.closing_costs_default_usd,
        "holding_costs_pct_of_cost": fees.holding_costs_default_pct_of_cost,
        "origination_fee_pct": fees.origination_default_pct,
    }


def assumption_defaults(config: Config) -> dict[str, Decimal | int]:
    """The five analysis assumptions' config values, by column.  # SPEC §8.4, §8.5, §8.6"""
    return {
        "broker_selling_pct": config.fees.broker_selling_pct,
        "rental_expenses_pct_of_rent": config.rental.expenses_pct_of_rent,
        "rental_takeout_rate": config.rental.takeout_rate,
        "take_back_legal_costs_usd": config.take_back.legal_costs_usd,
        "take_back_lost_interest_months": config.take_back.lost_interest_months,
    }


def flat_defaults(config: Config) -> dict[str, Decimal | int]:
    """Every flat config default - the economics and the assumptions - by column."""
    return {**economics_defaults(config), **assumption_defaults(config)}


def default_closing_date(submitted_on: date, config: Config) -> date:
    """The last day of the month ``closing.default_lead_days`` after arrival.  # SPEC §8.1"""
    return month_end(submitted_on + timedelta(days=config.closing.default_lead_days))


def submitted_on(deal: Deal) -> date:
    """The day the deal came in, which the closing default counts from.  # SPEC §8.1

    ``created_at`` on a stored row; today on a row that was never stored (the CLI's fixture
    deal), which is the one place a default here reads the clock.
    """
    created = deal.created_at
    return created.date() if created is not None else date.today()


def default_loan_split(
    product: Product | None,
    loan_requested: Decimal | None,
    rehab_costs: Decimal | None,
    contingency_pct: Decimal | None,
    config: Config,
) -> tuple[Decimal, Decimal] | None:
    """The split a split product gets when nobody has divided the loan.  # SPEC §8.2

    ``(advance, rehab portion)``: the rehab portion is the contingency-adjusted rehab budget
    capped at the loan amount, and the advance is what is left. None on a single-note
    product, which has no split, and until the loan amount and the rehab costs are both
    known, because a formula on a blank is not a default. The contingency in force is the
    deal's own, else the config's. Rounded to cents, because the columns hold cents and a
    split that does not add up to the loan amount to the cent is refused (SPEC §8.2).
    """
    if product not in SPLIT_PRODUCTS or loan_requested is None or rehab_costs is None:
        return None
    pct = contingency_pct if contingency_pct is not None else config.fees.contingency_default_pct
    rehab_adj = rehab_adjusted(rehab_costs, pct).quantize(CENTS, rounding=ROUND_HALF_UP)
    rehab_portion = max(ZERO, min(rehab_adj, loan_requested))
    return loan_requested - rehab_portion, rehab_portion


def deal_loan_split_default(deal: Deal, config: Config) -> tuple[Decimal, Decimal] | None:
    """``default_loan_split`` on what a deal row holds, contingency in force included."""
    return default_loan_split(
        deal.product, deal.loan_requested, deal.rehab_costs, deal.contingency_pct, config
    )


def defaults_for(deal: Deal, config: Config) -> dict[str, DefaultValue]:
    """Every defaultable column and the stand-in it would get on this deal.

    The split is only here on a split product with its loan amount and rehab known; the
    five economics, the five assumptions and the closing date are always here. Read by the
    page to pre-fill and tag the boxes, by the readiness checklist to say DEFAULT, by the
    assembly to price a row nobody has populated yet, and by ``populate`` to decide what to
    write.
    """
    out: dict[str, DefaultValue] = dict(flat_defaults(config))
    split = deal_loan_split_default(deal, config)
    if split is not None:
        out[SPLIT[0]], out[SPLIT[1]] = split
    out[CLOSING] = default_closing_date(submitted_on(deal), config)
    return out


def populate(deal: Deal, config: Config | None = None) -> dict[str, Any]:
    """Fill every blank defaultable column with its stand-in; relabel the rest.  # SPEC §8.1

    Returns the columns it set, as a stored value each - empty when nothing was blank, which
    is every call but the first on a given deal. ``defaulted_fields`` is rewritten whole on
    every call: a column holding its default is on it whether this call wrote the value or
    somebody left the box alone, and a column holding anything else is not. Pure on the row:
    no session, no audit row, no commit.

    The split is filled only when both halves are blank. One half without the other is a
    state the model and the database both refuse, so it never reaches here.
    """
    settings = config if config is not None else get_config()
    wanted = defaults_for(deal, settings)
    written: dict[str, Any] = {}
    for column in (*FLAT, CLOSING):
        if getattr(deal, column) is None:
            setattr(deal, column, wanted[column])
            written[column] = jsonable(wanted[column])
    if (
        SPLIT[0] in wanted
        and deal.loan_purchase_portion is None
        and deal.loan_rehab_portion is None
    ):
        for column in SPLIT:
            setattr(deal, column, wanted[column])
            written[column] = jsonable(wanted[column])
    marked = [
        column
        for column in DEFAULTABLE
        if column in wanted and getattr(deal, column) == wanted[column]
    ]
    if list(deal.defaulted_fields or []) != marked:
        deal.defaulted_fields = marked
    return written


def is_defaulted(deal: Deal, column: str) -> bool:
    """Whether ``column`` holds a stand-in rather than a number somebody chose.

    True also for a column still NULL on a deal nobody has populated yet: the engine reads
    the default in its place, which is what DEFAULT means (SPEC §9.2).
    """
    # ``or []``: a row built off a fixture and never flushed has not been given the column's
    # default yet (``db/repository.transient_deal``).
    return column in (deal.defaulted_fields or []) or getattr(deal, column) is None


def apply_defaults(
    session: Session, deal_id: UUID, *, actor: str, config: Config | None = None
) -> dict[str, Any]:
    """Populate a stored deal, with an audit row when anything was written.  # SPEC §8.1

    The idempotent service call: a deal already populated writes nothing and records
    nothing. Flushes; no commit.
    """
    # Looked up here rather than through ``services.runner.load_deal``: the runner's
    # assembly reads this module, and a service a deal is populated by cannot import the
    # one that prices it without the two chasing each other at import time.
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise DealNotFound(deal_id)
    written = populate(deal, config)
    if written:
        session.flush()
        record_audit(
            session,
            actor=actor,
            action=AuditAction.DEFAULTS_POPULATED,
            table_name=DEALS,
            row_id=deal.id,
            after=written,
        )
    return written
