"""Average outstanding balance per product.  # SPEC §8.3

Pure: ``Decimal`` in, ``Decimal`` out, constants from ``Config``. The simple model has no
monthly ledger, so Tranche A's drawn balance is summarized by its average utilization over
the rehab period (``draws.draw_avg_utilization``, 0.50 for straight-line draws); the tranche
is fully drawn from rehab completion to payoff. Every other product is fully funded from
close.

    rehab_months       = max(0, term - listing_months)
    tranche_a_avg(m)   = tranche_a x [rehab_months x u + (m - rehab_months)] / m  # m >= rehab
    avg_outstanding(m) = principal_note + tranche_a_avg(m)      # SPLIT_PRINCIPAL
                       = commitment                             # NO_DRAW, SPLIT_DRAW, WHOLETAIL

``dollar_months(m)`` is ``avg_outstanding(m) x m``: the balance-months funded through payoff,
which is what interest accrues on (SPEC §8.4).
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from config.config import Config
from schema.models import Product, SizingResult

ZERO = Decimal(0)


class LoanTerms(NamedTuple):
    """The sized loan plus the term facts every calc needs.  # SPEC §8.1, §8.3"""

    sizing: SizingResult
    term_months: int
    rehab_months: int
    extension_fee_pct: Decimal

    @property
    def commitment(self) -> Decimal:
        return self.sizing.commitment

    @property
    def product(self) -> Product:
        return self.sizing.product


def rehab_months(term_months: int, config: Config) -> int:
    """rehab_months = max(0, term - listing_months).  # SPEC §8.3

    The last ``draws.listing_months`` of the term are listing and sale, so a term at or
    below that leaves no rehab period (Tranche A would be fully drawn from close).
    """
    if term_months < 1:
        raise ValueError(f"term_months must be at least 1, got {term_months}")
    return max(0, term_months - config.draws.listing_months)


def loan_terms(
    sizing: SizingResult,
    term_months: int,
    extension_fee_pct: Decimal | None,
    config: Config,
) -> LoanTerms:
    """Bundle the sized loan with its term, derived rehab months, and extension fee.

    ``extension_fee_pct`` None means the config default (``fees.extension_default_pct``).
    """
    pct = config.fees.extension_default_pct if extension_fee_pct is None else extension_fee_pct
    return LoanTerms(sizing, term_months, rehab_months(term_months, config), pct)


def tranche_a_dollar_months(tranche_a: Decimal, month: int, rehab: int, config: Config) -> Decimal:
    """Balance-months on Tranche A through payoff at ``month``.  # SPEC §8.3

    tranche_a x [rehab x u + (month - rehab)], u = draws.draw_avg_utilization. Requires
    ``month >= rehab``: there is no early payoff in v1 (SPEC §8.4), and grid rows start at
    the term, so payoff before rehab completion never arises.
    """
    if month < rehab:
        raise ValueError(
            f"payoff month {month} is before rehab completion at month {rehab}; "
            "v1 models no early payoff (SPEC §8.4)"
        )
    utilization = config.draws.draw_avg_utilization
    return tranche_a * (Decimal(rehab) * utilization + Decimal(month - rehab))


def dollar_months(loan: LoanTerms, month: int, config: Config) -> Decimal:
    """Balance-months funded through payoff at ``month`` (= avg_outstanding x month).  # SPEC §8.3

    NO_DRAW, WHOLETAIL, SPLIT_DRAW: commitment x month (interest on the full commitment
    from close). SPLIT_PRINCIPAL: Principal Note x month + Tranche A balance-months.
    """
    if month < 1:
        raise ValueError(f"payoff month must be at least 1, got {month}")
    if loan.product is not Product.SPLIT_PRINCIPAL:
        return loan.commitment * Decimal(month)
    split = loan.sizing.split
    if split is None:
        raise ValueError("SPLIT_PRINCIPAL sizing has no Principal Note / Tranche A split")
    return split.purchase_portion * Decimal(month) + tranche_a_dollar_months(
        split.rehab_portion, month, loan.rehab_months, config
    )


def average_outstanding(loan: LoanTerms, month: int, config: Config) -> Decimal:
    """avg_outstanding(m) = dollar_months(m) / m.  # SPEC §8.3"""
    return dollar_months(loan, month, config) / Decimal(month)
