"""The /defaults page: every value in ``config/glenwood.yaml``, grouped, read-only.  # SPEC §10

A person pricing a deal wants to know what stands in when nobody typed a number - the rate,
the fees, the assumptions - and what the screen tests against, without reading yaml. This
module turns the validated ``Config`` into labelled rows, one per yaml leaf, keyed by the
leaf's dotted path (``fees.origination_default_pct``). The page prints that path beside every
value, so a person who wants a number changed can name exactly the one they mean.

Each row is labelled in the team's words (v0.25, ``LABELS``): "Origination", not "Origination
default pct"; "Sales cost", not "Broker selling pct"; "OK foreclosure months", not a bare
"OK". A default for a deal's own input reads exactly as the deal page captions that input
(v0.27): ``INPUT_COLUMNS`` names its ``deals`` column and ``schema/labels.INPUT_LABELS`` has
the word. The unit is the value's job - ``2.00%``, ``$1,000.00``, ``1.20x`` - so no label carries
"pct" or "usd", and every percentage, dollar figure and ratio prints to two decimals. Counts
(months, years, FICO points) stay whole. A key ``LABELS`` does not name still gets a label,
from its last part with those unit words dropped.

Every leaf belongs to one group by the longest prefix in ``GROUPS``. A leaf no prefix claims
lands in **Other** rather than off the page, and ``tests/test_defaults_page.py`` fails on
both a yaml leaf the page does not show and a leaf that fell into Other - so a key added to
the yaml is a key somebody has to place here.

The leverage caps are 160 leaves (product x tranche x tier x two ratios); they render as one
small table per product rather than 160 rows, each cell still carrying its two dotted keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from config.config import PLACEHOLDERS, Config, flatten
from schema.labels import enum_label, input_label
from schema.models import ExperienceTier, Product, Severity, Tranche

# (group heading, dotted prefixes it claims), in the order the page shows them.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Economics",
        (
            "fees.origination_default_pct",
            "fees.contingency_default_pct",
            "fees.closing_costs_default_usd",
            "fees.holding_costs_default_pct_of_cost",
            "interest.default_annual_rate",
        ),
    ),
    ("Flip", ("fees.broker_selling_pct",)),
    ("Rental", ("rental",)),
    ("Take-Back", ("take_back",)),
    ("Pricing", ("pricing",)),
    ("Sensitivity", ("sensitivity",)),
    (
        "Screen caps and thresholds",
        ("credit", "screen", "flags", "experience", "states", "web_intake", "leverage_caps"),
    ),
    ("Dates", ("closing", "interest.day_count_basis", "draws.listing_months")),
    ("Market data", ("rentcast",)),
    ("Sign-in", ("sign_in",)),
)
OTHER = "Other"

# What a leaf holds, by the end of its key, for formatting. Anything else prints as is.
_PERCENT_ENDINGS = (
    "_pct",
    "_pct_steps",
    "_pct_of_cost",
    "_pct_of_rent",
    "rate",
    "rate_min",
    "rate_max",
    "rate_step",
    "target_lender_irr",
    "tolerance_band",
    ".ltc",
    ".ltv",
    "takeout_ltv",
    "borrower_rate_cushion",
    "borrower_rate_rounding",
    "borrower_coc_target",
)
_MONEY_ENDINGS = ("_usd", "loan_amount_rounding")
_RATIO_ENDINGS = ("target_dscr",)


# The config keys that are the default for a deal's own input, by the ``deals`` column the
# input lives in: each row reads as the deal page reads the input (``INPUT_LABELS``, v0.27).
INPUT_COLUMNS: dict[str, str] = {
    "fees.origination_default_pct": "origination_fee_pct",
    "fees.contingency_default_pct": "contingency_pct",
    "fees.closing_costs_default_usd": "closing_costs_usd",
    "fees.holding_costs_default_pct_of_cost": "holding_costs_pct_of_cost",
    "interest.default_annual_rate": "interest_rate",
    "fees.broker_selling_pct": "broker_selling_pct",
}

# The team's word for each leaf (v0.25), by its dotted key. The flag severities and the
# per-state entries are worded by ``label_for`` from their last part.
LABELS: dict[str, str] = {
    **{key: input_label(column) for key, column in INPUT_COLUMNS.items()},
    "rental.expenses_pct_of_rent": "Operating expenses (of rent)",
    "rental.takeout_ltv": "Takeout LTV",
    "rental.takeout_rate": "Takeout rate",
    "rental.amortization_years": "Amortization (years)",
    "rental.target_dscr": "Target DSCR",
    "take_back.lost_interest_months": "Lost interest (months)",
    "take_back.legal_costs_usd": "Legal costs",
    "take_back.hold_months": "Hold periods (months)",
    "pricing.target_lender_irr": "Target lender IRR",
    "pricing.max_quoted_rate": "Max quoted rate",
    "pricing.loan_amount_pct_steps": "Loan amount steps",
    "pricing.loan_amount_rounding": "Loan amount rounding",
    "pricing.borrower_coc_target": "Borrower cash-on-cash target",
    "pricing.borrower_rate_cushion": "Borrower rate cushion",
    "pricing.borrower_rate_rounding": "Borrower rate rounding",
    "sensitivity.loan_step_usd": "Loan amount step",
    "sensitivity.loan_steps": "Loan amount steps (each way)",
    "sensitivity.rate_min": "Rate from",
    "sensitivity.rate_max": "Rate to",
    "sensitivity.rate_step": "Rate step",
    "credit.floor_tranche": "Credit floor tranche",
    "screen.tolerance_band": "Leverage tolerance band",
    "flags.lookbacks.bankruptcy_years": "Bankruptcy lookback (years)",
    "flags.lookbacks.satisfied_judgment_years": "Satisfied judgment lookback (years)",
    "flags.thresholds.unsatisfied_judgments_aggregate_usd": "Unsatisfied judgments (aggregate)",
    "flags.thresholds.active_civil_litigation_usd": "Active civil litigation",
    "experience.repeat_borrower_min_tier": "Repeat borrower minimum tier",
    "states.served": "States served",
    "web_intake.submissions_per_hour_per_ip": "Web submissions per hour per address",
    "web_intake.thanks_token_hours": "Thank-you link life (hours)",
    "draws.listing_months": "Listing period (months)",
    "interest.day_count_basis": "Day count basis",
    "closing.default_lead_days": "Closing lead (days)",
    "rentcast.cache_days": "RentCast cache (days)",
    "rentcast.monthly_request_budget": "RentCast monthly request budget",
    "sign_in.lockout_failures": "Failed sign-ins before lockout",
    "sign_in.lockout_minutes": "Lockout window (minutes)",
}

# A leaf under one of these, keyed by a state or a tranche, reads "<that> <words>".
_PER_ENTRY: dict[str, str] = {
    "take_back.foreclosure_months": "foreclosure months",
    "states.court_adapter": "court records source",
    "credit.tranche_cutoffs": "minimum FICO",
}
# Unit words a fallback label drops: the value says them.
_UNIT_WORDS = frozenset({"default", "pct", "usd", "of", "cost", "rent"})


@dataclass(frozen=True)
class Leaf:
    """One yaml value: its dotted key, a label, and the value as a person reads it."""

    key: str
    label: str
    value: str


@dataclass
class Group:
    heading: str
    leaves: list[Leaf] = field(default_factory=list)


@dataclass(frozen=True)
class CapsTable:
    """One product's caps: a row per tranche, a column per tier, LTC and LTV in each cell."""

    product: str
    tiers: list[str]
    rows: list[tuple[str, list[tuple[Leaf, Leaf]]]]


def group_of(key: str) -> str:
    """The group whose longest prefix matches ``key``; ``OTHER`` when none does."""
    best, length = OTHER, -1
    for heading, prefixes in GROUPS:
        for prefix in prefixes:
            if (key == prefix or key.startswith(prefix + ".")) and len(prefix) > length:
                best, length = heading, len(prefix)
    return best


def _one(key: str, value: Any) -> str:
    if isinstance(value, Severity):
        return enum_label(value)  # HARD -> Hard, as the deal page's chips read
    if isinstance(value, Enum):
        return str(value.value)  # a state or a grid code (OK, T4, E2) reads as itself
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        if key.endswith(_PERCENT_ENDINGS):
            return f"{value * 100:.2f}%"
        if key.endswith(_MONEY_ENDINGS):
            sign = "-" if value < 0 else ""
            return f"{sign}${abs(value):,.2f}"
        if key.endswith(_RATIO_ENDINGS):
            return f"{value:.2f}x"
    return str(value)


def display(key: str, value: Any) -> str:
    """A leaf's value as the page prints it: ``2.00%``, ``$1,000.00``, ``1.20x``, ``OK, CO``."""
    if isinstance(value, list):
        return ", ".join(_one(key, item) for item in value) or "(none)"
    return _one(key, value)


def label_for(key: str) -> str:
    """A leaf's label in the team's words: ``LABELS`` when it names the key; a per-state or
    per-tranche entry as ``OK foreclosure months``; a flag code sentence-cased; else the
    last part of the key without its unit words (``closing_costs_default_usd`` -> ``Closing
    costs``)."""
    if key in LABELS:
        return LABELS[key]
    parent, last = key.rsplit(".", 1) if "." in key else ("", key)
    if parent in _PER_ENTRY:
        return f"{'Other states' if last == 'OTHER' else last} {_PER_ENTRY[parent]}"
    if last.isupper():
        words = last.replace("_", " ").lower()
        return words[:1].upper() + words[1:]
    kept = [word for word in last.split("_") if word not in _UNIT_WORDS] or last.split("_")
    words = " ".join(kept)
    return words[:1].upper() + words[1:]


def config_leaves(config: Config) -> dict[str, Any]:
    """Every leaf of the validated config, by the dotted key the yaml spells it with."""
    return flatten(config.model_dump(mode="python"))


def groups(config: Config) -> list[Group]:
    """Every leaf but the caps grid, in its group, groups in ``GROUPS`` order."""
    by_heading = {heading: Group(heading) for heading, _ in GROUPS}
    by_heading[OTHER] = Group(OTHER)
    for key, value in config_leaves(config).items():
        if key.startswith("leverage_caps.") or key == PLACEHOLDERS:
            continue
        by_heading[group_of(key)].leaves.append(Leaf(key, label_for(key), display(key, value)))
    return [group for group in by_heading.values() if group.leaves or group.heading != OTHER]


def placeholder_line(config: Config) -> str:
    """The line under the title: how many values ``placeholders`` lists.  # SPEC §10 (v0.27)"""
    count = len(config.placeholders)
    if count == 0:
        return "No values are placeholders."
    if count == 1:
        return "1 value is a placeholder until Defiance confirms it."
    return f"{count} values are placeholders until Defiance confirms them."


def caps_tables(config: Config) -> list[CapsTable]:
    """The leverage caps grid, one table per product.  # SPEC §7.4"""
    tables: list[CapsTable] = []
    tiers = list(ExperienceTier)
    for product in Product:
        rows: list[tuple[str, list[tuple[Leaf, Leaf]]]] = []
        for tranche in Tranche:
            cells: list[tuple[Leaf, Leaf]] = []
            for tier in tiers:
                caps = config.leverage_caps[product][tranche][tier]
                base = f"leverage_caps.{product.value}.{tranche.value}.{tier.value}"
                cells.append(
                    (
                        Leaf(f"{base}.ltc", "LTC", display(f"{base}.ltc", caps.ltc)),
                        Leaf(f"{base}.ltv", "LTV", display(f"{base}.ltv", caps.ltv)),
                    )
                )
            rows.append((tranche.value, cells))
        tables.append(
            CapsTable(product=enum_label(product), tiers=[tier.value for tier in tiers], rows=rows)
        )
    return tables
