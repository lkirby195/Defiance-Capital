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
      column they sit in - ``deal.product``, ``estimated_sale_price``;
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


class UserExists(ServiceError):
    """A user already signs in with that email."""

    def __init__(self, email: str) -> None:
        super().__init__(f"a user already exists with email {email}")
        self.email = email


class UserNotFound(ServiceError):
    """No ``users`` row with that email."""

    def __init__(self, email: str) -> None:
        super().__init__(f"no user with email {email}")
        self.email = email


class ActionNotAllowed(ServiceError):
    """The deal's status rules the team action out.  # SPEC §4.6

    The review-queue actions each apply from a defined set of statuses: a deal already dead
    is not declined, a deal that was never declined is not re-opened. ``allowed_from`` names
    the states the action does run from, so the queue can say why the button did nothing.
    """

    def __init__(
        self, deal_id: UUID, status: Status, action: str, allowed_from: set[Status]
    ) -> None:
        names = ", ".join(sorted(s.value for s in allowed_from)) or "no status"
        super().__init__(
            f"deal {deal_id} is {status.value}, so it cannot be {action}; "
            f"that action applies from {names}"
        )
        self.deal_id = deal_id
        self.status = status
        self.action = action
        self.allowed_from = allowed_from


class ReasonRequired(ServiceError):
    """The action records a reason and none was given."""

    def __init__(self, action: str) -> None:
        super().__init__(f"{action} records a reason; enter one and try again")
        self.action = action
