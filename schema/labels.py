"""What a person is shown where the model stores a code.  # SPEC §3, §4.1, §7.1

Several of the enums on an ``IntakeRecord`` are stored as codes nobody outside this
repository speaks. ``Tranche`` is ``T1``..``T5`` and ``ExperienceBucket`` is ``0``/``1_2``/
``3_5``/``6_PLUS``; the thing a team member actually picked was a FICO range and a number of
deals. ``Product``, ``AssetType``, ``LoanPurpose`` and ``StatedExit`` are SCREAMING_SNAKE for
the same reason - they are stored values and config grid keys - and a person reads "Split
Draw", not ``SPLIT_DRAW``. The stored value does not change; nothing a person reads says the
code any more.

The credit labels are derived from ``credit.tranche_cutoffs`` rather than written out, so a
lender who moves a cutoff moves the label with it (CLAUDE.md, "No hardcoded thresholds"). The
cutoffs arrive as a mapping rather than a ``Config`` because ``schema/`` is imported by
``config/`` and must not import it back.

``enum_label`` is the general rule - title case with spaces - and ``_ENUM_LABELS`` is the
short list of codes title-casing gets wrong: an initialism (``SFR``) and two ranges
(``UNITS_2_4``, ``UNITS_5_PLUS``) that read as "Units 2 4" and "Units 5 Plus" otherwise.

``PRODUCT_DEFINITIONS`` is the SPEC §3 product table in one line each, shown beside the Loan
Type box so a person choosing one is told what they are choosing rather than expected to know.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from schema.models import AssetType, ExperienceBucket, Product, Tranche

# En dashes: these are ranges, and the form and the screen summary read as prose.
EXPERIENCE_BUCKET_LABELS: Mapping[ExperienceBucket, str] = {
    ExperienceBucket.ZERO: "0",
    ExperienceBucket.ONE_TO_TWO: "1–2",
    ExperienceBucket.THREE_TO_FIVE: "3–5",
    ExperienceBucket.SIX_PLUS: "6+",
}

# The codes ``str.title()`` gets wrong. Everything else - NO_DRAW, CASH_OUT, WHOLETAIL -
# title-cases correctly and is deliberately not listed, so this stays a list of exceptions
# rather than a second copy of every enum.
_ENUM_LABELS: Mapping[str, str] = {
    AssetType.SFR.value: "SFR",
    AssetType.UNITS_2_4.value: "Units 2–4",
    AssetType.UNITS_5_PLUS.value: "Units 5+",
}

# SPEC §3, one line each: structure, then how interest is charged. Shown under the Loan Type
# select and beside the chosen product on the deal page.
PRODUCT_DEFINITIONS: Mapping[Product, str] = {
    Product.NO_DRAW: (
        "Single note, full principal at close; interest on the full principal from close. "
        "Purchase only, no rehab funding."
    ),
    Product.SPLIT_DRAW: (
        "Single note: the purchase portion at close, the rehab holdback drawn over the "
        "rehab period; interest on the full commitment from close. Fix-and-flip."
    ),
    Product.SPLIT_PRINCIPAL: (
        "Two notes: the Principal Note at close and Tranche A drawn over the rehab period; "
        "interest on the Principal Note from close and on Tranche A's drawn balance. "
        "Fix-and-flip with a larger rehab."
    ),
    Product.WHOLETAIL: (
        "Single note, full principal at close; interest on the full principal from close. "
        "Buy below market, minimal work, retail resale; short term."
    ),
}

_TRANCHE_ORDER: tuple[Tranche, ...] = tuple(Tranche)  # T1 (best) .. T5 (worst)  # SPEC §7.1

DASH = "—"


def enum_label(value: StrEnum | str | None) -> str:
    """A stored code as the words a person reads: ``SPLIT_DRAW`` -> ``Split Draw``.

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
