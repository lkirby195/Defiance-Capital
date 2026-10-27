"""What a person is shown where the model stores a code.  # SPEC §3, §4.1, §7.1

Several of the enums on an ``IntakeRecord`` are stored as codes nobody outside this
repository speaks. ``Tranche`` is ``T1``..``T5`` and ``ExperienceBucket`` is ``0``/``1_2``/
``3_5``/``6_PLUS``; the thing a team member actually picked was a FICO range and a number of
deals. ``Product`` and ``LoanPurpose`` are SCREAMING_SNAKE for the same reason - they are
stored values and config grid keys - and a person reads "Holdback", not ``ACQUISITION_REHAB``.
The stored value does not change; nothing a person reads says the code any more.

The credit labels are derived from ``credit.tranche_cutoffs`` rather than written out, so a
lender who moves a cutoff moves the label with it (CLAUDE.md, "No hardcoded thresholds"). The
cutoffs arrive as a mapping rather than a ``Config`` because ``schema/`` is imported by
``config/`` and must not import it back.

``enum_label`` is the general rule - title case with spaces - and ``_ENUM_LABELS`` is the
short list of codes that rule does not fit. Today that is the two loan structures, which a
person knows by the words the team form's radio puts on them - "100% at close" and
"Holdback" (SPEC §3) - rather than by their codes title-cased.

``PRODUCT_DEFINITIONS`` is the SPEC §3 table in one line each.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from schema.models import ExperienceBucket, Product, TermBucket, Tranche

# En dashes: these are ranges, and the form and the screen summary read as prose.
EXPERIENCE_BUCKET_LABELS: Mapping[ExperienceBucket, str] = {
    ExperienceBucket.ZERO: "0",
    ExperienceBucket.ONE_TO_TWO: "1–2",
    ExperienceBucket.THREE_TO_FIVE: "3–5",
    ExperienceBucket.SIX_PLUS: "6+",
}

# The borrower's answer to "how long do you need the loan?" (SPEC §4.1), as they read it.
TERM_BUCKET_LABELS: Mapping[TermBucket, str] = {
    TermBucket.M3: "3 months",
    TermBucket.M6: "6 months",
    TermBucket.M9: "9 months",
    TermBucket.M12: "12 months",
    TermBucket.M12_PLUS: "12+ months",
}

# The codes ``str.title()`` does not fit: the two loan structures, by the words on the team
# form's radio (SPEC §3). Everything else - CASH_OUT, BANKRUPTCY_IN_LOOKBACK - title-cases
# correctly and is deliberately not listed, so this stays a list of exceptions rather than a
# second copy of every enum.
_ENUM_LABELS: Mapping[str, str] = {
    Product.ACQUISITION.value: "100% at close",
    Product.ACQUISITION_REHAB.value: "Holdback",
}

# SPEC §3, one line each: structure, then how interest is charged.
PRODUCT_DEFINITIONS: Mapping[Product, str] = {
    Product.ACQUISITION: (
        "One note, the whole loan advanced at close; interest on the full commitment from close."
    ),
    Product.ACQUISITION_REHAB: (
        "One note: the advance at closing, then the rehab portion held back and drawn over "
        "the rehab period; interest on the full commitment from close."
    ),
}

_TRANCHE_ORDER: tuple[Tranche, ...] = tuple(Tranche)  # T1 (best) .. T5 (worst)  # SPEC §7.1

DASH = "—"


def enum_label(value: StrEnum | str | None) -> str:
    """A stored code as the words a person reads: ``CASH_OUT`` -> ``Cash Out``.

    Title case with the underscores as spaces, except for the handful of codes that rule
    mangles (``_ENUM_LABELS``). An em dash when nobody chose one.
    """
    if value is None:
        return DASH
    code = str(getattr(value, "value", value))
    known = _ENUM_LABELS.get(code)
    if known is not None:
        return known
    return code.replace("_", " ").title()


def product_definition(product: Product | str | None) -> str:
    """The SPEC §3 one-liner for a product; empty for none, so a template can skip it."""
    if product is None:
        return ""
    return PRODUCT_DEFINITIONS.get(Product(product), "")


def experience_label(bucket: ExperienceBucket | None) -> str:
    """``0`` / ``1–2`` / ``3–5`` / ``6+``; an em dash when nobody said.  # SPEC §4.1"""
    if bucket is None:
        return DASH
    return EXPERIENCE_BUCKET_LABELS[bucket]


def term_bucket_label(bucket: TermBucket | None) -> str:
    """``9 months`` / ``12+ months``; an em dash when no borrower channel asked.  # SPEC §4.1"""
    if bucket is None:
        return DASH
    return TERM_BUCKET_LABELS[bucket]


def tranche_label(tranche: Tranche | None, cutoffs: Mapping[Tranche, int]) -> str:
    """The FICO range a tranche stands for: ``740+``, ``700–739``, ``Under 620``.  # SPEC §7.1

    The worst tranche is everything below the cutoff above it, so it has no lower bound to
    print; the best has no upper one. Every other is the pair.
    """
    if tranche is None:
        return DASH
    if tranche is _TRANCHE_ORDER[-1]:
        return f"Under {cutoffs[_TRANCHE_ORDER[-2]]}"
    index = _TRANCHE_ORDER.index(tranche)
    if index == 0:
        return f"{cutoffs[tranche]}+"
    return f"{cutoffs[tranche]}–{cutoffs[_TRANCHE_ORDER[index - 1]] - 1}"
