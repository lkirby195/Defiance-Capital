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
    """The deal is missing values a run needs; ``missing`` names them.

    Two sets reach here, and both are things the team has to go and get:

    * the narrow set the engine cannot run without (``services/assemble.py``), named as the
      column they sit in - ``deal.product``, ``as_is_value``;
    * the minimum viable intake (SPEC §4.1) still outstanding on a ``NEEDS_INFO`` deal,
      which is ``IntakeRecord.missing_fields`` verbatim - ``borrower.phone``.

    The queue shows either list the same way, so the distinction is in where the names come
    from, not in what the reader does about them.
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

    def __init__(self, deal_id: UUID, status: Status, detail: str | None = None) -> None:
        super().__init__(
            f"deal {deal_id} is {status.value} and cannot be underwritten; "
            + (detail or "a team member re-opens it first")
        )
        self.deal_id = deal_id
        self.status = status
