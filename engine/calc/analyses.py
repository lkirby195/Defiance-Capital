"""The two analysis toggles and what each defaults to.  # SPEC §8.1

There is no exit inference any more. A deal used to carry an asset type and a stated exit,
and a resale exit turned the Flip analysis on while a hold exit turned the Rental analysis
on; both are gone from the form, the deal page and the model. What decides each toggle's
default now is simpler and closer to what a person actually has: **the Flip analysis is on
when there is a sale price to sell at, and the Rental analysis is on when there is a rent
to carry a loan with.** A toggle set by hand wins over the default in either direction, and
the Take-Back analysis is not a toggle at all - it runs on every deal (SPEC §8.6).
"""

from __future__ import annotations

from decimal import Decimal

from schema.models import AnalysisToggles, UnderwriteInputs


def flip_default(estimated_sale_price: Decimal | None) -> bool:
    """The Flip analysis is on by default when a sale price is present.  # SPEC §8.1"""
    return estimated_sale_price is not None


def rental_default(monthly_rent: Decimal | None) -> bool:
    """The Rental analysis is on by default when a rent is present.  # SPEC §8.1"""
    return monthly_rent is not None


def analysis_toggles(inputs: UnderwriteInputs) -> AnalysisToggles:
    """Both toggles as they will run: the default from what is on the deal, overridden by hand."""
    flip_on = flip_default(inputs.deal.estimated_sale_price)
    rental_on = rental_default(inputs.monthly_rent)
    return AnalysisToggles(
        flip_analysis=flip_on if inputs.flip_analysis is None else inputs.flip_analysis,
        rental_analysis=rental_on if inputs.rental_analysis is None else inputs.rental_analysis,
        flip_analysis_default=flip_on,
        rental_analysis_default=rental_on,
    )
