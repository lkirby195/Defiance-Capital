"""Adjust my numbers: the borrower changes five figures on their own request.  # SPEC §4.2

The thank-you page carries a link, live for as long as its token is (``services/indicative``),
to a form of five boxes: the sale price they expect, the rehab budget, the loan amount, the
monthly rent and the term bucket. Nothing else - not who they are, not the property, not the
purchase price - and nothing the team enters: a borrower edit is the public form again with
five boxes retyped, applied as an **edit of the intake** exactly as Edit Intake applies one
(``services/intake.update_intake``), so

    - the latest web submission's stored payload is the base, the five boxes laid over it,
      and the result is a new ``intake_submissions`` row (immutable, the one before kept)
    - only the form's own columns move (``db/repository.form_columns``); every §8.1
      economic the team has typed, the valuation, the court search stay where they are, and
      the team's sale price and rent, when it has entered them, still beat the borrower's
      (SPEC §6.1) - the run reads the value in force, not the one just typed
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
from schema.models import Channel, IntakeRecord
from services.autorun import AutoRun, auto_run
from services.intake import LANGUAGE_KEY, update_intake

__all__ = [
    "ADJUSTABLE_FIELDS",
    "BORROWER_EDIT_PREFIX",
    "LANGUAGE_KEY",
    "apply_borrower_edit",
    "borrower_edit_name",
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
# Where each lands on the deal: the borrower's own columns for the two estimates (SPEC §4.2).
DEAL_COLUMNS: dict[str, str] = {
    "estimated_sale_price": "estimated_sale_price_borrower",
    "rehab_costs": "rehab_costs",
    "loan_requested": "loan_requested",
    "monthly_rent": "monthly_rent_borrower",
    "term_bucket": "term_bucket",
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


def current_values(deal: Deal) -> dict[str, Any]:
    """The five figures as the deal carries them now, by form field; the term as its code."""
    values: dict[str, Any] = {}
    for field, column in DEAL_COLUMNS.items():
        value = getattr(deal, column)
        values[field] = value.value if field == "term_bucket" and value is not None else value
    return values


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
    """Apply the borrower's edit and run the deal again; the page reads the run.  # SPEC §4.2

    ``record`` is the adjusted web form normalized (``services.intake.web_intake_record``).
    Raises ``ActionNotAllowed`` where an edit is refused. Flushes; no commit.
    """
    update_intake(session, deal.id, record, actor=actor, config=config)
    run = auto_run(session, deal, config, name=borrower_edit_name(session, deal))
    if run.version is not None:
        deal.borrower_terms_version = run.version.number
        session.flush()
    return run
