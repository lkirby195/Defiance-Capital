"""Every form post in the queue carries a CSRF token, and is refused without one.

This module is what lets every other test post without thinking about tokens: the signed-in
test client fills the field in the way a page does (``tests/conftest.py``), and the refusal
is proved here instead, route by route. A guard that quietly stopped being attached to a
router fails here rather than passing everywhere.

Two halves, and both matter. The routes half posts to every guarded path with no token, a
forged one, an expired one and another user's, and asserts 403 each time. The templates half
renders every page and asserts that every ``<form method="post">`` on it carries the field -
because a guard nobody can satisfy is an outage, and a form that forgot the field is exactly
that.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.security import SESSION_HOURS, check_csrf, issue_csrf
from db.models import AuditLog, Deal, User
from schema.models import Status
from services import create_user
from tests.conftest import USER_PASSWORD, QueueClient, requires_db, sign_in

pytestmark = requires_db

# A complete team-entry body, so the intake posts below are refused for the token and not
# for a missing box (``api/intake_form.py``).
COMPLETE_INTAKE: dict[str, Any] = {
    "borrower_name": "Sam",
    "borrower_phone": "918-555-0101",
    "credit_range": "T2",
    "experience_bucket": "3_5",
    "repeat_borrower": "false",
    "address": "1 Main St, Tulsa, OK 74119",
    "purchase_price": "100000.00",
    "rehab_costs": "$0",
    "loan_requested": "$70,000",
    "term_months": "9",
    "interest_rate": "12%",
    "closing_date": "2027-01-01",
}

# Every state-changing path a browser posts to, with a body that would otherwise be accepted.
GUARDED: list[tuple[str, dict[str, Any]]] = [
    ("/queue/deals/{deal}/intake", COMPLETE_INTAKE),
    ("/queue/deals/{deal}/screen", {}),
    ("/queue/deals/{deal}/underwrite", {}),
    ("/queue/deals/{deal}/overrides", {"estimated_sale_price_team": "$250,000"}),
    ("/queue/deals/{deal}/advance", {}),
    ("/queue/deals/{deal}/decline", {"reason": "leverage"}),
    ("/queue/deals/{deal}/dead", {"reason": "went quiet"}),
    ("/queue/deals/{deal}/reopen", {"reason": "new ARV"}),
    ("/queue/deals/{deal}/note", {"note": "spoke to the broker"}),
    ("/intake/team", COMPLETE_INTAKE),
    ("/logout", {}),
]

FORM_TAG = re.compile(r'<form\b[^>]*method="post"[^>]*>(.*?)</form>', re.I | re.S)
CSRF_INPUT = re.compile(r'name="_csrf"\s+value="([^"]*)"')


def paths(deal: Deal) -> list[tuple[str, dict[str, Any]]]:
    return [(path.format(deal=deal.id), dict(body)) for path, body in GUARDED]


def ids() -> list[str]:
    return [path for path, _ in GUARDED]


def audit_count(session: Session) -> int:
    return len(list(session.scalars(select(AuditLog))))


# --- the token itself -----------------------------------------------------------------------------


def test_a_token_is_good_for_the_user_it_was_minted_for(
    db_session: Session, queue_user: User
) -> None:
    """Bound to the user, so a signed-in attacker's own token is no use as somebody else."""
    other = create_user(
        db_session,
        name="Other Person",
        email="other@glenwood.example",
        password=USER_PASSWORD,
        actor="conftest",
    )
    db_session.commit()

    token = issue_csrf(queue_user)
    assert check_csrf(queue_user, token)
    assert not check_csrf(other, token)
    assert check_csrf(other, issue_csrf(other))


@pytest.mark.parametrize(
    "token",
    ["", "no-dot", "0.", ".sig", "notanumber.sig", "9999999999.wrong-signature"],
)
def test_a_token_it_did_not_mint_is_refused(queue_user: User, token: str) -> None:
    assert check_csrf(queue_user, token) is False
    assert check_csrf(queue_user, None) is False


def test_a_tampered_token_is_refused(queue_user: User) -> None:
    stamp, _, signature = issue_csrf(queue_user).partition(".")
    assert not check_csrf(queue_user, f"{stamp}.{signature[:-1]}x")
    # a later expiry with the old signature is the obvious forgery to try
    assert not check_csrf(queue_user, f"{int(stamp) + 86400}.{signature}")


def test_a_token_expires_with_the_session(queue_user: User) -> None:
    """Long enough that a page open all day still posts; not so long that a week does."""
    minted = datetime.now(UTC) - timedelta(hours=SESSION_HOURS + 1)
    assert not check_csrf(queue_user, issue_csrf(queue_user, now=minted))
    fresh = issue_csrf(queue_user)
    assert check_csrf(queue_user, fresh)
    assert not check_csrf(
        queue_user, fresh, now=datetime.now(UTC) + timedelta(hours=SESSION_HOURS + 1)
    )


# --- the routes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("index", range(len(GUARDED)), ids=ids())
def test_a_post_without_a_token_is_refused(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal, index: int
) -> None:
    path, body = paths(deal_with_overrides)[index]
    before = audit_count(db_session)

    response = client.post(path, data={**body, "_csrf": ""}, follow_redirects=False)

    assert response.status_code == 403, f"{path} accepted a post with no token"
    assert "did not carry a valid one-time token" in response.text
    assert "nothing was changed" in response.text.lower()
    db_session.expire_all()
    assert audit_count(db_session) == before, f"{path} wrote something before refusing"


@pytest.mark.parametrize("index", range(len(GUARDED)), ids=ids())
def test_a_post_with_a_forged_token_is_refused(
    client: QueueClient, deal_with_overrides: Deal, index: int
) -> None:
    path, body = paths(deal_with_overrides)[index]
    forged = "9999999999.not-a-signature-this-app-would-produce"
    response = client.post(path, data={**body, "_csrf": forged}, follow_redirects=False)
    assert response.status_code == 403, f"{path} accepted a forged token"


@pytest.mark.parametrize("index", range(len(GUARDED)), ids=ids())
def test_a_post_with_the_right_token_goes_through(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal, index: int
) -> None:
    """The guard has to be satisfiable, or it is an outage rather than a control."""
    # a status every action below runs from, so only the token is under test
    deal_with_overrides.status = Status.SCREENED
    db_session.commit()
    if GUARDED[index][0].endswith("/reopen"):
        deal_with_overrides.status = Status.DECLINED
        db_session.commit()

    path, body = paths(deal_with_overrides)[index]
    response = client.post(path, data=body, follow_redirects=False)
    assert response.status_code != 403, f"{path} refused a token it minted itself"


def test_another_users_token_is_refused(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """The half of the attack that a secret alone does not stop."""
    other = create_user(
        db_session,
        name="Other Person",
        email="other@glenwood.example",
        password=USER_PASSWORD,
        actor="conftest",
    )
    db_session.commit()

    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/note",
        data={"note": "not mine to write", "_csrf": issue_csrf(other)},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_a_token_survives_signing_out_and_back_in_as_someone_else(
    anon_client: QueueClient, db_session: Session, queue_user: User, deal_with_overrides: Deal
) -> None:
    """Each session's token is its own; the previous one does not carry over."""
    create_user(
        db_session,
        name="Other Person",
        email="other@glenwood.example",
        password=USER_PASSWORD,
        actor="conftest",
    )
    db_session.commit()

    sign_in(anon_client, queue_user.email, USER_PASSWORD)
    first = anon_client.csrf_token()
    anon_client.post("/logout", follow_redirects=False)

    sign_in(anon_client, "other@glenwood.example", USER_PASSWORD)
    anon_client.forget_csrf()
    stale = anon_client.post(
        f"/queue/deals/{deal_with_overrides.id}/note",
        data={"note": "with the old token", "_csrf": first},
        follow_redirects=False,
    )
    assert stale.status_code == 403
    fresh = anon_client.post(
        f"/queue/deals/{deal_with_overrides.id}/note",
        data={"note": "with my own"},
        follow_redirects=False,
    )
    assert fresh.status_code == 303


def test_a_stranger_is_told_to_sign_in_rather_than_told_about_tokens(
    anon_client: QueueClient, deal_with_overrides: Deal
) -> None:
    """The guard runs first but stands aside: a stranger has nothing to bind a token to."""
    response = anon_client.post(
        f"/queue/deals/{deal_with_overrides.id}/note",
        data={"note": "hello"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_get_is_not_guarded(client: QueueClient, deal_with_overrides: Deal) -> None:
    """A route that changes something on a GET would be the bug, not the missing token."""
    assert client.get("/queue").status_code == 200
    assert client.get(f"/queue/deals/{deal_with_overrides.id}").status_code == 200
    assert client.get(f"/queue/deals/{deal_with_overrides.id}/intake").status_code == 200
    assert client.get("/queue/new").status_code == 200


def test_a_json_body_is_exempt_but_a_form_body_is_not(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """A cross-site HTML form cannot send application/json; it can send urlencoded."""
    accepted = client.post("/intake/team", json=team_entry)
    assert accepted.status_code == 201, accepted.text

    refused = client.post(
        "/intake/team", data={"borrower_name": "Sam", "_csrf": ""}, follow_redirects=False
    )
    assert refused.status_code == 403
    del db_session


def test_the_json_deal_api_is_not_a_form_and_is_not_guarded(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """``/deals/*`` takes JSON and is reached by a client, not a page."""
    response = client.post(f"/deals/{deal_with_overrides.id}/screen")
    assert response.status_code == 201, response.text


# --- the pages ------------------------------------------------------------------------------------


def forms_on(body: str) -> list[str]:
    return FORM_TAG.findall(body)


@pytest.mark.parametrize(
    "path", ["/queue", "/queue/new", "/queue/deals/{deal}", "/queue/deals/{deal}/intake"]
)
def test_every_post_form_on_a_page_carries_the_field(
    client: QueueClient, deal_with_overrides: Deal, path: str
) -> None:
    """A guard nobody can satisfy is an outage; this is what stops one shipping."""
    response = client.get(path.format(deal=deal_with_overrides.id))
    assert response.status_code == 200
    found = forms_on(response.text)
    assert found, f"{path} rendered no form at all"
    for inner in found:
        match = CSRF_INPUT.search(inner)
        assert match is not None, f"a form on {path} has no CSRF field"
        assert match.group(1), f"a form on {path} has an empty CSRF field"


def test_the_deal_page_carries_one_on_every_action(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """Eight forms: two runs, four actions, the note and the override block."""
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    assert len(forms_on(body)) == 9  # the eight above plus sign-out in the header


def test_the_sign_in_page_has_no_token_to_carry(anon_client: QueueClient) -> None:
    """Nothing to bind one to before there is a session, and nothing yet to protect."""
    body = anon_client.get("/login").text
    assert forms_on(body), "the sign-in form should still be there"
    assert 'name="_csrf"' not in body


def test_the_rejection_page_does_not_leak_a_token(
    client: QueueClient, deal_with_overrides: Deal
) -> None:
    """A 403 that handed back a working token would be a strange sort of refusal."""
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/note",
        data={"note": "x", "_csrf": ""},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert 'name="_csrf" value=""' in response.text or 'name="_csrf"' not in response.text
