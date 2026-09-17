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
"""

from __future__ import annotations

ENGINE_VERSION = "0.5.0"
