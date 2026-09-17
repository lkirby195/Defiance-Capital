"""Borrower economics: profit and cash-on-cash at one (month, rate).  # SPEC §8.6

Information only: no floor and no flag. The lender funds and the borrower spends the full
contingency (``rehab_adj`` is the rehab cost); buy-side closing is borrower cash; holding
costs are the annual taxes, insurance, and utilities pro-rated to the payoff month.

    rehab_adj           = rehab_budget x (1 + contingency_pct)
    buy_closing         = sizing.buy_closing    # the screen's number, unchanged (engine.sizing)
    total_project_cost  = purchase_price + rehab_adj + buy_closing
    holding_costs       = (annual_taxes + annual_insurance + annual_utilities) / 12 x m
    exit_net            = exit_price x (1 - selling_cost_pct)
    profit              = exit_net - total_project_cost - interest_paid - fees_paid - holding_costs
    cash_in             = total_project_cost + interest_paid + fees_paid + holding_costs
                          - commitment
    cash_on_cash        = profit / cash_in                     # None when cash_in <= 0

Annual taxes and insurance are team actuals; when absent they default to the config
percentages of the **as-is** value (``takeout.opex_defaults``): the property is taxed and
insured as it stands, not at its repaired value. Utilities are always a team input. These
two figures are resolved once here and reused by the DSCR takeout (``engine.calc.exit``)
and the REO carry (``engine.calc.downside``), so a deal carries one tax number and one
insurance number everywhere.
"""

from __future__ import annotations

from decimal import Decimal

from config.config import Config
from engine.calc.lender import TWELVE, fee_schedule, interest
from engine.calc.outstanding import LoanTerms
from schema.models import BorrowerEconomics, OpexSource, UnderwriteInputs

ZERO = Decimal(0)
ONE = Decimal(1)


def resolve_annual_taxes(inputs: UnderwriteInputs, config: Config) -> tuple[Decimal, OpexSource]:
    """Team actual when supplied, else as_is_value x opex_defaults.taxes_pct_of_as_is_value."""
    if inputs.annual_taxes_usd is not None:
        return inputs.annual_taxes_usd, OpexSource.ACTUAL
    default = inputs.as_is_value * config.takeout.opex_defaults.taxes_pct_of_as_is_value
    return default, OpexSource.DEFAULT


def resolve_annual_insurance(
    inputs: UnderwriteInputs, config: Config
) -> tuple[Decimal, OpexSource]:
    """Team actual when supplied, else as_is x opex_defaults.insurance_pct_of_as_is_value."""
    if inputs.annual_insurance_usd is not None:
        return inputs.annual_insurance_usd, OpexSource.ACTUAL
    default = inputs.as_is_value * config.takeout.opex_defaults.insurance_pct_of_as_is_value
    return default, OpexSource.DEFAULT


def monthly_holding_cost(inputs: UnderwriteInputs, config: Config) -> Decimal:
    """(annual_taxes + annual_insurance + annual_utilities) / 12.  # SPEC §8.6"""
    taxes, _ = resolve_annual_taxes(inputs, config)
    insurance, _ = resolve_annual_insurance(inputs, config)
    return (taxes + insurance + inputs.annual_utilities_usd) / TWELVE


def exit_net(exit_price: Decimal, config: Config) -> Decimal:
    """exit_net = exit_price x (1 - fees.selling_cost_pct).  # SPEC §8.6"""
    return exit_price * (ONE - config.fees.selling_cost_pct)


def resolve_exit_price(inputs: UnderwriteInputs) -> Decimal:
    """Team-set retail price when supplied (wholetail), else the ARV (flip).  # SPEC §8.6"""
    return inputs.exit_price if inputs.exit_price is not None else inputs.arv


def borrower_economics(
    inputs: UnderwriteInputs, loan: LoanTerms, month: int, rate: Decimal, config: Config
) -> BorrowerEconomics:
    """Profit and cash-on-cash for a payoff at ``month`` at note rate ``rate``.  # SPEC §8.6"""
    price = inputs.deal.purchase_price
    rehab_adj = loan.sizing.rehab_adj
    closing = loan.sizing.buy_closing
    total_project_cost = price + rehab_adj + closing
    interest_paid = interest(loan, month, rate, config)
    fees_paid = fee_schedule(loan, month, config).total
    holding = monthly_holding_cost(inputs, config) * Decimal(month)
    price_out = resolve_exit_price(inputs)
    net = exit_net(price_out, config)
    profit = net - total_project_cost - interest_paid - fees_paid - holding
    cash_in = total_project_cost + interest_paid + fees_paid + holding - loan.commitment
    return BorrowerEconomics(
        month=month,
        rate=rate,
        purchase_price=price,
        rehab_adj=rehab_adj,
        buy_closing=closing,
        total_project_cost=total_project_cost,
        interest_paid=interest_paid,
        fees_paid=fees_paid,
        holding_costs=holding,
        exit_price=price_out,
        exit_net=net,
        profit=profit,
        cash_in=cash_in,
        cash_on_cash=profit / cash_in if cash_in > ZERO else None,
    )
