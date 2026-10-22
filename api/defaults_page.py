"""The /defaults page: every value in ``config/glenwood.yaml``, grouped, read-only.  # SPEC §10

A person pricing a deal wants to know what stands in when nobody typed a number - the rate,
the fees, the assumptions - and what the screen tests against, without reading yaml. This
module turns the validated ``Config`` into labelled rows, one per yaml leaf, keyed by the
leaf's dotted path (``fees.origination_default_pct``). The page prints that path beside every
value, so a person who wants a number changed can name exactly the one they mean.

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

from config.config import Config
from schema.labels import enum_label
from schema.models import ExperienceTier, Product, Tranche

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
)
_MONEY_ENDINGS = ("_usd", "loan_amount_rounding")
_RATIO_ENDINGS = ("dscr_floor",)


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


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of a nested mapping by dotted key; a list is one leaf, not one per item."""
    if isinstance(data, dict):
        leaves: dict[str, Any] = {}
        for name, value in data.items():
            name = name.value if isinstance(name, Enum) else str(name)
            leaves.update(flatten(value, f"{prefix}.{name}" if prefix else name))
        return leaves
    return {prefix: data}


def group_of(key: str) -> str:
    """The group whose longest prefix matches ``key``; ``OTHER`` when none does."""
    best, length = OTHER, -1
    for heading, prefixes in GROUPS:
        for prefix in prefixes:
            if (key == prefix or key.startswith(prefix + ".")) and len(prefix) > length:
                best, length = heading, len(prefix)
    return best


def _one(key: str, value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        if key.endswith(_PERCENT_ENDINGS):
            text = f"{value * 100:.3f}".rstrip("0").rstrip(".")
            return f"{text}%"
        if key.endswith(_MONEY_ENDINGS):
            sign = "-" if value < 0 else ""
            return f"{sign}${abs(value):,.2f}"
        if key.endswith(_RATIO_ENDINGS):
            return f"{value:.2f}x"
    return str(value)


def display(key: str, value: Any) -> str:
    """A leaf's value as the page prints it: ``2%``, ``$1,000.00``, ``1.20x``, ``OK, CO``."""
    if isinstance(value, list):
        return ", ".join(_one(key, item) for item in value) or "(none)"
    return _one(key, value)


def label_for(key: str) -> str:
    """The last part of a dotted key, in words: ``holding_costs_default_pct_of_cost`` ->
    ``Holding costs default pct of cost``."""
    words = key.rsplit(".", 1)[-1].replace("_", " ")
    return words[:1].upper() + words[1:]


def config_leaves(config: Config) -> dict[str, Any]:
    """Every leaf of the validated config, by the dotted key the yaml spells it with."""
    return flatten(config.model_dump(mode="python"))


def groups(config: Config) -> list[Group]:
    """Every leaf but the caps grid, in its group, groups in ``GROUPS`` order."""
    by_heading = {heading: Group(heading) for heading, _ in GROUPS}
    by_heading[OTHER] = Group(OTHER)
    for key, value in config_leaves(config).items():
        if key.startswith("leverage_caps."):
            continue
        by_heading[group_of(key)].leaves.append(Leaf(key, label_for(key), display(key, value)))
    return [group for group in by_heading.values() if group.leaves or group.heading != OTHER]


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
