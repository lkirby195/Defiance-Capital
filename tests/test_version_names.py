"""The arrival version's name: the deal's own, on the web and from the New Deal form.  # SPEC §5

A web deal's first version is ``{last name}; {street number} {street name}, {state} -
Request``; a team deal's is the same with ``- Team``, unless the New Deal form's optional
Version name box says otherwise. Every later run keeps ``v<n>`` / ``Auto v<n>`` / a typed
name, and the arrival version is the system's run either way: Working, never human-selected.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ratelimit import LIMITER
from api.routes.apply import THANKS_PATH
from db.models import Borrower, Deal, Property
from schema.models import Channel, State
from services import arrival_name, deal_versions
from services.versions import NAME_MAX
from tests.conftest import USER_EMAIL, QueueClient, form_body, requires_db
from tests.test_apply import VALID

# --- the name, from the deal (no database) --------------------------------------------------------


def deal_of(
    channel: Channel,
    name: str | None = "Dana Whitlock",
    address: str | None = "1412 S Trenton Ave, Tulsa, OK 74120",
    state: State = State.OK,
) -> Deal:
    deal = Deal(channel=channel)
    deal.borrower = Borrower(name=name)
    deal.property = Property(address_raw=address, state=state)
    return deal


def test_a_web_deal_is_its_last_name_street_and_state_then_request() -> None:
    assert arrival_name(deal_of(Channel.WEB)) == "Whitlock; 1412 S Trenton Ave, OK - Request"


def test_a_team_deal_ends_in_team() -> None:
    assert arrival_name(deal_of(Channel.TEAM)) == "Whitlock; 1412 S Trenton Ave, OK - Team"


def test_the_last_name_is_the_last_word_and_the_street_the_first_part() -> None:
    deal = deal_of(
        Channel.WEB,
        name="  María Elena  Ortiz ",
        address=" 3320   Meade St , Denver, CO 80211",
        state=State.CO,
    )
    assert arrival_name(deal) == "Ortiz; 3320 Meade St, CO - Request"
    # an address typed as the street alone is the street
    street_only = deal_of(Channel.TEAM, address="3320 Meade St", state=State.CO)
    assert arrival_name(street_only) == "Whitlock; 3320 Meade St, CO - Team"


def test_a_piece_the_deal_does_not_have_is_left_out() -> None:
    # a state outside OK and CO has no two-letter code stored
    other = deal_of(Channel.WEB, address="77 Main St, Amarillo, TX", state=State.OTHER)
    assert arrival_name(other) == "Whitlock; 77 Main St - Request"
    assert arrival_name(deal_of(Channel.TEAM, name=None)) == "1412 S Trenton Ave, OK - Team"
    assert arrival_name(deal_of(Channel.TEAM, name="  ")) == "1412 S Trenton Ave, OK - Team"
    assert arrival_name(deal_of(Channel.WEB, address=None)) == "Whitlock; OK - Request"
    # nothing to name it for: the run is Auto v1
    assert arrival_name(deal_of(Channel.TEAM, name=None, address=None)) is None


def test_the_other_channels_keep_auto() -> None:
    for channel in (Channel.SMS, Channel.LINK, Channel.CONTRACT):
        assert arrival_name(deal_of(channel)) is None


def test_a_long_street_is_shortened_and_the_ending_kept() -> None:
    street = "1412 " + "Very Long Street Name " * 8 + "Ave"
    name = arrival_name(deal_of(Channel.TEAM, address=f"{street}, Tulsa, OK"))
    assert name is not None and len(name) <= NAME_MAX
    assert name.startswith("Whitlock; 1412 Very Long") and name.endswith(", OK - Team")


# --- the arrival run, through the routes ---------------------------------------------------------


def notice(response: Any) -> str:
    return unquote(response.headers["location"].split("notice=", 1)[1])


def versions(session: Session, deal: Deal) -> list[tuple[str, bool, bool, str]]:
    """Every version oldest first: name, Working, human-selected, actor."""
    session.expire_all()
    return [
        (version.name, version.is_working, version.human_selected, version.actor)
        for version in reversed(deal_versions(session, deal.id))
    ]


def only_deal(session: Session) -> Deal:
    (deal,) = list(session.scalars(select(Deal)))
    return deal


@requires_db
def test_a_web_submission_arrives_as_its_request(
    anon_client: QueueClient, db_session: Session
) -> None:
    LIMITER.reset()
    response = anon_client.post("/apply", data=VALID, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(THANKS_PATH)
    deal = only_deal(db_session)
    assert versions(db_session, deal) == [
        ("Ortiz; 1412 S Peoria Ave, OK - Request", True, False, "system")
    ]


@requires_db
def test_a_new_deal_left_unnamed_arrives_as_team_and_later_runs_keep_their_names(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    created = client.post("/intake/team", data=form_body(team_entry), follow_redirects=False)
    assert created.status_code == 303, created.text
    assert notice(created).endswith("Saved as Ortiz; 3320 Meade St, CO - Team (Working).")
    deal = only_deal(db_session)
    assert client.post(f"/queue/deals/{deal.id}/run", follow_redirects=False).status_code == 303
    edited = client.post(
        f"/queue/deals/{deal.id}/intake",
        data=form_body(team_entry, rehab_costs="50000.00"),
        follow_redirects=False,
    )
    assert edited.status_code == 303, edited.text
    assert versions(db_session, deal) == [
        ("Ortiz; 3320 Meade St, CO - Team", False, False, "system"),
        ("v2", False, False, USER_EMAIL),
        ("Auto v3", True, False, "system"),
    ]


@requires_db
def test_a_typed_version_name_names_the_arrival_run_and_is_not_a_choice(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    posted = {**form_body(team_entry), "version_name": "  First   look "}
    created = client.post("/intake/team", data=posted, follow_redirects=False)
    assert created.status_code == 303, created.text
    deal = only_deal(db_session)
    assert versions(db_session, deal) == [("First look", True, False, "system")]
    # the box is not the intake: the stored payload never carries it
    assert all("version_name" not in sub.raw_payload for sub in deal.submissions)


@requires_db
def test_a_json_post_is_named_for_the_deal_too(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    assert client.post("/intake/team", json=team_entry).status_code == 201
    deal = only_deal(db_session)
    assert [name for name, *_ in versions(db_session, deal)] == ["Ortiz; 3320 Meade St, CO - Team"]


@requires_db
def test_a_name_too_long_is_refused_under_its_box_and_nothing_is_stored(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    posted = {**form_body(team_entry), "version_name": "x" * (NAME_MAX + 1)}
    response = client.post("/intake/team", data=posted, follow_redirects=False)
    assert response.status_code == 422
    assert 'id="version_name-error"' in response.text
    assert f'value="{"x" * (NAME_MAX + 1)}"' in response.text
    assert list(db_session.scalars(select(Deal))) == []


@requires_db
def test_only_the_new_deal_form_has_the_box(client: QueueClient, stored_deal: Deal) -> None:
    new = client.get("/queue/new").text
    assert new.count('name="version_name"') == 1
    assert '<label for="version_name">Version name <span class="mark">Optional</span>' in new
    # under the three groups, above the button
    assert new.index("<h2>Deal Economics</h2>") < new.index('name="version_name"')
    assert new.index('name="version_name"') < new.index(">Create deal</button>")
    edit = client.get(f"/queue/deals/{stored_deal.id}/intake").text
    assert 'name="version_name"' not in edit
