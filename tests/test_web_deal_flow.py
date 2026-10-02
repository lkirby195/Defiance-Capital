"""A deal submitted on the public form, then worked from the queue: every step answers 200.

The whole path a real deal takes, with a real sign-in in the middle: the borrower posts
``/apply``, a team member is sent to sign in, signs in, lands on the queue, opens the deal
page and the JSON view. Parametrized over the shapes a borrower actually produces - every
optional box blank, a listing link instead of an address, a state outside the two served,
Spanish, each term bucket, no rehab - because the page renders every one of them and a 500
on any is a deal nobody can work.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ratelimit import LIMITER
from db.models import Deal, User
from tests.conftest import USER_PASSWORD, QueueClient, requires_db, sign_in
from tests.test_apply import VALID, ZILLOW

pytestmark = requires_db


@pytest.fixture(autouse=True)
def fresh_limiter() -> Any:
    LIMITER.reset()
    yield
    LIMITER.reset()


VARIANTS: dict[str, dict[str, str]] = {
    "complete": {},
    "no_closing_date": {"closing_date": ""},
    "no_entity": {"entity_name": ""},
    "no_rent": {"monthly_rent": ""},
    "no_sale_price": {"estimated_sale_price": ""},
    "no_optionals": {
        "closing_date": "",
        "entity_name": "",
        "monthly_rent": "",
        "estimated_sale_price": "",
        "referral_note": "",
        "listing_url": "",
        "src": "",
    },
    "listing_link": {"address": "", "city": "", "state": "", "listing_url": ZILLOW},
    "other_state": {"state": "OTHER"},
    "spanish": {"lang": "es"},
    "term_3": {"term_bucket": "3"},
    "term_6": {"term_bucket": "6"},
    "term_9": {"term_bucket": "9"},
    "term_12": {"term_bucket": "12"},
    "no_rehab": {"rehab_costs": "$0"},
}


@pytest.mark.parametrize("variant", sorted(VARIANTS), ids=sorted(VARIANTS))
def test_apply_then_login_then_queue_then_deal_page(
    anon_client: QueueClient, queue_user: User, db_session: Session, variant: str
) -> None:
    body = {**VALID, **VARIANTS[variant]}
    submitted = anon_client.post("/apply", data=body, follow_redirects=False)
    assert submitted.status_code == 303, submitted.text
    (deal,) = list(db_session.scalars(select(Deal)))

    landing = anon_client.get("/queue", follow_redirects=False)
    assert landing.status_code == 303
    login_page = anon_client.get("/login?next=%2Fqueue")
    assert login_page.status_code == 200
    sign_in(anon_client, queue_user.email, USER_PASSWORD)
    queue = anon_client.get("/queue")
    assert queue.status_code == 200, queue.text[:2000]
    page = anon_client.get(f"/queue/deals/{deal.id}")
    assert page.status_code == 200, page.text[:3000]
    api = anon_client.get(f"/deals/{deal.id}")
    assert api.status_code == 200, api.text[:2000]
