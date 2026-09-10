"""REO downside, run on every deal; no income or cap-rate valuation.  # SPEC §8.6

Recovery is what a foreclosure and resale returns after the haircut, selling costs, the
fixed foreclosure cost, and the holding cost carried through the foreclosure period:

    recovery_basis  = min(as_is_value + rehab_adj, arv)
    liquidation     = recovery_basis x (1 - reo_haircut)
    monthly_holding = (annual_taxes + annual_insurance + annual_utilities) / 12
    recovery        = liquidation x (1 - selling_cost_pct) - foreclosure_cost_usd
                      - foreclosure_months[state] x monthly_holding
    exposure        = commitment + unpaid fees          # the origination portion due at payoff
    downside_cover  = recovery / exposure               # flagged below downside.cover_floor
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.borrower import monthly_holding_cost
from engine.calc.lender import payoff_fees
from engine.calc.outstanding import LoanTerms
from schema.models import DownsideResult, UnderwriteInputs

ONE = Decimal(1)


def recovery_basis(as_is_value: Decimal, rehab_adj: Decimal, arv: Decimal) -> Decimal:
    """min(as_is_value + rehab_adj, arv): value recovered cannot exceed the ARV.  # SPEC §8.6"""
    return min(as_is_value + rehab_adj, arv)


def liquidation_value(basis: Decimal, config: Config) -> Decimal:
    """basis x (1 - downside.reo_haircut).  # SPEC §8.6"""
    return basis * (ONE - config.downside.reo_haircut)


def reo_downside(inputs: UnderwriteInputs, loan: LoanTerms, config: Config) -> DownsideResult:
    """Recovery vs. exposure in a foreclosure-and-resale scenario.  # SPEC §8.6"""
    basis = recovery_basis(inputs.as_is_value, loan.sizing.rehab_adj, inputs.arv)
    liquidation = liquidation_value(basis, config)
    selling = liquidation * config.fees.selling_cost_pct
    months = config.downside.foreclosure_months[inputs.state]
    monthly = monthly_holding_cost(inputs, config)
    holding = monthly * Decimal(months)
    recovery = liquidation - selling - config.downside.foreclosure_cost_usd - holding
    unpaid = payoff_fees(loan, config)
    exposure = loan.commitment + unpaid
    if exposure <= 0:
        raise ValueError(f"exposure must be positive, got {exposure}")
    cover = recovery / exposure
    floor = config.downside.cover_floor
    return DownsideResult(
        recovery_basis=basis,
        liquidation=liquidation,
        selling_costs=selling,
        foreclosure_cost=config.downside.foreclosure_cost_usd,
        foreclosure_months=months,
        monthly_holding_cost=monthly,
        holding_through_foreclosure=holding,
        recovery=recovery,
        unpaid_fees=unpaid,
        exposure=exposure,
        cover=cover,
        cover_floor=floor,
        passed=cover >= floor,
    )
