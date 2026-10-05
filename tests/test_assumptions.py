"""The Underwriting Assumptions panel.  # SPEC §8.1, §8.4, §8.5, §8.6, §9

Open by default, directly under Deal Economics: sixteen boxes in two columns, each showing
what the deal carries, tagged "default" while that is the config or formula value, and
resettable one at a time; one Save & Run at the bottom and a Reset all to defaults beside it
that confirms on a page of its own. Five of the boxes are new columns on ``deals`` - the
broker's selling percentage, the rental's expense ratio and takeout rate, the take-back's
legal costs and lost-interest months - and the engine reads the deal's number when it carries
one and the config value otherwise (engine ``1.6.0``).

Four promises are pinned here: the panel renders every box with the right tag, each of the
five overrides moves the engine's result, Reset all puts the config values back and runs
again, and every audit row names exactly the columns that moved.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.routes.queue import ASSUMPTION_NAMES, assumptions_form
from config.config import Config
from db.models import AuditLog, Deal, Underwrite
from engine.underwrite import underwrite
from schema.models import AuditAction, UnderwriteResult
from services import (
    ASSUMPTIONS,
    DEALS,
    FLAT,
    InputSource,
    UnderwritingAssumptions,
    is_defaulted,
    latest_underwrite,
    save_assumptions,
    underwrite_readiness,
    underwrite_result,
)
from services.assemble import underwrite_inputs
from services.requests import UnderwriteRequest
from tests.conftest import ACTOR, USER_EMAIL, QueueClient, requires_db, store_deal

pytestmark = requires_db

CONFIG = Config.load()
D = Decimal

# The five new assumptions: the text a person types, the number the deal then carries, the
# figure on the result that reports it, and a figure downstream of it that has to move.
Figure = Callable[[UnderwriteResult], Any]
OVERRIDES: list[tuple[str, str, Decimal | int, Figure, Figure]] = [
    (
        "broker_selling_pct",
        "5%",
        D("0.05"),
        lambda r: r.flip.broker_selling_pct,
        lambda r: r.flip.net_profit,
    ),
    (
        "rental_expenses_pct_of_rent",
        "40%",
        D("0.40"),
        lambda r: r.rental.expenses_pct,
        lambda r: r.rental.dscr,
    ),
    (
        "rental_takeout_rate",
        "7%",
        D("0.07"),
        lambda r: r.rental.takeout_rate,
        lambda r: r.rental.debt_service_monthly,
    ),
    (
        "take_back_legal_costs_usd",
        "$7,500",
        D("7500"),
        lambda r: r.take_back.legal_costs,
        lambda r: r.take_back.total_cost,
    ),
    (
        "take_back_lost_interest_months",
        "4",
        4,
        lambda r: r.take_back.lost_interest_months,
        lambda r: r.take_back.lost_interest,
    ),
]
NAMES = [name for name, *_ in OVERRIDES]


def notice(response: Any) -> str:
    return unquote(response.headers["location"].split("notice=", 1)[1])


def audit(session: Session, deal: Deal, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.table_name == DEALS,
                AuditLog.row_id == str(deal.id),
                AuditLog.action == action.value,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def panel(client: QueueClient, deal: Deal) -> str:
    """The Underwriting Assumptions section of the deal page, and nothing after it."""
    body = client.get(f"/queue/deals/{deal.id}").text
    start = body.index('<section class="panel" id="assumptions">')
    end = body.index("<h2>Return Overview</h2>")
    return body[start:end]


def tag(section: str, name: str) -> str | None:
    """The default tag beside one box: "shown", "hidden", or None when the box has none."""
    found = re.search(
        rf'<span class="dflt" data-default-tag="{name}"( hidden)?>default</span>', section
    )
    if found is None:
        return None
    return "hidden" if found.group(1) else "shown"


def post(client: QueueClient, deal: Deal, **changes: str) -> Any:
    """Save & Run with the panel exactly as rendered, plus the boxes a person changed."""
    body = {name: value for name, value in assumptions_form(deal).items() if value}
    body.update(changes)
    return client.post(f"/queue/deals/{deal.id}/assumptions", data=body, follow_redirects=False)


# --- the panel ------------------------------------------------------------------------------------


def test_the_panel_is_open_under_deal_economics_with_every_box_and_the_right_tag(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """A SPLIT_DRAW with a typed closing cost and holding cost and a split equal to the
    formula, so the tags say the different things they have to say."""
    deal = store_deal(db_session, team_entry)
    body = client.get(f"/queue/deals/{deal.id}").text
    assert body.index("<h2>Deal Economics</h2>") < body.index('id="assumptions"')
    assert body.index('id="assumptions"') < body.index("<h2>Return Overview</h2>")
    assert 'id="inputs"' not in body
    section = panel(client, deal)
    assert "<h2>Underwriting Assumptions" in section
    assert '<div class="grid2">' in section
    for name in ASSUMPTION_NAMES:
        assert f'name="{name}"' in section, name
    # the ten config defaults and the formula split carry the tag, shown while untouched
    typed = ("closing_costs_usd", "holding_costs_pct_of_cost")
    for name in (*FLAT, "loan_purchase_portion", "loan_rehab_portion"):
        assert tag(section, name) == ("hidden" if name in typed else "shown"), name
        assert f'data-reset="{name}"' in section, name
    # ...hidden on the two the team typed over, which show the team's number
    assert re.search(r'id="closing_costs_usd"[^>]*value="\$1,500"', section)
    assert re.search(r'id="holding_costs_pct_of_cost"[^>]*value="3%"', section)
    # ...absent on the two with no default at all
    assert tag(section, "estimated_sale_price_team") is None
    assert tag(section, "monthly_rent") is None
    # ...and on the toggles, shown while Default is the chosen state, with a reset of their own
    for name in ("flip_analysis", "rental_analysis"):
        assert tag(section, name) == "shown", name
        assert f'data-reset-toggle="{name}"' in section
    # the boxes show what the deal carries, masked, the five assumptions at their config value
    assert re.search(r'id="broker_selling_pct"[^>]*value="4%"', section)
    assert re.search(r'id="rental_expenses_pct_of_rent"[^>]*value="35%"', section)
    assert re.search(r'id="rental_takeout_rate"[^>]*value="6.5%"', section)
    assert re.search(r'id="take_back_legal_costs_usd"[^>]*value="\$5,000"', section)
    assert re.search(r'id="take_back_lost_interest_months"[^>]*value="3"', section)
    # the two buttons
    assert ">Save &amp; Run</button>" in section
    assert f'href="/queue/deals/{deal.id}/assumptions/reset"' in section
    assert ">Reset all to defaults</a>" in section
    # the court search is its own collapsed section below, with its own Save
    assert '<details class="panel" id="court">' in body
    court = body[body.index('id="court"') :]
    assert f'action="/queue/deals/{deal.id}/court"' in court
    assert 'name="court_records_status"' in court and 'name="court_records_status"' not in section


def test_a_typed_over_assumption_is_tagged_as_the_teams_on_the_page(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    save_assumptions(
        db_session,
        deal_with_overrides.id,
        UnderwritingAssumptions(
            interest_rate=D("0.13"),
            estimated_sale_price_team=D("200000.00"),
            monthly_rent=D("1800.00"),
            rental_takeout_rate=D("0.07"),
            flip_analysis=False,
        ),
        actor=ACTOR,
    )
    db_session.commit()
    section = panel(client, deal_with_overrides)
    assert tag(section, "rental_takeout_rate") == "hidden"
    assert re.search(r'id="rental_takeout_rate"[^>]*value="7%"', section)
    assert tag(section, "broker_selling_pct") == "shown"
    assert tag(section, "flip_analysis") == "hidden"
    assert re.search(r'name="flip_analysis" value="false"\s+checked', section)
    # and the readiness checklist agrees about where each came from
    rows = {row.key: row for row in underwrite_readiness(deal_with_overrides).rows}
    assert rows["rental_takeout_rate"].source is InputSource.TEAM
    assert rows["broker_selling_pct"].source is InputSource.DEFAULT


def test_the_readiness_checklist_lists_the_five_at_their_config_values(
    deal_with_overrides: Deal,
) -> None:
    rows = {row.key: row for row in underwrite_readiness(deal_with_overrides).rows}
    for name in ASSUMPTIONS:
        assert rows[name].source is InputSource.DEFAULT, name
        assert rows[name].required is False, name
    assert rows["broker_selling_pct"].value == CONFIG.fees.broker_selling_pct
    assert rows["rental_expenses_pct_of_rent"].value == CONFIG.rental.expenses_pct_of_rent
    assert rows["rental_takeout_rate"].value == CONFIG.rental.takeout_rate
    assert rows["take_back_legal_costs_usd"].value == CONFIG.take_back.legal_costs_usd
    assert rows["take_back_lost_interest_months"].value == CONFIG.take_back.lost_interest_months
    assert rows["take_back_lost_interest_months"].fmt == "plain"


# --- each override changes the engine result (SPEC §8.4-§8.6) -------------------------------------


@pytest.mark.parametrize(("name", "typed", "stored", "figure", "downstream"), OVERRIDES, ids=NAMES)
def test_each_override_reaches_the_engine_and_moves_the_result(
    deal_with_overrides: Deal,
    name: str,
    typed: str,
    stored: Decimal | int,
    figure: Figure,
    downstream: Figure,
) -> None:
    on_config = underwrite(underwrite_inputs(deal_with_overrides, UnderwriteRequest()), CONFIG)
    setattr(deal_with_overrides, name, stored)
    inputs = underwrite_inputs(deal_with_overrides, UnderwriteRequest())
    assert getattr(inputs, name) == stored
    own = underwrite(inputs, CONFIG)
    assert figure(own) == stored and figure(on_config) != stored
    assert downstream(own) != downstream(on_config), f"{name} moved nothing downstream"
    # the ledger reads none of the five
    assert own.return_overview == on_config.return_overview
    assert own.economics == on_config.economics


@pytest.mark.parametrize(("name", "typed", "stored", "figure", "downstream"), OVERRIDES, ids=NAMES)
def test_save_and_run_stores_each_override_and_prices_on_it_as_the_person(
    client: QueueClient,
    db_session: Session,
    deal_with_overrides: Deal,
    name: str,
    typed: str,
    stored: Decimal | int,
    figure: Figure,
    downstream: Figure,
) -> None:
    response = post(client, deal_with_overrides, **{name: typed})
    assert response.status_code == 303, response.text
    assert notice(response).startswith("Assumptions saved. Analysis recorded: GO at an IRR of ")
    db_session.expire_all()
    deal = db_session.get(Deal, deal_with_overrides.id)
    assert deal is not None
    assert getattr(deal, name) == stored
    assert not is_defaulted(deal, name)
    row = latest_underwrite(db_session, deal.id)
    assert row is not None
    assert figure(underwrite_result(row)) == stored
    assert Decimal(str(row.inputs[name])) == Decimal(str(stored))
    assert [log.actor for log in audit(db_session, deal, AuditAction.UNDERWRITE_RUN)] == [
        USER_EMAIL
    ]


def test_a_blank_on_a_defaulted_box_is_the_default_again(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    assert post(client, deal_with_overrides, broker_selling_pct="5%").status_code == 303
    db_session.expire_all()
    assert deal_with_overrides.broker_selling_pct == D("0.05")
    body = {
        name: value
        for name, value in assumptions_form(deal_with_overrides).items()
        if value and name != "broker_selling_pct"
    }
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions", data=body, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    db_session.expire_all()
    assert deal_with_overrides.broker_selling_pct == CONFIG.fees.broker_selling_pct
    assert is_defaulted(deal_with_overrides, "broker_selling_pct")


# --- Reset all to defaults ------------------------------------------------------------------------


def typed_over(session: Session, deal: Deal) -> None:
    """Every resettable box typed over, plus the sale price and the rent, which are not."""
    save_assumptions(
        session,
        deal.id,
        UnderwritingAssumptions(
            interest_rate=D("0.14"),
            origination_fee_pct=D("0.03"),
            contingency_pct=D("0.10"),
            closing_costs_usd=D("2500.00"),
            holding_costs_pct_of_cost=D("0.04"),
            loan_purchase_portion=D("150000.00"),
            loan_rehab_portion=D("45000.00"),
            estimated_sale_price_team=D("310000.00"),
            monthly_rent=D("2400.00"),
            broker_selling_pct=D("0.05"),
            rental_expenses_pct_of_rent=D("0.40"),
            rental_takeout_rate=D("0.07"),
            take_back_legal_costs_usd=D("7500.00"),
            take_back_lost_interest_months=4,
            flip_analysis=False,
            rental_analysis=True,
        ),
        actor=ACTOR,
    )
    session.commit()


def test_reset_all_restores_the_config_values_and_runs_again(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)  # a SPLIT_DRAW: the split resets too
    typed_over(db_session, deal)
    assert deal.defaulted_fields == []

    # the confirmation page first, saying what will move
    page = client.get(f"/queue/deals/{deal.id}/assumptions/reset")
    assert page.status_code == 200
    assert "Reset every assumption to its default?" in page.text
    assert f'action="/queue/deals/{deal.id}/assumptions/reset"' in page.text
    assert 'name="confirm" value="yes"' in page.text
    for label in ("Interest Rate", "Rental Takeout Rate", "Take-Back Lost Interest Months"):
        assert label in page.text, label
    assert "14%" in page.text and "12%" in page.text  # now, and the default

    # a post without the confirmation is the page again, and nothing moves
    refused = client.post(f"/queue/deals/{deal.id}/assumptions/reset", data={})
    assert refused.status_code == 422
    db_session.expire_all()
    assert deal.interest_rate == D("0.14")

    response = client.post(
        f"/queue/deals/{deal.id}/assumptions/reset", data={"confirm": "yes"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    assert notice(response).startswith(
        "Assumptions reset to the defaults. Analysis recorded: GO at an IRR of "
    )
    db_session.expire_all()
    deal = db_session.get(Deal, deal.id)  # type: ignore[assignment]
    assert deal is not None
    assert deal.interest_rate == CONFIG.interest.default_annual_rate
    assert deal.origination_fee_pct == CONFIG.fees.origination_default_pct
    assert deal.contingency_pct == CONFIG.fees.contingency_default_pct
    assert deal.closing_costs_usd == CONFIG.fees.closing_costs_default_usd
    assert deal.holding_costs_pct_of_cost == CONFIG.fees.holding_costs_default_pct_of_cost
    assert deal.broker_selling_pct == CONFIG.fees.broker_selling_pct
    assert deal.rental_expenses_pct_of_rent == CONFIG.rental.expenses_pct_of_rent
    assert deal.rental_takeout_rate == CONFIG.rental.takeout_rate
    assert deal.take_back_legal_costs_usd == CONFIG.take_back.legal_costs_usd
    assert deal.take_back_lost_interest_months == CONFIG.take_back.lost_interest_months
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (D("147000.00"), D("48000.00"))
    assert deal.flip_analysis is None and deal.rental_analysis is None
    for name in (*FLAT, "loan_purchase_portion", "loan_rehab_portion"):
        assert is_defaulted(deal, name), name
    # the sale price and the rent have no default and stay
    assert deal.estimated_sale_price_team == D("310000.00")
    assert deal.monthly_rent == D("2400.00")
    # ...and it ran again, as the person, on the defaults
    runs = audit(db_session, deal, AuditAction.UNDERWRITE_RUN)
    assert [log.actor for log in runs] == [USER_EMAIL]
    row = latest_underwrite(db_session, deal.id)
    assert row is not None
    priced = underwrite_result(row)
    assert priced.flip.broker_selling_pct == CONFIG.fees.broker_selling_pct
    assert priced.take_back.lost_interest_months == CONFIG.take_back.lost_interest_months
    assert priced.analyses.flip_analysis is True  # the default, now that the toggle is back
    # the page says every one is the default again
    section = panel(client, deal)
    for name in (*FLAT, "loan_purchase_portion", "loan_rehab_portion", "flip_analysis"):
        assert tag(section, name) == "shown", name


def test_reset_all_on_a_deal_already_on_its_defaults_writes_no_reset_row(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    """The Tulsa deal's 13% rate is the team's, so the first reset moves it; the second
    finds every box on its default and records nothing - but still runs, as the button says."""
    for _ in range(2):
        response = client.post(
            f"/queue/deals/{deal_with_overrides.id}/assumptions/reset",
            data={"confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    (reset,) = audit(db_session, deal_with_overrides, AuditAction.ASSUMPTIONS_RESET)
    assert reset.after is not None and set(reset.after) == {"interest_rate"}
    assert len(audit(db_session, deal_with_overrides, AuditAction.UNDERWRITE_RUN)) == 2


# --- the audit rows name the changed fields (SPEC §11) --------------------------------------------


def test_the_audit_row_names_exactly_the_fields_that_moved(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    response = post(
        client, deal_with_overrides, rental_expenses_pct_of_rent="40%", monthly_rent="$2,400"
    )
    assert response.status_code == 303, response.text
    (saved,) = audit(db_session, deal_with_overrides, AuditAction.ASSUMPTIONS_SAVED)
    assert saved.actor == USER_EMAIL
    assert saved.after is not None and saved.before is not None
    assert set(saved.after) == {"rental_expenses_pct_of_rent", "monthly_rent"}
    assert set(saved.before) == {"rental_expenses_pct_of_rent", "monthly_rent"}
    assert Decimal(saved.after["rental_expenses_pct_of_rent"]) == D("0.40")
    assert Decimal(saved.before["rental_expenses_pct_of_rent"]) == D("0.35")
    assert Decimal(saved.after["monthly_rent"]) == D("2400")
    assert "broker_selling_pct" not in saved.after and "interest_rate" not in saved.after
    # the same panel posted again moves nothing and writes nothing
    db_session.expire_all()
    again = post(client, deal_with_overrides)
    assert again.status_code == 303
    assert len(audit(db_session, deal_with_overrides, AuditAction.ASSUMPTIONS_SAVED)) == 1
    # the page's trail names the fields
    body = client.get(f"/queue/deals/{deal_with_overrides.id}").text
    trail = body[body.index("<h3>Actions and notes</h3>") :]
    assert "ASSUMPTIONS_SAVED" in trail
    assert "monthly_rent, rental_expenses_pct_of_rent" in trail  # in the panel's own order


def test_the_reset_row_names_what_went_back_and_the_default_it_landed_on(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    save_assumptions(
        db_session,
        deal_with_overrides.id,
        UnderwritingAssumptions(
            interest_rate=D("0.13"),
            estimated_sale_price_team=D("200000.00"),
            monthly_rent=D("1800.00"),
            take_back_legal_costs_usd=D("7500.00"),
            rental_analysis=False,
        ),
        actor=ACTOR,
    )
    db_session.commit()
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/assumptions/reset",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    (reset,) = audit(db_session, deal_with_overrides, AuditAction.ASSUMPTIONS_RESET)
    assert reset.actor == USER_EMAIL
    assert reset.after is not None and reset.before is not None
    assert set(reset.after) == {"interest_rate", "take_back_legal_costs_usd", "rental_analysis"}
    assert Decimal(reset.before["take_back_legal_costs_usd"]) == D("7500")
    assert Decimal(reset.after["take_back_legal_costs_usd"]) == CONFIG.take_back.legal_costs_usd
    assert reset.before["rental_analysis"] is False and reset.after["rental_analysis"] is None
    assert "estimated_sale_price_team" not in reset.after
    assert "monthly_rent" not in reset.after


def test_the_court_search_row_names_its_own_fields_and_nothing_of_the_panels(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    response = client.post(
        f"/queue/deals/{deal_with_overrides.id}/court",
        data={"court_records_status": "NOT_CHECKED"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    (saved,) = audit(db_session, deal_with_overrides, AuditAction.COURT_SEARCH_SAVED)
    assert saved.after is not None
    assert set(saved.after) == {"court_records_status", "court_records_as_of"}
    assert saved.after["court_records_status"] == "NOT_CHECKED"
    assert saved.after["court_records_as_of"] is None
    assert audit(db_session, deal_with_overrides, AuditAction.ASSUMPTIONS_SAVED) == []
    db_session.expire_all()
    assert deal_with_overrides.estimated_sale_price_team == D("200000.00")


# --- a deal from before the columns existed -------------------------------------------------------


def test_an_older_deal_is_populated_with_the_five_on_its_next_open(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    for name in ASSUMPTIONS:
        setattr(deal_with_overrides, name, None)
    deal_with_overrides.defaulted_fields = [
        name for name in deal_with_overrides.defaulted_fields if name not in ASSUMPTIONS
    ]
    db_session.commit()
    assert client.get(f"/queue/deals/{deal_with_overrides.id}").status_code == 200
    db_session.expire_all()
    for name in ASSUMPTIONS:
        assert getattr(deal_with_overrides, name) is not None, name
        assert is_defaulted(deal_with_overrides, name), name
    (populated,) = audit(db_session, deal_with_overrides, AuditAction.DEFAULTS_POPULATED)
    assert populated.after is not None and set(populated.after) == set(ASSUMPTIONS)


def test_the_underwrite_count_is_one_per_save_and_run(
    client: QueueClient, db_session: Session, deal_with_overrides: Deal
) -> None:
    assert post(client, deal_with_overrides, rental_takeout_rate="7%").status_code == 303
    assert post(client, deal_with_overrides, rental_takeout_rate="7.5%").status_code == 303
    rows = list(db_session.scalars(select(Underwrite).order_by(Underwrite.created_at)))
    assert [Decimal(str(row.inputs["rental_takeout_rate"])) for row in rows] == [
        D("0.07"),
        D("0.075"),
    ]
