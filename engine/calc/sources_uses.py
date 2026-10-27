"""Sources & Uses: where the deal's money comes from and where it goes.  # SPEC §8.11

    Sources                                   Uses
    advance at closing   (the ledger's)       purchase      = purchase_price
    rehab holdback       (the ledger's)       closing       = closing_costs
    borrower equity      = total deal cost    rehab         = rehab_costs
                           - loan amount      contingency   = rehab_costs x contingency_pct
                                              holding       = holding_costs_total
                                              interest      = total interest (SPEC §8.3)
                                              origination   = both origination halves

The two advances are the lender's as the ledger lays them out: what goes out at closing and
the rehab money drawn after it, adding up to the loan amount. On 100% at close the holdback is
zero, and so it is on a holdback whose term leaves no rehab period, where the rehab money goes
out at closing (SPEC §8.3). The uses are the Flip Analysis's cost stack line by line, the
financing as its two parts (SPEC §8.4); their total is the total deal cost. The borrower's
equity is that total less the loan, so the two sides are the same number - the table is a
statement of the identity, and ``tests/test_fixtures.py`` holds every fixture to it to the
cent. Nothing here reads a sale price, so the table is there on every run.

Pure: the resolved ``LoanTerms`` and the ``FlipAnalysis`` in, ``SourcesAndUses`` out. Not
rounded; the outputs round.
"""

from __future__ import annotations

from engine.calc.terms import LoanTerms
from schema.models import FlipAnalysis, SourcesAndUses


def sources_and_uses(loan: LoanTerms, flip: FlipAnalysis) -> SourcesAndUses:
    """The lender's two advances and the borrower's equity against the cost stack.

    # SPEC §8.11
    """
    if flip.interest is None or flip.origination is None or flip.borrower_equity is None:
        raise ValueError("Sources & Uses reads a flip from engine 2.0.0 on")
    advance = loan.funded_at_close
    holdback = loan.commitment - advance
    uses = (
        flip.purchase_price,
        flip.closing_costs,
        flip.rehab_costs,
        flip.contingency,
        flip.holding_costs_total,
        flip.interest,
        flip.origination,
    )
    return SourcesAndUses(
        advance_at_closing=advance,
        rehab_holdback=holdback,
        borrower_equity=flip.borrower_equity,
        total_sources=advance + holdback + flip.borrower_equity,
        purchase_price=flip.purchase_price,
        closing_costs=flip.closing_costs,
        rehab_costs=flip.rehab_costs,
        contingency=flip.contingency,
        holding_costs=flip.holding_costs_total,
        interest=flip.interest,
        origination=flip.origination,
        total_uses=sum(uses[1:], uses[0]),
    )
