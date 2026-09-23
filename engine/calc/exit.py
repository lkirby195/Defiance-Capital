"""The §3 exit inference, and the two analysis toggles it defaults.  # SPEC §3, §8.1

The exit type gates nothing and flags nothing. All it decides is which of the two optional
analyses a deal gets by default: a resale exit means somebody is going to sell the house, so
the Flip analysis is the one worth running; a hold exit means somebody is going to let it, so
the Rental analysis is. A rent entered on the deal turns Rental on whatever the exit says -
if the team has looked a rent up, they want to see what it does.

A team member can turn either toggle on or off by hand, and that wins over the default in
both directions. The Take-Back analysis is not a toggle: it runs on every deal (SPEC §8.6).
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from schema.models import (
    AssetType,
    ExitInference,
    ExitSource,
    Product,
    StatedExit,
    UnderwriteInputs,
)

# Asset types that resell as a flip or a wholetail on a short term.  # SPEC §3
RESALE_ASSET_TYPES = frozenset({AssetType.SFR, AssetType.UNITS_2_4})

# The exits that mean somebody sells the property at the end.  # SPEC §8.1
RESALE_EXITS = frozenset({StatedExit.FLIP, StatedExit.WHOLETAIL})


def infer_exit(
    stated: StatedExit,
    asset_type: AssetType | None,
    term_months: int,
    product: Product,
    config: Config,
) -> tuple[StatedExit, ExitSource]:
    """The exit in force and where it came from.  # SPEC §3

    A team-stated exit always wins, including a stated HOLD on a short term. Otherwise the
    term and the asset type decide: a short term on a house or a 2-4 resells (as a
    WHOLETAIL when that is the product, else a FLIP), a long term is a hold, and anything
    the two rules do not reach stays UNKNOWN. Term boundaries are config
    (``exit.resale_max_term_months`` / ``exit.hold_min_term_months``).
    """
    if stated is not StatedExit.UNKNOWN:
        return stated, ExitSource.STATED
    if term_months <= config.exit.resale_max_term_months and asset_type in RESALE_ASSET_TYPES:
        resale = StatedExit.WHOLETAIL if product is Product.WHOLETAIL else StatedExit.FLIP
        return resale, ExitSource.INFERRED
    if term_months >= config.exit.hold_min_term_months:
        return StatedExit.HOLD, ExitSource.INFERRED
    return StatedExit.UNKNOWN, ExitSource.INFERRED


def flip_default(exit_type: StatedExit) -> bool:
    """The Flip analysis is on by default for a resale exit.  # SPEC §8.1"""
    return exit_type in RESALE_EXITS


def rental_default(exit_type: StatedExit, monthly_rent: Decimal | None) -> bool:
    """On by default for a hold exit, or when a rent has been entered.  # SPEC §8.1"""
    return exit_type is StatedExit.HOLD or monthly_rent is not None


def exit_inference(inputs: UnderwriteInputs, config: Config) -> ExitInference:
    """The exit, and the state of both toggles - defaulted, then overridden by hand."""
    exit_type, source = infer_exit(
        inputs.stated_exit, inputs.asset_type, inputs.term_months, inputs.deal.product, config
    )
    flip_on = flip_default(exit_type)
    rental_on = rental_default(exit_type, inputs.monthly_rent)
    return ExitInference(
        type=exit_type,
        exit_source=source,
        flip_analysis=flip_on if inputs.flip_analysis is None else inputs.flip_analysis,
        rental_analysis=rental_on if inputs.rental_analysis is None else inputs.rental_analysis,
        flip_analysis_default=flip_on,
        rental_analysis_default=rental_on,
    )
