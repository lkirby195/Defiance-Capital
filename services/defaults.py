"""The SPEC §8.1 economics a deal is populated with when nobody has entered them.  # SPEC §8.1, §8.2

Six inputs have a defensible stand-in and every deal gets it at intake: the interest rate
(``interest.default_annual_rate``), the four fees (``fees.*_default_*``), and - on the two
split products - the loan split, by the SPEC §8.2 formula:

    rehab portion   = min(rehab_adj, loan amount)     rehab_adj = rehab x (1 + contingency)
    advance         = loan amount - rehab portion

A ``NO_DRAW`` or ``WHOLETAIL`` loan is one advance and carries no split; its advance *is*
the loan amount and nothing is stored for it (SPEC §8.2).

The value is stored on the deal, so what the engine runs on is what the page shows, and
``deals.defaulted_fields`` names which of them are stand-ins rather than a person's choice.
That list is the whole of the DEFAULT / TEAM distinction the readiness checklist (SPEC
§9.2) and the override block's "default" tag read. **A value equal to the default is the
default**: a person who leaves a box holding the number it was pre-filled with has not
chosen it, and the box says so; a person who wants that very number on purpose gets the
same page and the same price, because the number is the same.

``populate`` is idempotent and is the only writer of the list. It runs at intake on every
channel, after every override save and intake edit, and when an existing deal is opened -
so a deal stored before this existed is populated the first time somebody looks at it, with
a team value already on it left exactly as it was.

Nothing here commits; the caller owns the transaction, as every service does.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from engine.sizing import rehab_adjusted
from schema.models import SPLIT_PRODUCTS, AuditAction, Product
from services.audit import DEALS, jsonable, record_audit
from services.errors import DealNotFound

# The five flat config defaults, by the ``deals`` column each stands in for.
ECONOMICS: tuple[str, ...] = (
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
)
# The two halves of the loan split (SPEC §8.2), defaulted together or not at all.
SPLIT: tuple[str, str] = ("loan_purchase_portion", "loan_rehab_portion")
# Everything ``defaulted_fields`` may name.
DEFAULTABLE: tuple[str, ...] = (*ECONOMICS, *SPLIT)

CENTS = Decimal("0.01")
ZERO = Decimal(0)


def economics_defaults(config: Config) -> dict[str, Decimal]:
    """The five config defaults, keyed by the column each stands in for.  # SPEC §8.1"""
    fees = config.fees
    return {
        "interest_rate": config.interest.default_annual_rate,
        "contingency_pct": fees.contingency_default_pct,
        "closing_costs_usd": fees.closing_costs_default_usd,
        "holding_costs_pct_of_cost": fees.holding_costs_default_pct_of_cost,
        "origination_fee_pct": fees.origination_default_pct,
    }


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


def defaults_for(deal: Deal, config: Config) -> dict[str, Decimal]:
    """Every defaultable column and the stand-in it would get on this deal.

    The split is only here on a split product with its loan amount and rehab known; the
    five economics are always here. Read by the page to pre-fill and tag the boxes, and by
    ``populate`` to decide what to write.
    """
    out: dict[str, Decimal] = economics_defaults(config)
    split = deal_loan_split_default(deal, config)
    if split is not None:
        out[SPLIT[0]], out[SPLIT[1]] = split
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
    for column in ECONOMICS:
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
    the config default in its place, which is what DEFAULT means (SPEC §9.2).
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
