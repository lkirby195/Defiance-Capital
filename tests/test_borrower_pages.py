"""The borrower's pages after the thank-you (SPEC §4.2, v0.22): the Simple and Advanced views,
Adjust my numbers, the leak guard and the noindex header.

The thank-you page shows every rate cushioned (``tests/test_apply.py`` holds the table to
that); here the Advanced view's two tables are checked against the engine's quote, an edit
from the adjust page is followed through the deal - a new submission, a version named
"Borrower edit 1", the page reading it, the record and the trail the team sees - and every
borrower-facing page of a fully analyzed deal is rendered and searched for anything that is
the lender's: the IRR, the verdict, the flags, the sensitivity, the pricing target, the
maximum quoted rate, the cushion, the assumptions, the take-back and rental figures, and
the team's names.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.apply_form import ADJUST_FIELDS, HONEYPOT
from api.intake_form import intake_form_values
from api.masks import mask_one
from api.render import dollars, pct, quoted
from config.config import get_config
from db.models import AuditLog, Deal, Version
from schema.masks import money_display
from schema.models import AuditAction, Channel, LoanPurpose, Status, TermBucket, Verdict
from services import deal_history, decline, version_results
from tests.conftest import ACTOR, USER_EMAIL, USER_NAME, QueueClient, requires_db
from tests.test_apply import (
    arrival,
    between,
    controls,
    deals,
    post,
    quote_for,
    shown_main,
    terms_table,
    thanks_link,
)
from tests.test_form_errors import field_error

pytestmark = requires_db

ADJUST = {
    "lang": "en",
    "estimated_sale_price": "$300,000",
    "rehab_costs": "$45,000",
    "loan_requested": "$150,000",
    "monthly_rent": "",
    "term_bucket": "9",
}


@pytest.fixture(autouse=True)
def fresh_limiter() -> Any:
    from api.ratelimit import LIMITER

    LIMITER.reset()
    yield
    LIMITER.reset()


def token_base(link: str) -> str:
    return link.split("?")[0]


def adjust(client: QueueClient, base: str, **changes: str) -> Any:
    return client.post(f"{base}/adjust", data={**ADJUST, **changes}, follow_redirects=False)


def versions(session: Session, deal: Deal) -> list[Version]:
    return list(
        session.scalars(select(Version).where(Version.deal_id == deal.id).order_by(Version.number))
    )


def shown_rows(session: Session, deal: Deal) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.row_id == str(deal.id),
                AuditLog.action == AuditAction.INDICATIVE_TERMS_SHOWN.value,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


# --- Simple and Advanced --------------------------------------------------------------------------


def test_simple_is_the_default_and_advanced_adds_the_two_tables(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    quote = quote_for(db_session, deal)
    assert quote.cash_flows is not None and quote.sources_and_uses is not None

    simple = anon_client.get(link).text
    assert terms_table(simple) is not None
    assert "Your cash flows" not in simple and "Sources and uses" not in simple
    # the two views are links on the page, the current one marked, both keeping the token
    assert f'href="{base}?lang=en"' in simple
    assert f'href="{base}?lang=en&amp;view=advanced"' in simple
    views = between(simple, '<nav class="views"', "</nav>")
    assert views is not None and 'aria-current="page"' in views
    assert views.index("Simple") < views.index("Advanced")

    advanced = anon_client.get(f"{base}?lang=en&view=advanced").text
    assert terms_table(advanced) is not None, "the table is still on the Advanced view"
    block = between(
        advanced, '<div id="borrower-advanced">', '<p><a class="btn quiet" id="adjust-link"'
    )
    assert block is not None
    first = quote.rows[0]
    assert first.is_request
    assert f"for a {dollars(first.loan_amount)} loan at {quoted(first.borrower_rate)}." in block
    # the cash flows: one row per ledger period, the totals row adding up to the profit shown
    flows = between(block, '<div class="chart" id="borrower-cash-flows">', "</table>")
    assert flows is not None
    months = re.findall(r'<th scope="row">(\d+)</th>', flows)
    assert [int(m) for m in months] == [row.month for row in quote.cash_flows]
    totals = between(flows, '<tr id="cash-flow-totals">', "</tr>")
    assert totals is not None
    assert first.profit is not None
    assert dollars(first.profit) in totals, "the net total is the profit on the table above"
    sale = [row.sale for row in quote.cash_flows if row.sale is not None]
    assert len(sale) == 1 and dollars(sale[0]) in totals
    for cell in (
        "Purchase",
        "Rehab",
        "Holding",
        "Financing",
        "Sale",
        "Loan advance",
        "Draws",
        "Payoff",
        "Net cash",
    ):
        assert f">{cell}</th>" in flows
    assert "Purchase includes closing costs" in block
    # the loan is in the borrower's table: the advance in month 0, the payoff - the loan
    # amount - in the last, and the two cancel in the totals row
    first_row = between(flows, "<tbody>", "</tr>")
    assert first_row is not None and dollars(quote.cash_flows[0].advance) in first_row
    assert quote.cash_flows[0].advance > 0
    assert quote.cash_flows[-1].payoff == -first.loan_amount
    assert dollars(quote.cash_flows[-1].payoff) in flows
    loan_columns = sum((r.advance + r.draws + r.payoff for r in quote.cash_flows), Decimal(0))
    assert loan_columns == 0  # the loan's three columns add to nothing
    assert dollars(-first.loan_amount) in totals  # the payoff total
    assert "the net cash column is what you put in or take out each month" in block
    # Sources & Uses, balanced, its equity the borrower's cash at the cushioned rate
    su = quote.sources_and_uses
    uses = between(block, '<div class="chart" id="borrower-sources-uses">', "</table>")
    assert uses is not None
    assert su.total_sources == su.total_uses
    assert uses.count(dollars(su.total_sources)) >= 2
    assert dollars(su.borrower_equity) in uses and dollars(first.cash_invested) in uses
    assert dollars(su.interest) in uses and dollars(su.advance_at_closing) in uses
    for cell in ("Loan advance at closing", "Rehab holdback", "Your equity", "Interest"):
        assert cell in uses
    # the toggle keeps the view
    assert f'href="{base}?lang=es&amp;view=advanced"' in advanced

    # both views are on the record, each recorded the first time it was opened and not again
    db_session.expire_all()
    record = deal.indicative_terms_shown
    assert record is not None and record["views"] == ["simple", "advanced"]
    assert len(shown_rows(db_session, deal)) == 2
    anon_client.get(f"{base}?lang=en&view=advanced")
    anon_client.get(link)
    assert len(shown_rows(db_session, deal)) == 2
    # the team's page says which views the borrower opened
    page_record = anon_client.get(link).text
    assert page_record  # a refresh still renders


def test_the_advanced_view_reads_in_spanish(anon_client: QueueClient, db_session: Session) -> None:
    link = thanks_link(post(anon_client, lang="es"))
    body = anon_client.get(f"{token_base(link)}?lang=es&view=advanced").text
    for text in (
        "Sus flujos de efectivo",
        "Fuentes y usos",
        "Avanzado",
        "Ajustar mis números",
        "Su capital propio",
        "Rendimiento del proyecto",
        "Desembolso del préstamo",
        "Desembolsos de obra",
        "Liquidación del préstamo",
        "Efectivo neto",
    ):
        assert text in body, text
    assert "Your cash flows" not in body and "Advanced" not in body
    assert "Net cash" not in body and "Payoff" not in body


# --- Adjust my numbers ----------------------------------------------------------------------------


def test_the_adjust_page_has_the_five_boxes_holding_the_deals_numbers(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    base = token_base(link)
    thanks = anon_client.get(link).text
    assert f'href="{base}/adjust?lang=en"' in thanks and ">Adjust my numbers</a>" in thanks

    page = anon_client.get(f"{base}/adjust?lang=en")
    assert page.status_code == 200
    boxes = controls(page.text)
    assert set(boxes) == {*ADJUST_FIELDS, "lang", HONEYPOT}
    assert 'value="$290,000"' in page.text and 'value="$40,000"' in page.text
    assert 'value="$160,000"' in page.text and 'value="$1,950"' in page.text
    assert '<option value="12_PLUS" selected>' in page.text
    assert 'action="' + base + '/adjust"' in page.text
    assert "Required" in page.text and "Optional" in page.text
    assert f'href="{base}?lang=en"' in page.text  # back to the terms
    # in Spanish too
    spanish = anon_client.get(f"{base}/adjust?lang=es").text
    assert "Ajustar mis números" in spanish and "Actualizar mis términos" in spanish


def test_a_save_is_a_new_submission_a_borrower_edit_version_and_new_terms(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    anon_client.get(link)  # version 1 shown and recorded
    _, arrival_pricing = arrival(db_session, deal)
    assert arrival_pricing is not None

    response = adjust(anon_client, base)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{base}?lang=en&notice=")

    db_session.expire_all()
    # the five numbers moved, and nothing else on the intake did
    assert deal.estimated_sale_price_borrower == Decimal("300000")
    assert deal.rehab_costs == Decimal("45000")
    assert deal.loan_requested == Decimal("150000")
    assert deal.monthly_rent_borrower is None  # the blank optional box took the rent off
    assert deal.term_bucket is TermBucket.M9 and deal.term_months == 9
    assert deal.purchase_price == Decimal("185000")
    assert deal.borrower is not None and deal.borrower.name == "María Elena Ortiz"
    assert deal.channel is Channel.WEB and deal.intake_source == "tulsa-reia"
    # a new, immutable submission over the first, in the borrower's language
    assert len(deal.submissions) == 2
    payload = deal.submissions[-1].raw_payload
    assert deal.submissions[-1].channel is Channel.WEB
    assert payload["loan_requested"] == "150000" and payload["term_bucket"] == "9"
    assert payload["purchase_price"] == "185000" and payload["language"] == "en"
    assert "monthly_rent" not in payload
    # the run: a version named for the edit, the system's, Working by the usual rule, and the
    # page now reads it
    first, second = versions(db_session, deal)
    assert second.number == 2 and second.name == "Borrower edit 1" and second.actor == "system"
    assert second.is_working and not first.is_working and not second.human_selected
    assert deal.borrower_terms_version == 2
    results = version_results(db_session, second)
    assert results.underwrite is not None and results.underwrite_inputs is not None
    assert results.underwrite_inputs.deal.loan_requested == Decimal("150000")
    assert results.underwrite_inputs.term_months == 9
    # the edit is the borrower's on the trail
    edited = [
        row
        for row in db_session.scalars(select(AuditLog).where(AuditLog.row_id == str(deal.id)))
        if row.action == AuditAction.INTAKE_EDITED.value
    ]
    assert len(edited) == 1 and edited[0].actor == "web:maria@ortizbuilds.example"
    assert edited[0].after is not None and edited[0].after["loan_requested"] == "150000"

    # the thank-you page: the notice, and the new run's terms, re-rendered and recorded
    body = anon_client.get(location).text
    assert "Your numbers were updated." in body
    table = terms_table(body)
    assert table is not None
    quote = quote_for(db_session, deal)
    assert [row.loan_amount for row in quote.rows][0] == Decimal("150000")
    for row in quote.rows:
        assert dollars(row.loan_amount) in table and quoted(row.borrower_rate) in table
    assert dollars(Decimal("160000")) not in table
    db_session.expire_all()
    record = deal.indicative_terms_shown
    assert record is not None and record["version"] == 2 and record["views"] == ["simple"]
    assert [Decimal(row["loan_amount"]) for row in record["rows"]][0] == Decimal("150000")
    assert len(shown_rows(db_session, deal)) == 2  # version 1 once, version 2 once

    # the team sees the trail in History: the version by its name, the edit as the borrower
    page = client.get(f"/queue/deals/{deal.id}").text
    history = between(page, '<section class="panel chart" id="history">', "</section>")
    assert history is not None
    assert "<strong>Borrower edit 1</strong>" in history
    assert "web:maria@ortizbuilds.example" in history
    assert [entry.name for entry in deal_history(db_session, deal.id) if entry.number] == [
        "Borrower edit 1",
        "Ortiz; 1412 S Peoria Ave, OK - Request",
    ]
    shown_block = between(page, '<div id="shown-to-borrower">', "</table>")
    assert shown_block is not None and "<dt>Version</dt><dd>2</dd>" in shown_block

    # a second edit counts up
    assert adjust(anon_client, base, loan_requested="$140,000").status_code == 303
    db_session.expire_all()
    assert [version.name for version in versions(db_session, deal)][-1] == "Borrower edit 2"
    assert deal.borrower_terms_version == 3


def test_a_team_edit_in_between_survives_a_borrower_edit(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    """The borrower moves one box; what the team entered since stays (SPEC §4.2)."""
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    assert deal.loan_purpose is None and deal.closing_date is not None
    arrived_on = deal.closing_date

    # the team edits the closing date and the purpose through Edit Intake
    body = {
        key: value for key, value in intake_form_values(deal).items() if value not in ("", None)
    }
    body.update({"closing_date": "2027-01-15", "loan_purpose": "REFINANCE"})
    response = client.post(f"/queue/deals/{deal.id}/intake", data=body, follow_redirects=False)
    assert response.status_code == 303, response.text
    db_session.expire_all()
    assert deal.closing_date == date(2027, 1, 15) and deal.closing_date != arrived_on
    assert deal.loan_purpose is LoanPurpose.REFINANCE
    assert deal.rehab_costs == Decimal("40000") and deal.loan_requested == Decimal("160000")
    was_sale, was_rent = deal.estimated_sale_price_borrower, deal.monthly_rent_borrower
    assert was_sale == Decimal("290000") and was_rent is not None
    # the team form carries the term in months only, so the edit cleared the bucket
    was_term, was_months = deal.term_bucket, deal.term_months
    assert was_term is None and was_months == 12
    team_versions = len(versions(db_session, deal))

    # the borrower changes the sale price alone; the other four boxes go back as the page
    # filled them, which is as the deal stands
    page = anon_client.get(f"{base}/adjust?lang=en").text
    assert money_display(was_sale) in page and money_display(Decimal(160000)) in page
    assert 'value="12" selected' in page  # the bucket that seeds the deal's twelve months
    boxes = {
        "estimated_sale_price": "$325,000",
        "rehab_costs": money_display(deal.rehab_costs),
        "loan_requested": money_display(deal.loan_requested),
        "monthly_rent": money_display(was_rent),
        "term_bucket": "12",
    }
    response = adjust(anon_client, base, **boxes)
    assert response.status_code == 303, response.text
    db_session.expire_all()

    # both survive: the sale price is the borrower's new one, the date and the purpose the
    # team's, and nothing else moved
    assert deal.estimated_sale_price_borrower == Decimal("325000")
    assert deal.closing_date == date(2027, 1, 15)
    assert deal.loan_purpose is LoanPurpose.REFINANCE
    assert deal.rehab_costs == Decimal("40000") and deal.loan_requested == Decimal("160000")
    assert deal.monthly_rent_borrower == was_rent
    assert deal.term_bucket is was_term and deal.term_months == was_months
    edited = [
        row
        for row in db_session.scalars(
            select(AuditLog)
            .where(AuditLog.row_id == str(deal.id))
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        if row.action == AuditAction.INTAKE_EDITED.value
    ]
    assert [row.actor for row in edited] == [USER_EMAIL, "web:maria@ortizbuilds.example"]
    after = dict(edited[-1].after or {})
    after.pop("submission_id")
    assert after == {"estimated_sale_price_borrower": "325000"}
    assert edited[-1].before == {"estimated_sale_price_borrower": "290000.00"}
    # the run read the team's date and the borrower's price
    (latest,) = versions(db_session, deal)[team_versions:]
    assert latest.name == "Borrower edit 1"
    results = version_results(db_session, latest)
    assert results.underwrite_inputs is not None
    assert results.underwrite_inputs.closing_date == date(2027, 1, 15)
    assert deal.borrower_terms_version == latest.number

    # a box sent back unchanged is no edit: the second save moves nothing but the submission
    assert adjust(anon_client, base, **boxes).status_code == 303
    db_session.expire_all()
    rows = [
        row
        for row in db_session.scalars(select(AuditLog).where(AuditLog.row_id == str(deal.id)))
        if row.action == AuditAction.INTAKE_EDITED.value
    ]
    assert len(rows) == 3
    assert set(rows[-1].after or {}) == {"submission_id"} and not rows[-1].before


def test_a_bad_box_is_refused_in_place_and_nothing_is_saved(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    response = adjust(anon_client, base, loan_requested="", rehab_costs="-5", term_bucket="99")
    assert response.status_code == 422
    assert field_error(response.text, "loan_requested") == "This is required."
    assert field_error(response.text, "rehab_costs") == "Please enter an amount greater than zero."
    assert field_error(response.text, "term_bucket") == "Please choose one of the options."
    assert 'value="$300,000"' in response.text  # what was typed comes back
    db_session.expire_all()
    assert len(deal.submissions) == 1 and deal.loan_requested == Decimal("160000")
    assert len(versions(db_session, deal)) == 1
    # a Spanish complaint on a Spanish page
    spanish = adjust(anon_client, base, lang="es", loan_requested="")
    assert field_error(spanish.text, "loan_requested") == "Este campo es obligatorio."


def test_the_adjust_page_is_rate_limited_like_the_form_and_has_the_honeypot(
    anon_client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))  # attempt 1 on this address
    (deal,) = deals(db_session)
    base = token_base(link)
    limit = get_config().web_intake.submissions_per_hour_per_ip
    assert limit == 5
    # the honeypot drops the post without a word or a row
    dropped = adjust(anon_client, base, **{HONEYPOT: "http://spam.example"})
    assert dropped.status_code == 303 and dropped.headers["location"] == f"{base}?lang=en"
    db_session.expire_all()
    assert len(deal.submissions) == 1
    # the bot's post counted (2); three more honest ones fit, the sixth is turned away
    for _ in range(3):
        assert adjust(anon_client, base).status_code == 303
    refused = adjust(anon_client, base)
    assert refused.status_code == 429
    assert "We have received several requests from your connection" in refused.text
    db_session.expire_all()
    assert len(deal.submissions) == 4


def test_an_unknown_token_and_a_closed_deal(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    unknown = anon_client.get("/apply/thanks/no-such-token/adjust")
    assert unknown.status_code == 200
    assert "A member of our team will be in touch soon" in unknown.text
    assert "<form" not in shown_main(unknown.text)
    assert (
        anon_client.post(
            "/apply/thanks/no-such-token/adjust", data=ADJUST, follow_redirects=False
        ).headers["location"]
        == "/apply/thanks/no-such-token?lang=en"
    )

    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    decline(db_session, deal.id, actor=ACTOR, reason="Not a fit")
    db_session.commit()
    assert deal.status is Status.DECLINED
    # the thank-you page has no link, the page says so, and a post changes nothing
    assert "adjust-link" not in anon_client.get(link).text
    closed = anon_client.get(f"{base}/adjust?lang=en")
    assert closed.status_code == 200
    assert "This request can no longer be changed online." in closed.text
    assert 'id="adjust-form"' not in closed.text
    refused = adjust(anon_client, base)
    assert refused.status_code == 409 and "can no longer be changed" in refused.text
    db_session.expire_all()
    assert len(deal.submissions) == 1 and len(versions(db_session, deal)) == 1
    del client


# --- the leak guard -------------------------------------------------------------------------------


def forbidden_on_borrower_pages(session: Session, deal: Deal) -> list[str | re.Pattern[str]]:
    """Everything that is the lender's on a fully analyzed deal, as any page would print it."""
    found: list[str | re.Pattern[str]] = [
        re.compile(r"\b(GO|CONDITIONAL|DECLINE|Go|Conditional|Decline|Verdict|verdict)\b"),
        "IRR",
        "Sensitivity",
        "sensitivity",
        "Lender",
        "lender",
        "Target lender",
        "Max quoted",
        "max quoted",
        "cushion",
        "Cushion",
        "Underwriting Assumptions",
        "Take-Back",
        "Take-back",
        "take-back",
        re.compile(r"\b[Rr]ental\b"),  # the profile's "Rentals" is the borrower's own word
        "Takeout",
        "takeout",
        "DSCR",
        "haircut",
        "Foreclosure",
        "foreclosure",
        "Flag",
        "flag",
        "Working",
        "Draft",
        USER_NAME,
        USER_EMAIL,
        ACTOR,
    ]
    config = get_config()
    found += [quoted(config.pricing.target_lender_irr), quoted(config.pricing.max_quoted_rate)]
    found += [
        quoted(config.pricing.borrower_rate_cushion),
        quoted(config.pricing.borrower_rate_rounding),
    ]
    # the five assumption values as the panel prints them, not inside another number
    for value in (
        mask_one("broker_selling_pct", deal.broker_selling_pct),
        mask_one("rental_expenses_pct_of_rent", deal.rental_expenses_pct_of_rent),
        mask_one("rental_takeout_rate", deal.rental_takeout_rate),
    ):
        found.append(re.compile(r"(?<![\d.,$])" + re.escape(value)))
    found.append(mask_one("take_back_legal_costs_usd", deal.take_back_legal_costs_usd))
    for version in versions(session, deal):
        results = version_results(session, version)
        if results.screen is not None:
            for flag in results.screen.flags:
                found += [flag.code.value, flag.message]
            found += results.screen.reasons
            found.append(results.screen.suggested_reply)
        underwrite = results.underwrite
        if underwrite is None:
            continue
        for flag in underwrite.flags:
            found += [flag.code.value, flag.message]
        irr = underwrite.return_overview.irr
        if irr is not None:
            found += [quoted(irr), pct(irr), f"{irr * 100:.4f}%"]
        if underwrite.sensitivity is not None:
            for row in underwrite.sensitivity.rows:
                for cell in row.cells:
                    if cell.irr is not None:
                        found += [quoted(cell.irr), f"{cell.irr * 100:.4f}%"]
        if underwrite.pricing is not None:
            for row in underwrite.pricing.rows:
                if row.lender_irr is not None:
                    found += [quoted(row.lender_irr), pct(row.lender_irr)]
                if row.interest_rate is not None:
                    found.append(quoted(row.interest_rate))  # the solve: the team's number
                found.append(row.sentence)
                if row.derisk:
                    found.append(row.derisk)
        take_back = underwrite.take_back
        price = getattr(take_back, "haircut_price", None)
        if price is not None and price.value is not None:
            found.append(dollars(price.value))
        rental = underwrite.rental
        for attribute in ("required_rent", "takeout_loan", "spread_to_loan"):
            value = getattr(rental, attribute, None)
            if value is not None:
                found.append(dollars(value))
    return found


def test_no_borrower_page_shows_the_lenders_side(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    """Every borrower-facing page of a fully analyzed deal - the form, its re-render, the
    thank-you on both views, the adjust page and its re-render, in both languages - is
    rendered and searched for the lender's side of the wall."""
    link = thanks_link(post(anon_client))
    (deal,) = deals(db_session)
    base = token_base(link)
    verdict, pricing = arrival(db_session, deal)
    assert verdict is Verdict.GO and pricing is not None
    # the team works the deal: a note and a run as Sam Reed, so the name is on the trail
    assert (
        client.post(
            f"/queue/deals/{deal.id}/note",
            data={"note": "Called the borrower"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.post(f"/queue/deals/{deal.id}/run", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert len(versions(db_session, deal)) == 2
    quote = quote_for(db_session, deal)
    for row, priced in zip(quote.rows, reversed(pricing.rows), strict=True):
        assert quoted(row.borrower_rate) != quoted(priced.interest_rate), "the cushion shows"

    pages = {
        "form en": anon_client.get("/apply?lang=en").text,
        "form es": anon_client.get("/apply?lang=es").text,
        "form re-rendered": post(anon_client, borrower_phone="12").text,
        "thanks simple en": anon_client.get(f"{base}?lang=en").text,
        "thanks simple es": anon_client.get(f"{base}?lang=es").text,
        "thanks advanced en": anon_client.get(f"{base}?lang=en&view=advanced").text,
        "thanks advanced es": anon_client.get(f"{base}?lang=es&view=advanced").text,
        "adjust en": anon_client.get(f"{base}/adjust?lang=en").text,
        "adjust es": anon_client.get(f"{base}/adjust?lang=es").text,
        "adjust re-rendered": adjust(anon_client, base, loan_requested="nope").text,
    }
    # each page is the page it should be
    assert "Indicative terms" in pages["thanks simple en"]
    assert "Your cash flows" in pages["thanks advanced en"]
    assert 'id="adjust-form"' in pages["adjust en"]
    assert "Please enter an amount in dollars." in pages["adjust re-rendered"]
    assert "Please enter a 10-digit phone number." in pages["form re-rendered"]

    forbidden = forbidden_on_borrower_pages(db_session, deal)
    assert len(forbidden) > 40
    for name, page in pages.items():
        # everything a person or a crawler reads: the stylesheet's class names (.tag.GO) and
        # the masks script are not content
        body = re.sub(r"<(style|script)\b.*?</\1>", "", page, flags=re.S)
        for needle in forbidden:
            if isinstance(needle, re.Pattern):
                assert needle.search(body) is None, (name, needle.pattern)
            else:
                assert needle not in body, (name, needle)


def test_every_apply_route_says_noindex(
    anon_client: QueueClient, client: QueueClient, db_session: Session
) -> None:
    link = thanks_link(post(anon_client))
    base = token_base(link)
    responses = [
        anon_client.get("/apply"),
        post(anon_client, borrower_phone="12"),
        post(anon_client, borrower_email="second@ortizbuilds.example"),
        anon_client.get("/apply/thanks"),
        anon_client.get("/apply/thanks/no-such-token"),
        anon_client.get(link),
        anon_client.get(f"{base}?lang=en&view=advanced"),
        anon_client.get(f"{base}/adjust?lang=en"),
        adjust(anon_client, base, loan_requested="nope"),
        adjust(anon_client, base),
    ]
    assert {response.status_code for response in responses} == {200, 303, 422}
    for response in responses:
        assert response.headers.get("x-robots-tag") == "noindex", response.url
    # the team's pages are behind the session, not hidden from a crawler by a header
    assert "x-robots-tag" not in client.get("/queue").headers
    assert "x-robots-tag" not in anon_client.get("/login").headers
    del db_session
