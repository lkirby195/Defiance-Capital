"""Flip analysis: the project's margin if the property is sold, and the borrower's return.

# SPEC §8.4

Line by line, in the order every output prints it:

    sale price          = estimated_sale_price
    broker              = sale price x broker_selling_pct           # the deal's own, else config
    purchase            = purchase_price
    closing             = closing_costs
    rehab               = rehab_costs
    contingency         = rehab_costs x contingency_pct
    holding             = holding_costs_total
    financing           = interest (total over the term) + origination (both halves)
    profit              = sale price - broker - purchase - closing - rehab - contingency
                          - holding - financing

    total deal cost     = purchase + closing + rehab + contingency + holding + financing
    borrower equity     = total deal cost - loan amount
    project yield       = profit / total deal cost
    cash-on-cash        = profit / borrower equity          (None when equity <= 0)

``project_yield`` is a project margin, not an annualized return, and everything that renders
it labels it "Project yield" so nobody reads it as the IRR sitting above it on the same page.
The broker's cut comes off the sale price and is deliberately not in the total deal cost: it
is what it costs to sell the house, not what it costs to do the project. The cash-on-cash is
held to ``pricing.borrower_coc_target``, and ``engine/underwrite.py`` flags a deal that falls
short of it.

The cost stack and the borrower's equity do not depend on the sale price, so they are
computed and reported on every run; the figures the sale price feeds are null when there is
no price to sell at. There is no toggle: the analysis runs on every deal (engine 2.0.0).
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.terms import LoanTerms, broker_selling_pct
from schema.models import AnalysisStatus, FlipAnalysis, ReturnOverview, UnderwriteInputs

ZERO = Decimal(0)


def financing_costs(overview: ReturnOverview) -> Decimal:
    """Every dollar the loan costs the borrower: interest over the term, plus both fees."""
    return overview.total_interest + overview.total_fees


def cash_on_cash(net_profit: Decimal, equity: Decimal) -> Decimal | None:
    """Profit over the borrower's equity; None when the loan covers every cost.  # SPEC §8.4

    A return on no cash at all - or on a negative amount of it - is not a number.
    """
    return net_profit / equity if equity > ZERO else None


def flip_analysis(
    inputs: UnderwriteInputs,
    loan: LoanTerms,
    overview: ReturnOverview,
    config: Config,
) -> FlipAnalysis:
    """The flip's cost stack and the borrower's equity, and its profit when there is a price.

    # SPEC §8.4
    """
    sizing = loan.sizing
    contingency = sizing.rehab_adj - sizing.rehab_costs
    financing = financing_costs(overview)
    total_costs = (
        sizing.purchase_price
        + sizing.closing_costs
        + sizing.rehab_costs
        + contingency
        + loan.holding_costs_total
        + financing
    )
    equity = total_costs - loan.commitment
    target = config.pricing.borrower_coc_target
    sale_price = inputs.deal.estimated_sale_price
    common = {
        "purchase_price": sizing.purchase_price,
        "closing_costs": sizing.closing_costs,
        "holding_costs_total": loan.holding_costs_total,
        "rehab_costs": sizing.rehab_costs,
        "contingency": contingency,
        "financing_costs": financing,
        "interest": overview.total_interest,
        "origination": overview.total_fees,
        "total_costs": total_costs,
        "loan_amount": loan.commitment,
        "borrower_equity": equity,
        "cash_on_cash_target": target,
        "broker_selling_pct": broker_selling_pct(inputs, config),
    }
    if sale_price is None:
        return FlipAnalysis(
            status=AnalysisStatus.NOT_EVALUATED,
            estimated_sale_price=None,
            broker_costs=None,
            net_profit=None,
            project_yield=None,
            **common,
        )
    broker_costs = sale_price * common["broker_selling_pct"]
    net_profit = sale_price - broker_costs - total_costs
    coc = cash_on_cash(net_profit, equity)
    return FlipAnalysis(
        status=AnalysisStatus.EVALUATED,
        estimated_sale_price=sale_price,
        broker_costs=broker_costs,
        net_profit=net_profit,
        project_yield=net_profit / total_costs,
        cash_on_cash=coc,
        cash_on_cash_passed=None if coc is None else coc >= target,
        **common,
    )
