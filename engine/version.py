"""Engine version, recorded on every screens / underwrites row.  # CLAUDE.md "Record everything"

Bump on any change to the math in ``engine/`` and add or update a fixture test that
demonstrates the change.

**1.11.0 (ui-deal, 2026-10-27) puts the default rate on the grid and takes the deal's rate
off it.** ``interest.default_annual_rate`` is 12.5% where it was 12%, the sensitivity grid's
first column, and the two sensitivity tables' columns are the config grid alone
(``engine/calc/sensitivity.rate_columns``, 12.5% to 17.5% by 1%): the deal's own rate is no
longer inserted, so a deal priced off the grid has no marked cell and its IRR is the Return
Overview's. Every grid cell is unchanged; a deal on the default rate now prices its ledger at
12.5%, and its own cell is the first column's bottom row. The pricing solve still starts its
bracket from the grid and the deal's rate, and finds the same rates. Every fixture carries
its own rate, so no fixture's ledger moves; Denver at 12% loses its inserted column and its
marked cell (``tests/test_sensitivity.py``).

**1.10.0 (Phase 8e, 2026-10-22) steps the pricing matrix by percent.** A row is a share of
the request rather than a dollar change: ``pricing.loan_amount_pct_steps`` (placeholder 0,
-5%, -10%) replaces ``pricing.loan_amount_steps``, and each stepped row's amount is the
request times ``1 + step`` rounded half-up to the nearest ``pricing.loan_amount_rounding``
(placeholder $1,000), never above the request; the request row is never rounded
(``engine/calc/pricing.row_amount``). ``PricingRow`` gains ``pct_step`` and a ``label``
("Requested", "-5%", "-10%"); ``step`` is now the dollar change the share came to. A stored
1.9.0 row loads with ``pct_step`` None and is labelled by its dollar step. The solve is
unchanged. Wichita's rows are $150,000, $143,000 and $135,000, all at 16.34% (its IRR does
not depend on the amount); Denver's are $195,000, $185,000 and $176,000 at 13.82%, 13.72% and
13.63%, each checked against an independent solve (``tests/test_pricing.py``). The loader
refuses the old key by name.

**1.9.0 (Phase 8c prep, 2026-10-20) caps what the pricing matrix quotes.** A row whose solved
rate is above ``pricing.max_quoted_rate`` (placeholder 18%) is no longer a quote: its rate,
IRR and borrower figures are None, ``PricingRow.rate_needed`` carries the solved rate, and the
sentence reads "A $150,000 loan exceeds max quoted rate (20.15% needed)." A rate equal to the
maximum is quoted. ``PricingMatrix`` records ``max_quoted_rate``; both new fields default to
None, so a stored 1.8.0 result loads. No fixture's rate is above the placeholder, so nothing
else moves; ``tests/test_pricing.py`` lowers the maximum under Wichita's 16.34%.

**1.8.0 (Phase 8b, 2026-10-20) solves the pricing rate.** The pricing matrix no longer
picks the lowest Lender Sensitivity column whose lender XIRR reaches
``pricing.target_lender_irr``; for each loan amount step it bisects on the ledger itself,
over whole steps of 0.01%, for the lowest rate whose XIRR reaches the target, and reports
that rate (two decimals of a percent, rounded up so it still clears), the XIRR there and the
borrower's profit, cash and cash-on-cash there (``engine/calc/pricing.py``). The grid's
columns only set the starting bracket; a row they never clear is searched up to 100%, the
most the model accepts. ``PricingMatrix`` gains ``rate_precision`` and ``rate_ceiling`` and
leaves ``rates`` (the columns a 1.7.0 matrix searched) empty, so a stored 1.7.0 result
still loads. Wichita prices at 16.34% where the grid said 16.5%, Denver at 13.82%, 13.72%
and 13.61%, each checked against an independent solve (``tests/test_pricing.py``). Nothing
else moves: the ledger, the IRR, the two sensitivity tables and the three analyses are
unchanged on every fixture.

**1.7.0 (Phase 8a, 2026-10-20) adds the borrower's side and the pricing matrix.**
``UnderwriteResult`` gains ``borrower_sensitivity`` (``BorrowerSensitivityTable``, SPEC §8.9)
and ``pricing`` (``PricingMatrix``, SPEC §8.10). The Borrower Sensitivity has the Lender
Sensitivity's rows and columns, and each cell is the Flip Analysis's net profit at that loan
amount and rate, the cash the borrower puts in (total project costs less the loan) and the
one over the other; it is ``NOT_EVALUATED`` when the flip is off or there is no sale price
(``engine/calc/borrower.py``). The pricing matrix takes each ``pricing.loan_amount_steps``
entry and names the lowest grid rate at which the lender's XIRR reaches
``pricing.target_lender_irr`` (placeholders 20% and 0 / -10,000 / -20,000), with the lender
IRR and the borrower's profit and cash-on-cash there, and one plain-English sentence per
row (``engine/calc/pricing.py``). The three tables share one memo of ledgers
(``engine/calc/sensitivity.Grid``), so the new tables cost almost nothing on top of the
lender's. The old table is the **Lender Sensitivity** wherever it is shown; its numbers and
every other figure are unchanged, and a stored 1.6.0 result loads with both new fields None.

**1.6.0 (Phase 7c, 2026-10-05) lets a deal carry its own analysis assumptions.** Five
numbers the three analyses read from config alone become per-deal inputs with the config
value as their default, on the same rule as the origination fee and the holding costs:
``broker_selling_pct`` (SPEC §8.4), ``rental_expenses_pct_of_rent`` and
``rental_takeout_rate`` (SPEC §8.5), ``take_back_legal_costs_usd`` and
``take_back_lost_interest_months`` (SPEC §8.6). ``UnderwriteInputs`` gains the five as
optional fields, ``engine/calc/terms.py`` resolves each in one place, and the flip, the
rental and the take-back read the resolved value. Nothing moves on a deal that carries
none of them: every fixture's ledger, IRR, flip, rental and take-back stand, and the one new
fixture (``go_team_assumptions_tulsa``) is the Tulsa deal with all five typed over. No
result shape changes, so no migration deletes a run; migration ``0017`` adds the columns.

**1.5.1 (Phase 7b, 2026-10-04) puts the deal's own rate on the sensitivity grid.** The
table's columns are the config grid plus the deal's actual rate, inserted in rate order when
it falls between two grid rates and not duplicated when it lands on one, so the deal's own
cell is always present and marked. Every grid cell is unchanged; the one new column is the
ledger the Return Overview already shows, at each loan amount.

**1.5.0 (Phase 7a, 2026-10-04) adds the sensitivity table.** ``UnderwriteResult`` gains
``sensitivity`` (``SensitivityTable``, SPEC §8.9): IRR by loan amount and rate, every cell a
full re-sizing and re-ledgering of the deal in the same caps cell
(``engine/calc/sensitivity.py``). Rows are the request and ``sensitivity.loan_steps`` rows
above it, each ``sensitivity.loan_step_usd`` less, the request on the bottom row; columns run
``sensitivity.rate_min`` to ``rate_max`` in ``rate_step`` steps (placeholders: $5,000 x 4,
12.5% to 17.5% by 1%). A reduction comes off the advance at closing and leaves the rehab
portion alone until the advance is gone. The deal's own cell is marked when its rate falls
on a column. Nothing else moves: every fixture's ledger, totals and IRR stand, and the base
row at the deal's own rate reproduces the deal's IRR exactly. The field defaults to None so a
stored 1.4.0 row still loads; no migration.

**1.4.0 (Phase 7, 2026-10-02) anchors the ledger to month ends and drops the exit
inference.** Closing is treated as the last day of its month: month 0 of the ledger is dated
that month end, month ``m`` the last day of the month ``m`` later, and the payoff date is the
last day of the month that is the closing month plus the term (``schema/dates.anchor_date``).
A deal closing 15 October for nine months pays off on 31 July, where it paid off on 15 July.
Whole-month interest is unchanged - a period's interest is a twelfth of the annual rate
whatever its days - so every fixture's totals stand and only its dates and its XIRR move.
The stub arithmetic is kept, measured from the month-end anchor, for a future actual-payoff
entry; no form produces one. ``asset_type``, ``stated_exit`` and the §3 exit inference are
gone from the engine and the model: ``UnderwriteResult.exit`` is ``analyses``
(``AnalysisToggles``), and the Flip analysis defaults on when a sale price is present, the
Rental analysis when a rent is present (``engine/calc/analyses.py``). Migration ``0016``
drops the two columns and deletes the stored underwrites, whose shape no longer loads.

**1.3.0 (Phase 6a, 2026-10-01) names the borrower as a source.** ``ValueSource`` gains
``BORROWER``: the estimated sale price and the monthly rent a borrower types on the public
form (SPEC §4.2) reach the engine as the borrower's own when no adapter and no team member
has one - adapter over team over borrower, each superseded value kept on the deal in its own
column. ``UnderwriteInputs`` gains ``monthly_rent_source`` so the underwrite can say where the
rent its two DSCRs rest on came from. New flag ``BORROWER_SOURCED_VALUES`` (Info, fixed in
code), raised by both stages and naming which inputs were the borrower's, beside
``TEAM_SOURCED_VALUES`` when one value is the team's and another the borrower's. No number
changes: a borrower's sale price sizes LTV exactly as a team's would. Fixtures
``go_web_borrower_values_okc`` and ``go_web_overridden_by_team_okc`` demonstrate it.

**1.2.0 (Phase 5b, 2026-09-24) prices an arbitrary payoff date, and makes the holding cost a
percentage.** The term is no longer a whole number of months: ``term_months`` is the count of
full monthly periods anchored to the closing date's day (clamped to a short month), and
``term_stub_days`` is whatever a payoff date between two anchors leaves over. The ledger gets
one more row for that stub, dated the payoff date itself, accruing one month's interest scaled
by ``stub_days / interest.day_count_basis`` (config, placeholder 30) on the balance standing at
its start, and carrying the payoff and the fee's payoff half - so both land on the date the team
entered rather than on the anchor before it. The draw schedule and ``rehab_months`` are whole
periods only. ``UnderwriteResult`` gains ``term_stub_days`` and ``term_months_decimal``, and
``LedgerEntry`` gains ``stub_days``. Entering a term in months still derives a whole-month
payoff date, and a term beside a mid-month payoff date is refused as a disagreement.

``holding_costs_total_usd`` is ``holding_costs_pct_of_cost``: the team enters a share of
``purchase_price + rehab_costs`` (config default 2%) and the dollar figure is computed from it,
so a corrected price moves the carry with it. ``DealEconomics`` reports all three -
``holding_costs_pct_of_cost``, ``holding_costs_basis`` and ``holding_costs_total`` - and the
monthly carry now divides by ``term_months_decimal`` rather than by the whole months, because a
hold that runs eleven days past its last anchor pays eleven days more. Migration ``0012``
converts each stored dollar figure to the percentage it was and deletes the stored runs.

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

ENGINE_VERSION = "1.11.0"
