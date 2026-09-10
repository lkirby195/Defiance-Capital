"""Engine version, recorded on every screens / underwrites row.  # CLAUDE.md "Record everything"

Bump on any change to the math in ``engine/`` and add or update a fixture test that
demonstrates the change. Phase 2a (sizing + screen) started at 0.2.0; 0.2.1 aggregated
the judgment and tax-lien thresholds across matters. Phase 2b (0.3.0) added the calc
layer, the rate solve, the yield grid, and the underwrite, and made any open tax lien
HARD regardless of amount.
"""

from __future__ import annotations

ENGINE_VERSION = "0.3.0"
