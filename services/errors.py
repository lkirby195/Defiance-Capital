"""Errors the service layer raises; the API turns them into status codes."""

from __future__ import annotations

from uuid import UUID

from schema.models import Status


class ServiceError(Exception):
    """Base for everything in this package."""


class DealNotFound(ServiceError):
    """No ``deals`` row with that id."""

    def __init__(self, deal_id: UUID) -> None:
        super().__init__(f"no deal {deal_id}")
        self.deal_id = deal_id


class DealNotReady(ServiceError):
    """The deal is missing values the engine needs; ``missing`` names them.

    Distinct from ``IntakeRecord.missing_fields`` (SPEC §4.1), which is what the team still
    has to ask the borrower for. This is the narrower set the engine cannot run without.
    """

    def __init__(self, deal_id: UUID, missing: list[str]) -> None:
        super().__init__(f"deal {deal_id} is missing: {', '.join(missing)}")
        self.deal_id = deal_id
        self.missing = missing


class DealNotUnderwritable(ServiceError):
    """The deal's status rules out an underwrite.  # SPEC §4.5

    A declined or dead deal is not priced until a person re-opens it; doing it silently
    would put an underwrite on a deal nobody is working.
    """

    def __init__(self, deal_id: UUID, status: Status) -> None:
        super().__init__(
            f"deal {deal_id} is {status.value} and cannot be underwritten; "
            "a team member re-opens it first"
        )
        self.deal_id = deal_id
        self.status = status
