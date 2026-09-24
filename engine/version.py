"""Engine version, recorded on every screens / underwrites row.  # CLAUDE.md "Record everything"

Bump on any change to the math in ``engine/`` and add or update a fixture test that
demonstrates the change.

**1.1.0 (Phase 5a, 2026-09-23) makes LTV the only value ratio.** The as-is value is gone
from the engine, and so is LTARV: ``LTV = commitment / estimated_sale_price`` with no
fallback denominator, NOT_AVAILABLE when there is no price, and a caps cell of ``ltc`` and
``ltv``. ``ValueBasis`` and ``MetricCheck.basis`` go with them - there is one denominator, so
nothing has to say which was used - as do ``ScreenFlag.AS_IS_VALUE_MISSING`` and
``ScreenFlag.LTARV_OVER_CAP``; ``LTV_AS_IS_OVER_CAP`` is ``LTV_OVER_CAP``, and
``ESTIMATED_SALE_PRICE_MISSING`` now reports the LTV that could not be computed rather than
the LTARV. The underwrite no longer needs a sale price to run: with the Flip toggle on and no
price the flip is NOT_EVALUATED and ``SALE_PRICE_MISSING`` (Info, fixed in code) says so,
exactly as ``MONTHLY_RENT_MISSING`` does for the two DSCRs. The LTC placeholder cap is 100%.

**1.0.0 (Phase 5, 2026-09-23) replaces the underwrite calc layer and its inputs.** Nothing
about a stored 0.8.1 underwrite survives it, which is why the number is a major one and why
migration ``0010`` deletes the rows written before it rather than pretending they can be
rebuilt.

What the underwrite is now: a dated monthly cash-flow ledger from ``closing_date`` to
``payoff_date`` (SPEC §8.3), whose headline number is a true XIRR on the actual dates, and
three analyses beside it - Flip (SPEC §8.4), Rental (§8.5) and Take-Back (§8.6). The deal
carries its own ``interest_rate``, ``closing_date``, ``origination_fee_pct``,
``contingency_pct``, ``closing_costs_usd`` and ``holding_costs_total_usd``, each with a
config default but the rate.

What is gone: the term-level average-outstanding model and its ``draw_avg_utilization``
constant, the closed-form rate solve and ``target_irr``, the sensitivity grid, the
minimum-interest and extension arithmetic, the borrower economics block, the DSCR takeout
(``REFI_SHORTFALL``), the REO liquidation downside (``DOWNSIDE_COVER_BELOW_FLOOR``) and its
foreclosure config, and the itemized annual taxes / insurance / utilities inputs and their
percentage-of-as-is defaults - one ``holding_costs_total_usd`` stands where all three did.

The screen keeps its structure and changes one number: the LTC denominator carries the
lender's ``closing_costs_usd`` (an input, config default 1,000) where it carried 3% of the
purchase price, and ``ARV_MISSING`` is ``ESTIMATED_SALE_PRICE_MISSING``. A previously
``0.10`` contingency now defaults to ``0.00``, so ``rehab_adj`` and every LTC on a deal with
a rehab budget moves with it.

Earlier history, for the record: 0.2.0 sizing + screen; 0.2.1 aggregated the judgment and
tax-lien thresholds; 0.3.0 the calc layer, rate solve, grid and underwrite; 0.4.0 the Phase
2b review decisions; 0.5.0 valuation and court-record provenance on the results; 0.6.0 the
grid's 1e-9 target tolerance and TEAM_SOURCED_VALUES; 0.7.0 court inputs on the underwrite;
0.8.0 the team-entered loan split and the optional market rent; 0.8.1
REHAB_PORTION_EXCEEDS_BUDGET.
"""

from __future__ import annotations

ENGINE_VERSION = "1.1.0"
