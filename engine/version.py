"""Engine version, recorded on every screens / underwrites row.  # CLAUDE.md "Record everything"

Bump on any change to the math in ``engine/`` and add or update a fixture test that
demonstrates the change. Phase 2a (sizing + screen) started at 0.2.0; 0.2.1 aggregated
the judgment and tax-lien thresholds across matters. Phase 2b (0.3.0) added the calc
layer, the rate solve, the yield grid, and the underwrite, and made any open tax lien
HARD regardless of amount. Phase 2c (0.4.0) applied the Phase 2b review decisions: one
buy-side closing number (3% of price) in the screen's total_cost and the borrower's
project cost, opex defaults as a percentage of the as-is value rather than the ARV, the
SPEC §3 exit inference with ``exit_source``, and the NO_REHAB_PERIOD and
SOLVED_RATE_BELOW_GRID informational flags.

Phase 2d (0.5.0) changed no math: every input produces the number it produced at 0.4.0.
What changed is the shape of the result - ``SizingResult`` now carries where each half of
the valuation came from and ``ScreenComponents`` where the court record came from (SPEC
§6.1). The version still moves, because ``screens.engine_version`` is what tells a later
reader which shape a stored row is in, and two different shapes must not both claim 0.4.0.

Phase 2e (0.6.0) is a real change of math semantics: a grid cell now meets the target within
``engine.grids.TARGET_TOLERANCE`` (1e-9) rather than exactly, so the r* column always meets
the target it was solved for instead of missing it by a unit in the last place on some
commitments. Every fixture's grid flag counts were recomputed against it. The screen and the
underwrite also raise TEAM_SOURCED_VALUES (INFO) when a value they ran on was entered by
hand (SPEC §6.1).

Phase 4 (0.7.0) gives the underwrite court inputs of its own: ``UnderwriteInputs`` carries a
``CourtRecordInputs``, and the underwrite re-runs the SPEC §7.2 tests on it rather than
leaving the court record to Stage 1 (SPEC §8.1). The same inputs therefore produce a longer
flag list than they did at 0.6.0 - the §7.2 codes, COURT_RECORDS_NOT_CHECKED where nothing
was checked, and a TEAM_SOURCED_VALUES message that can now name the court search - so the
version moves even though no formula changed.

Phase 4c (0.8.0) changes the split products and the takeout, and both change numbers.

The purchase / rehab split is now entered by the team rather than derived: ``SizingInputs``
carries ``loan_purchase_portion`` and ``loan_rehab_portion``, which add up to the loan
requested, and ``purchase_portion_override`` is gone along with the
``commitment - rehab_adj`` default behind it (SPEC §8.2). A split product with no split
entered is sized on the loan requested and reports no split at all, which is where a
borrower-channel intake sits until somebody divides it; the underwrite refuses one. The
rehab side is still capped at ``rehab_adj`` and a SPLIT_PRINCIPAL commitment still lands
below the request when that cap bites (COMMITMENT_BELOW_REQUEST). ``CommitmentSplit`` drops
``purchase_portion_overridden`` - there is no derivation left to override - and gains
``rehab_portion_requested`` and ``rehab_portion_capped``; a row written before 0.8.0 still
rebuilds, through a validator that retires the old field.

The DSCR takeout is no longer computed on a rent nobody entered. ``market_rent_monthly`` is
optional, and without it ``ExitResult.status`` is NOT_EVALUATED, every figure the rent feeds
is None - ``refi_covers`` included, rather than False - and the underwrite raises
MARKET_RENT_MISSING (INFO, fixed) instead of REFI_SHORTFALL (SPEC §8.1, §8.6). Annual
utilities gained the config default the taxes and insurance already had
(``takeout.opex_defaults.utilities_pct_of_as_is_value``), so a deal without them is carried
at a percentage of the as-is value instead of refusing to run.
"""

from __future__ import annotations

ENGINE_VERSION = "0.8.0"
