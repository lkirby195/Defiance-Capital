"""What a person is shown where the model stores a code.  # SPEC §4.1, §7.1

Two of the enums on an ``IntakeRecord`` are stored as codes nobody outside this repository
speaks. ``Tranche`` is ``T1``..``T5`` and ``ExperienceBucket`` is ``0``/``1_2``/``3_5``/
``6_PLUS``; the thing a team member actually picked was a FICO range and a number of deals.
The stored value does not change - it is what the config grids are keyed on and what every
``screens`` row already holds - but nothing a person reads says ``T3`` any more.

The credit labels are derived from ``credit.tranche_cutoffs`` rather than written out, so a
lender who moves a cutoff moves the label with it (CLAUDE.md, "No hardcoded thresholds").
The cutoffs arrive as a mapping rather than a ``Config`` because ``schema/`` is imported by
``config/`` and must not import it back.
"""

from __future__ import annotations

from collections.abc import Mapping

from schema.models import ExperienceBucket, Tranche

# En dashes: these are ranges, and the form and the screen summary read as prose.
EXPERIENCE_BUCKET_LABELS: Mapping[ExperienceBucket, str] = {
    ExperienceBucket.ZERO: "0",
    ExperienceBucket.ONE_TO_TWO: "1–2",
    ExperienceBucket.THREE_TO_FIVE: "3–5",
    ExperienceBucket.SIX_PLUS: "6+",
}

_TRANCHE_ORDER: tuple[Tranche, ...] = tuple(Tranche)  # T1 (best) .. T5 (worst)  # SPEC §7.1


def experience_label(bucket: ExperienceBucket | None) -> str:
    """``0`` / ``1–2`` / ``3–5`` / ``6+``; an em dash when nobody said.  # SPEC §4.1"""
    if bucket is None:
        return "—"
    return EXPERIENCE_BUCKET_LABELS[bucket]


def tranche_label(tranche: Tranche | None, cutoffs: Mapping[Tranche, int]) -> str:
    """The FICO range a tranche stands for: ``740+``, ``700–739``, ``Under 620``.  # SPEC §7.1

    The worst tranche is everything below the cutoff above it, so it has no lower bound to
    print; the best has no upper one. Every other is the pair.
    """
    if tranche is None:
        return "—"
    if tranche is _TRANCHE_ORDER[-1]:
        return f"Under {cutoffs[_TRANCHE_ORDER[-2]]}"
    index = _TRANCHE_ORDER.index(tranche)
    if index == 0:
        return f"{cutoffs[tranche]}+"
    return f"{cutoffs[tranche]}–{cutoffs[_TRANCHE_ORDER[index - 1]] - 1}"
