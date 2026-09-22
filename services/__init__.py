"""Service layer: the seam between the pure engine and the database.  # SPEC §7, §8

``engine/`` never touches I/O; ``api/`` never touches the engine directly. Everything that
loads a deal, assembles typed inputs, runs the math, and records the result lives here.

Every write here takes an ``actor`` and records an ``audit_log`` row beside what it changed
(SPEC §5, §11). Nothing here commits: the caller owns the transaction, so a change and its
audit row land together or not at all.
"""

from __future__ import annotations

from services.actions import (
    ADVANCE_TO_REVIEW_FROM,
    DECLINE_FROM,
    MARK_DEAD_FROM,
    REOPEN_FROM,
    add_note,
    advance_to_review,
    decline,
    mark_dead,
    reopen,
    reopen_status,
    save_overrides,
)
from services.audit import DEALS, USERS, deal_trail, jsonable, last_action_at, record_audit
from services.enrichment import NO_ADAPTER_VALUES, AdapterValues, adapter_values
from services.errors import (
    ActionNotAllowed,
    DealNotFound,
    DealNotReady,
    DealNotUnderwritable,
    ReasonRequired,
    ServiceError,
    UserExists,
    UserNotFound,
)
from services.intake import (
    EDIT_INTAKE_FROM,
    create_deal,
    latest_submission,
    screen_is_stale,
    update_intake,
)
from services.lifecycle import (
    advance_after_screen,
    advance_for_underwrite,
    check_intake_complete,
    check_underwritable,
    status_after_screen,
    status_after_underwrite,
)
from services.passwords import WeakPassword, hash_password, verify_password
from services.persistence import (
    latest_screen,
    latest_underwrite,
    record_screen,
    record_underwrite,
    screen_result,
    underwrite_result,
)
from services.queue import (
    QueueEntry,
    QueueGroup,
    QueueView,
    last_activity,
    pin_state,
    queue_view,
)
from services.requests import TeamOverrides, UnderwriteRequest
from services.runner import load_deal, run_screen, run_underwrite
from services.users import (
    authenticate,
    create_user,
    deactivate_user,
    get_user,
    list_users,
    normalize_email,
    user_by_email,
)

__all__ = [
    "ADVANCE_TO_REVIEW_FROM",
    "DEALS",
    "DECLINE_FROM",
    "EDIT_INTAKE_FROM",
    "MARK_DEAD_FROM",
    "NO_ADAPTER_VALUES",
    "REOPEN_FROM",
    "USERS",
    "ActionNotAllowed",
    "AdapterValues",
    "DealNotFound",
    "DealNotReady",
    "DealNotUnderwritable",
    "QueueEntry",
    "QueueGroup",
    "QueueView",
    "ReasonRequired",
    "ServiceError",
    "TeamOverrides",
    "UnderwriteRequest",
    "UserExists",
    "UserNotFound",
    "WeakPassword",
    "adapter_values",
    "add_note",
    "advance_after_screen",
    "advance_for_underwrite",
    "advance_to_review",
    "authenticate",
    "check_intake_complete",
    "check_underwritable",
    "create_deal",
    "create_user",
    "deactivate_user",
    "deal_trail",
    "decline",
    "get_user",
    "hash_password",
    "jsonable",
    "last_action_at",
    "last_activity",
    "latest_screen",
    "latest_submission",
    "latest_underwrite",
    "list_users",
    "load_deal",
    "mark_dead",
    "normalize_email",
    "pin_state",
    "queue_view",
    "record_audit",
    "record_screen",
    "record_underwrite",
    "reopen",
    "reopen_status",
    "run_screen",
    "run_underwrite",
    "save_overrides",
    "screen_is_stale",
    "screen_result",
    "status_after_screen",
    "status_after_underwrite",
    "underwrite_result",
    "update_intake",
    "user_by_email",
    "verify_password",
]
