"""Adjust my numbers: the borrower changes five figures on their own request.  # SPEC §4.2

The thank-you page carries a link, live for as long as its token is (``services/indicative``),
to a form of five boxes: the sale price they expect, the rehab budget, the loan amount, the
monthly rent and the term bucket. Nothing else - not who they are, not the property, not the
purchase price - and nothing the team enters. A borrower edit is the public form again with
five boxes retyped, applied as an **edit of the intake** the way Edit Intake applies one
(``services/intake.update_intake``) but **only for the boxes the borrower changed**:

    - the latest web submission's stored payload is the base, the five boxes laid over it,
      and the result is a new ``intake_submissions`` row (immutable, the one before kept)
    - a box is a change when it differs from what the deal carries now (``changed_fields``):
      the page filled the boxes from the deal, so a box sent back as it was is no edit. The
      term box is the one read through a mapping: the deal carries the term in months, and
      the bucket the page shows is the one that seeds those months (``TERM_BUCKET_MONTHS``)
      when the team has set the months and cleared the bucket - blank when none does - and
      the term is a change when the months the chosen bucket seeds differ from the deal's,
      or when the deal carries a bucket and a different one was chosen. Only
      the changed boxes' columns move (``update_intake(only=...)``); everything else on the
      deal - the closing date, the purpose, every §8.1 economic the team has typed since
      the borrower's submission, the valuation, the court search - stays exactly where it
      is, and the team's sale price and rent, when it has entered them, still beat the
      borrower's (SPEC §6.1): the run reads the value in force, not the one just typed
    - the audit row is ``INTAKE_EDITED`` as the borrower, ``web:<email>``
    - the deal is run again as ``system`` (``services/autorun.auto_run``), the version named
      **Borrower edit n** - n counting the borrower's edits on this deal - and the Working
      mark moves by the usual rule (SPEC §5); the thank-you page then reads that version
      (``deals.borrower_terms_version``) and records what it showed
    - a deal in a status an edit is refused on - declined, dead, past an LOI - refuses the
      borrower's too (``ActionNotAllowed``), and the page says the request can no longer be
      changed online; a paused deal takes the edit and runs nothing, as it does for the team

The team sees every piece of it in History: the edit row as the borrower and the version as
the system, "Borrower edit 1", with its own verdict and IRR beside the arrival run's.

Every write here takes an actor and writes ``audit_log`` through the services it calls;
nothing commits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import Deal, IntakeSubmission, Version
from db.repository import borrower_columns, intake_columns
from schema.models import TERM_BUCKET_MONTHS, Channel, IntakeRecord, TermBucket
from services.autorun import AutoRun, auto_run
from services.intake import LANGUAGE_KEY, update_intake

__all__ = [
    "ADJUSTABLE_FIELDS",
    "BORROWER_EDIT_PREFIX",
    "EDIT_COLUMNS",
    "LANGUAGE_KEY",
    "apply_borrower_edit",
    "borrower_edit_name",
    "changed_fields",
    "current_bucket",
    "current_values",
    "latest_web_payload",
]

# The five boxes the borrower may change, by the public form's field name, in the order the
# page shows them (SPEC §4.2).
ADJUSTABLE_FIELDS: tuple[str, ...] = (
    "estimated_sale_price",
    "rehab_costs",
    "loan_requested",
    "monthly_rent",
    "term_bucket",
)
# Where each box lands on the deal - the borrower's own columns for the two estimates (SPEC
# §4.2) - and the column it is compared against to tell a change.
DEAL_COLUMNS: dict[str, str] = {
    "estimated_sale_price": "estimated_sale_price_borrower",
    "rehab_costs": "rehab_costs",
    "loan_requested": "loan_requested",
    "monthly_rent": "monthly_rent_borrower",
    "term_bucket": "term_bucket",
}
# Every column a changed box moves: the term bucket carries the term in months with it, the
# way the public form stores it (SPEC §4.2).
EDIT_COLUMNS: dict[str, tuple[str, ...]] = {
    "estimated_sale_price": ("estimated_sale_price_borrower",),
    "rehab_costs": ("rehab_costs",),
    "loan_requested": ("loan_requested",),
    "monthly_rent": ("monthly_rent_borrower",),
    "term_bucket": ("term_bucket", "term_months", "term_stub_days"),
}
# What a borrower edit's version is named, followed by its number (SPEC §5).
BORROWER_EDIT_PREFIX = "Borrower edit "


def latest_web_payload(session: Session, deal: Deal) -> dict[str, Any] | None:
    """The stored payload of the borrower's latest web submission; None when there is none."""
    submission = session.scalar(
        select(IntakeSubmission)
        .where(IntakeSubmission.deal_id == deal.id, IntakeSubmission.channel == Channel.WEB)
        .order_by(IntakeSubmission.received_at.desc(), IntakeSubmission.id.desc())
        .limit(1)
    )
    if submission is None or not isinstance(submission.raw_payload, dict):
        return None
    return dict(submission.raw_payload)


def current_bucket(deal: Deal) -> TermBucket | None:
    """The term bucket the deal carries, or the one that seeds its months when the team has
    set the months and no bucket is on the deal; None when none does (SPEC §4.1)."""
    if deal.term_bucket is not None:
        return deal.term_bucket
    for bucket, months in TERM_BUCKET_MONTHS.items():
        if bucket is not TermBucket.M12_PLUS and months == deal.term_months:
            return bucket
    return None


def current_values(deal: Deal) -> dict[str, Any]:
    """The five figures as the deal carries them now, by form field; the term as its code."""
    values: dict[str, Any] = {}
    for field, column in DEAL_COLUMNS.items():
        if field == "term_bucket":
            bucket = current_bucket(deal)
            values[field] = None if bucket is None else bucket.value
        else:
            values[field] = getattr(deal, column)
    return values


def changed_fields(deal: Deal, record: IntakeRecord) -> list[str]:
    """The boxes the borrower changed: each of the five whose value on ``record`` differs
    from what the deal carries now, in the page's order. The term compares in months, and
    in buckets too when the deal carries one."""
    proposed = {**intake_columns(record), **borrower_columns(record)}
    changed: list[str] = []
    for field, column in DEAL_COLUMNS.items():
        if field == "term_bucket":
            moved = proposed["term_months"] != deal.term_months or (
                deal.term_bucket is not None and proposed[column] is not deal.term_bucket
            )
        else:
            moved = proposed[column] != getattr(deal, column)
        if moved:
            changed.append(field)
    return changed


def borrower_edit_name(session: Session, deal: Deal) -> str:
    """``Borrower edit n``: one more than the borrower edits already run on this deal."""
    names = session.scalars(
        select(Version.name).where(
            Version.deal_id == deal.id, Version.name.like(f"{BORROWER_EDIT_PREFIX}%")
        )
    )
    return f"{BORROWER_EDIT_PREFIX}{sum(1 for _ in names) + 1}"


def apply_borrower_edit(
    session: Session,
    deal: Deal,
    record: IntakeRecord,
    *,
    actor: str,
    config: Config | None = None,
) -> AutoRun:
    """Apply the boxes the borrower changed and run the deal again; the page reads the run.

    # SPEC §4.2. ``record`` is the adjusted web form normalized
    (``services.intake.web_intake_record``); only the columns of the boxes that differ from
    the deal move, so what the team entered since stays. Raises ``ActionNotAllowed`` where
    an edit is refused. Flushes; no commit.
    """
    only = [column for field in changed_fields(deal, record) for column in EDIT_COLUMNS[field]]
    update_intake(session, deal.id, record, actor=actor, config=config, only=only)
    run = auto_run(session, deal, config, name=borrower_edit_name(session, deal))
    if run.version is not None:
        deal.borrower_terms_version = run.version.number
        session.flush()
    return run
