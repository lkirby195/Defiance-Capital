"""The SPEC §8.1 defaults every deal is populated with, and how the page shows them.

# SPEC §8.1, §8.2, §9.2

Six inputs have a stand-in - the rate, the four fees and, on a split product, the loan split
by the §8.2 formula - and ``services/defaults.py`` writes it onto the deal at intake, tags
it on ``deals.defaulted_fields``, and fills any blank the next time the deal is opened. What
is tested: the formula, the population on every channel, the idempotent call and its audit
row, that a team value is never touched, that a value equal to the default is the default,
that the readiness checklist calls a default present, and that the page tags and resets it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config
from db.models import AuditLog, Deal
from db.repository import create_deal_from_intake
from intake.normalize import normalize
from intake.parsers.team_form import TeamEntryForm, parse_team_form
from schema.models import AuditAction, Channel, Product
from services import (
    DEALS,
    InputSource,
    TeamOverrides,
    apply_defaults,
    default_loan_split,
    defaults_for,
    is_defaulted,
    populate,
    save_overrides,
    underwrite_readiness,
)
from services.assemble import screen_inputs
from tests.conftest import ACTOR, QueueClient, requires_db, store_deal

pytestmark = requires_db

D = Decimal
CONFIG = Config.load()
ECONOMICS = (
    "interest_rate",
    "contingency_pct",
    "closing_costs_usd",
    "holding_costs_pct_of_cost",
    "origination_fee_pct",
)


def unpopulated(session: Session, payload: dict[str, Any]) -> Deal:
    """A deal stored the way deals were before the defaults existed: NULL where nobody typed."""
    record = normalize(parse_team_form(TeamEntryForm(**payload)), Channel.TEAM, raw_payload=payload)
    deal = create_deal_from_intake(session, record)
    session.commit()
    return deal


def audit(session: Session, deal: Deal, action: AuditAction) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.table_name == DEALS,
                AuditLog.row_id == str(deal.id),
                AuditLog.action == action.value,
            )
        )
    )


# --- the formula ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "loan", "rehab", "contingency", "expected"),
    [
        # rehab_adj = 48,000 x 1.00 = 48,000 <= 195,000: the rehab portion is the budget
        (Product.SPLIT_DRAW, "195000.00", "48000.00", None, ("147000.00", "48000.00")),
        # ...with a 10% contingency the budget is 52,800
        (Product.SPLIT_PRINCIPAL, "195000.00", "48000.00", "0.10", ("142200.00", "52800.00")),
        # a budget past the loan is capped at the loan, and nothing is left for closing
        (Product.SPLIT_DRAW, "40000.00", "48000.00", None, ("0.00", "40000.00")),
        # no rehab: the whole loan at closing
        (Product.SPLIT_DRAW, "100000.00", "0.00", None, ("100000.00", "0.00")),
        # a contingency that leaves a fraction of a cent is rounded to the cent, half up:
        # 10,001 x 1.055 = 10,551.055
        (Product.SPLIT_DRAW, "50000.00", "10001.00", "0.055", ("39448.94", "10551.06")),
        # single-note products carry no split (SPEC §8.2)
        (Product.NO_DRAW, "100000.00", "0.00", None, None),
        (Product.WHOLETAIL, "100000.00", "5000.00", None, None),
        # and no formula on a blank
        (Product.SPLIT_DRAW, None, "48000.00", None, None),
        (Product.SPLIT_DRAW, "195000.00", None, None, None),
        (None, "195000.00", "48000.00", None, None),
    ],
)
def test_the_default_split_is_the_spec_8_2_formula(
    product: Product | None,
    loan: str | None,
    rehab: str | None,
    contingency: str | None,
    expected: tuple[str, str] | None,
) -> None:
    got = default_loan_split(
        product,
        None if loan is None else D(loan),
        None if rehab is None else D(rehab),
        None if contingency is None else D(contingency),
        CONFIG,
    )
    if expected is None:
        assert got is None
    else:
        assert got == (D(expected[0]), D(expected[1]))
        assert sum(got) == D(loan)  # type: ignore[arg-type]


def test_the_contingency_in_force_is_the_deals_own_else_the_configs() -> None:
    """The split's formula reads the contingency the deal will be sized on (SPEC §8.2)."""
    own = default_loan_split(Product.SPLIT_DRAW, D("195000"), D("48000"), D("0.05"), CONFIG)
    config = default_loan_split(Product.SPLIT_DRAW, D("195000"), D("48000"), None, CONFIG)
    assert own is not None and own[1] == D("50400.00")
    assert config is not None and config[1] == D("48000.00")  # the placeholder is 0%


# --- population --------------------------------------------------------------------------------


def test_a_team_entry_without_the_economics_is_populated_and_tagged(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    for name in ("interest_rate", "closing_costs_usd", "holding_costs_pct_of_cost"):
        del team_entry[name]
    deal = store_deal(db_session, team_entry)
    assert deal.interest_rate == D("0.12")
    assert deal.closing_costs_usd == D("1000.00")
    assert deal.holding_costs_pct_of_cost == D("0.02")
    assert deal.contingency_pct == D("0") and deal.origination_fee_pct == D("0.02")
    assert set(ECONOMICS) <= set(deal.defaulted_fields)
    # the split the fixture typed is left exactly where it was - and because 147,000 /
    # 48,000 is what the §8.2 formula gives this deal, it is the default by the same rule
    # that makes a 12% rate nobody changed the default
    assert deal.loan_purchase_portion == D("147000.00")
    assert is_defaulted(deal, "loan_purchase_portion")


def test_a_team_value_is_never_touched_and_never_called_a_default(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)  # 12%, $1,500 closing, 3% holding
    assert deal.interest_rate == D("0.12") and "interest_rate" in deal.defaulted_fields, (
        "12% is the config default, so a 12% nobody chose differently is the default"
    )
    assert deal.closing_costs_usd == D("1500.00")
    assert deal.holding_costs_pct_of_cost == D("0.03")
    assert "closing_costs_usd" not in deal.defaulted_fields
    assert "holding_costs_pct_of_cost" not in deal.defaulted_fields
    assert not is_defaulted(deal, "closing_costs_usd")


def test_a_value_equal_to_the_default_is_the_default(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """A person who leaves a box holding the number it was pre-filled with has not chosen it."""
    deal = store_deal(db_session, {**team_entry, "closing_costs_usd": "1000.00"})
    assert deal.closing_costs_usd == D("1000.00")
    assert is_defaulted(deal, "closing_costs_usd")


def test_populate_is_idempotent(db_session: Session, team_entry: dict[str, Any]) -> None:
    deal = store_deal(db_session, team_entry)
    before = {name: getattr(deal, name) for name in ECONOMICS}
    assert populate(deal, CONFIG) == {}
    assert {name: getattr(deal, name) for name in ECONOMICS} == before


def test_an_existing_deal_is_populated_on_its_next_open_and_only_once(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    """Stored before the defaults existed: NULL where nobody typed, no tag at all."""
    del team_entry["interest_rate"]
    deal = unpopulated(db_session, team_entry)
    assert deal.interest_rate is None and deal.contingency_pct is None
    assert deal.defaulted_fields == []
    # ...but the checklist already reads a NULL as the default the engine will use
    rows = {row.key: row for row in underwrite_readiness(deal).rows}
    assert rows["deal.interest_rate"].source is InputSource.DEFAULT
    assert rows["deal.interest_rate"].value == D("0.12")

    assert client.get(f"/queue/deals/{deal.id}").status_code == 200
    db_session.expire_all()
    opened = db_session.get(Deal, deal.id)
    assert opened is not None
    assert opened.interest_rate == D("0.12") and opened.contingency_pct == D("0")
    assert set(opened.defaulted_fields) == {
        "interest_rate",
        "contingency_pct",
        "origination_fee_pct",
        # the fixture's split is the formula's own numbers (see above)
        "loan_purchase_portion",
        "loan_rehab_portion",
    }
    # the team's own numbers were not migrated, and are not called defaults
    assert opened.closing_costs_usd == D("1500.00")
    assert opened.holding_costs_pct_of_cost == D("0.03")
    (row,) = audit(db_session, opened, AuditAction.DEFAULTS_POPULATED)
    assert row.actor == "system"
    assert row.after is not None and set(row.after) == {
        "interest_rate",
        "contingency_pct",
        "origination_fee_pct",
    }

    assert client.get(f"/queue/deals/{deal.id}").status_code == 200
    db_session.expire_all()
    assert len(audit(db_session, opened, AuditAction.DEFAULTS_POPULATED)) == 1, "written twice"


def test_apply_defaults_writes_nothing_on_a_populated_deal(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)
    assert apply_defaults(db_session, deal.id, actor=ACTOR) == {}
    assert audit(db_session, deal, AuditAction.DEFAULTS_POPULATED) == []


def test_an_unpopulated_split_deal_is_sized_on_the_formula_at_run_time(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The run reads the same default the population would write (SPEC §8.2)."""
    deal = unpopulated(
        db_session, {**team_entry, "loan_purchase_portion": None, "loan_rehab_portion": None}
    )
    assert deal.loan_purchase_portion is None
    assembled = screen_inputs(deal, config=CONFIG)
    assert assembled.deal.loan_split == (D("147000.00"), D("48000.00"))
    rows = {row.key: row for row in underwrite_readiness(deal).rows}
    assert rows["deal.loan_rehab_portion"].source is InputSource.DEFAULT
    assert rows["deal.loan_rehab_portion"].value == D("48000.00")
    assert rows["deal.loan_rehab_portion"].required is True
    assert underwrite_readiness(deal).ready is True


# --- the override block: a default is editable and resettable ------------------------------


def test_typing_over_a_default_makes_it_the_teams_and_a_blank_puts_it_back(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, {**team_entry, "closing_costs_usd": None})
    assert is_defaulted(deal, "closing_costs_usd")

    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(closing_costs_usd=D("2500.00"), interest_rate=deal.interest_rate),
        actor=ACTOR,
    )
    db_session.commit()
    assert deal.closing_costs_usd == D("2500.00")
    assert not is_defaulted(deal, "closing_costs_usd")

    # the block posts every box; a blank one on a defaulted economic is the default again
    save_overrides(db_session, deal.id, TeamOverrides(), actor=ACTOR)
    db_session.commit()
    assert deal.closing_costs_usd == D("1000.00")
    assert is_defaulted(deal, "closing_costs_usd")
    assert deal.interest_rate == D("0.12") and is_defaulted(deal, "interest_rate")
    (first, second) = audit(db_session, deal, AuditAction.OVERRIDES_SAVED)
    assert first.after is not None and first.after["closing_costs_usd"] == "2500.00"
    assert second.after is not None and second.after["closing_costs_usd"] == "1000.00"


def test_a_blank_split_on_the_block_is_the_default_split_again(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)  # a SPLIT_DRAW with a typed 147,000 / 48,000
    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(loan_purchase_portion=D("150000.00"), loan_rehab_portion=D("45000.00")),
        actor=ACTOR,
    )
    db_session.commit()
    assert deal.loan_purchase_portion == D("150000.00")
    assert not is_defaulted(deal, "loan_rehab_portion")

    save_overrides(db_session, deal.id, TeamOverrides(), actor=ACTOR)
    db_session.commit()
    assert (deal.loan_purchase_portion, deal.loan_rehab_portion) == (D("147000.00"), D("48000.00"))
    assert is_defaulted(deal, "loan_rehab_portion")


def test_a_split_that_does_not_add_up_is_refused_on_the_block(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(db_session, team_entry)
    with pytest.raises(ValueError, match="must add up to the loan requested"):
        save_overrides(
            db_session,
            deal.id,
            TeamOverrides(loan_purchase_portion=D("1.00"), loan_rehab_portion=D("2.00")),
            actor=ACTOR,
        )
    db_session.rollback()


def test_moving_off_a_split_product_drops_the_posted_split(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    """The block's split boxes post along with a product change; a WHOLETAIL has no split."""
    deal = store_deal(db_session, team_entry)
    save_overrides(
        db_session,
        deal.id,
        TeamOverrides(
            product=Product.WHOLETAIL,
            loan_purchase_portion=D("147000.00"),
            loan_rehab_portion=D("48000.00"),
        ),
        actor=ACTOR,
    )
    db_session.commit()
    assert deal.product is Product.WHOLETAIL
    assert deal.loan_purchase_portion is None and deal.loan_rehab_portion is None
    assert "loan_purchase_portion" not in deal.defaulted_fields


def test_the_defaults_for_a_single_note_product_carry_no_split(
    db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(
        db_session,
        {
            **team_entry,
            "rehab_costs": "0.00",
            "loan_purchase_portion": None,
            "loan_rehab_portion": None,
        },
    )
    assert deal.product is Product.NO_DRAW
    assert "loan_purchase_portion" not in defaults_for(deal, CONFIG)
    assert deal.loan_purchase_portion is None


# --- the page -------------------------------------------------------------------------------------


def test_the_deal_page_tags_each_default_and_offers_a_reset(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    del team_entry["interest_rate"]
    deal = store_deal(
        db_session, {**team_entry, "loan_purchase_portion": None, "loan_rehab_portion": None}
    )
    body = client.get(f"/queue/deals/{deal.id}").text
    # the facts: the value, then the tag, for every default; none on the team's own numbers
    for label, value in (
        ("Interest Rate", "12.0%"),
        ("Contingency", "0.0%"),
        ("Origination Fee", "2.0%"),
        ("Advance at closing", "$147,000.00"),
        ("Rehab portion", "$48,000.00"),
    ):
        row = re.search(rf"<dt>{label}.*?</dt>\s*<dd>{re.escape(value)}(.*?)</dd>", body, re.S)
        assert row is not None, label
        assert 'class="dflt">default</span>' in row.group(1), f"{label} is not tagged"
    closing = re.search(r"<dt>Closing Costs.*?</dt>\s*<dd>\$1,500\.00(.*?)</dd>", body, re.S)
    assert closing is not None and "default" not in closing.group(1)
    # the override block: pre-filled, tagged, and resettable, the split boxes included
    for name in ("interest_rate", "loan_purchase_portion", "loan_rehab_portion"):
        assert re.search(rf'id="{name}"[^>]*data-default="[^"]+"', body), name
        assert f'data-default-tag="{name}"' in body, name
        assert f'data-reset="{name}"' in body, name
    assert re.search(r'id="interest_rate"[^>]*value="12%"', body)
    assert re.search(r'id="loan_rehab_portion"[^>]*value="\$48,000"', body)
    # and the checklist calls every one of them present
    assert "Run underwrite is off until these are entered" not in body
    assert "disabled>Run underwrite" not in body


def test_a_single_note_deal_shows_the_whole_loan_at_closing(
    client: QueueClient, db_session: Session, team_entry: dict[str, Any]
) -> None:
    deal = store_deal(
        db_session,
        {
            **team_entry,
            "product": "WHOLETAIL",
            "loan_purchase_portion": None,
            "loan_rehab_portion": None,
        },
    )
    body = client.get(f"/queue/deals/{deal.id}").text
    assert re.search(r"<dt>Advance at closing.*?</dt>\s*<dd>\$195,000\.00", body, re.S)
    assert re.search(r"<dt>Rehab portion.*?</dt>\s*<dd>\$0\.00", body, re.S)
    assert 'name="loan_purchase_portion"' not in body, "no split box on a single-note loan"
