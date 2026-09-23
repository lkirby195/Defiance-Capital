"""Flip analysis: the project's margin if the property is sold.  # SPEC §8.4

    broker_costs    = estimated_sale_price x broker_selling_pct
    contingency     = rehab_costs x contingency_pct
    financing_costs = total interest over the term + both origination halves
    net_profit      = estimated_sale_price - broker_costs
                      - (purchase_price + closing_costs) - holding_costs_total
                      - rehab_costs - contingency - financing_costs
    total_costs     = purchase_price + closing_costs + holding_costs_total
                      + rehab_costs + contingency + financing_costs
    profit_yield    = net_profit / total_costs

``profit_yield`` is a project margin, not an annualized return, and everything that renders
it labels it "Yield (Profit / Costs)" so nobody reads it as the IRR sitting above it on the
same page. The broker's cut comes off the sale price and is deliberately not in
``total_costs``: it is what it costs to sell the house, not what it costs to do the project.

The cost stack does not depend on the sale price, so it is computed and reported whether the
toggle is on or not. Only the four figures the sale price feeds are null when it is off or
when there is no price to sell at.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.terms import LoanTerms
from schema.models import AnalysisStatus, FlipAnalysis, ReturnOverview, UnderwriteInputs


def financing_costs(overview: ReturnOverview) -> Decimal:
    """Every dollar the loan costs the borrower: interest over the term, plus both fees."""
    return overview.total_interest + overview.total_fees


def flip_analysis(
    inputs: UnderwriteInputs,
    loan: LoanTerms,
    overview: ReturnOverview,
    on: bool,
    config: Config,
) -> FlipAnalysis:
    """The flip's cost stack, and its profit when there is a price to sell at.  # SPEC §8.4"""
    sizing = loan.sizing
    contingency = sizing.rehab_adj - sizing.rehab_costs
    financing = financing_costs(overview)
    total_costs = (
        sizing.purchase_price
        + sizing.closing_costs
        + loan.holding_costs_total
        + sizing.rehab_costs
        + contingency
        + financing
    )
    broker_pct = config.fees.broker_selling_pct
    sale_price = inputs.deal.estimated_sale_price
    common = {
        "purchase_price": sizing.purchase_price,
        "closing_costs": sizing.closing_costs,
        "holding_costs_total": loan.holding_costs_total,
        "rehab_costs": sizing.rehab_costs,
        "contingency": contingency,
        "financing_costs": financing,
        "total_costs": total_costs,
        "broker_selling_pct": broker_pct,
    }
    if not on:
        return FlipAnalysis(
            status=AnalysisStatus.OFF,
            estimated_sale_price=None,
            broker_costs=None,
            net_profit=None,
            profit_yield=None,
            **common,
        )
    if sale_price is None:
        return FlipAnalysis(
            status=AnalysisStatus.NOT_EVALUATED,
            estimated_sale_price=None,
            broker_costs=None,
            net_profit=None,
            profit_yield=None,
            **common,
        )
    broker_costs = sale_price * broker_pct
    net_profit = sale_price - broker_costs - total_costs
    return FlipAnalysis(
        status=AnalysisStatus.EVALUATED,
        estimated_sale_price=sale_price,
        broker_costs=broker_costs,
        net_profit=net_profit,
        profit_yield=net_profit / total_costs,
        **common,
    )
