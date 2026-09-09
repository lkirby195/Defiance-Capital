"""Intake endpoints. Phase 1: team entry only.  # SPEC §4.2"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from db.repository import create_deal_from_intake
from db.session import get_session
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import Channel, IntakeRecord

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/team", response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
def submit_team_entry(
    form: TeamEntryForm, session: Annotated[Session, Depends(get_session)]
) -> IntakeRecord:
    """Accept a team-entered deal, store it, return the IntakeRecord with ``missing_fields``."""
    record = normalize(
        parse_team_form(form),
        Channel.TEAM,
        raw_payload=form.model_dump(mode="json", exclude_none=True),
    )
    create_deal_from_intake(session, record)
    session.commit()
    return record
