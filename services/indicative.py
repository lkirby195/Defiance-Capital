"""The indicative terms a web borrower is shown, and the record of it.  # SPEC §4.2

A deal posted on the public form is sent to ``/apply/thanks/<token>``: a random, unguessable
token stored on the deal (``issue_thanks_token``) and live for
``web_intake.thanks_token_hours``. The page it opens shows the borrower one table -
**Indicative terms** - and nothing else of the analysis: no verdict, no IRR, no flags, no
sensitivity. The rules, all here so the route and the page cannot word them differently:

    shown at all     only when the automatic screen on arrival (``Auto v1``, the actor
                     ``system``) said Go or Conditional, and the ledger ran
    one row per      pricing step (SPEC §8.10) that quotes a rate: a row whose solved rate is
                     above ``pricing.max_quoted_rate``, or that nothing prices, is left out
    the order        the request first, then each smaller loan, largest to smallest
    the columns      Loan amount and Rate always; Estimated profit and Yield (cash-on-cash)
                     only when the borrower gave a sale price, which is what the arrival
                     run's borrower side was evaluated on - otherwise a line asks for one
    nothing to show  when no row qualifies: the page is the plain thank-you

The results are the arrival run's, not the Working version's, on purpose: what the team
enters afterwards - a rate, a valuation, a court finding - is the team's side of the wall
until a person calls the borrower.

**What was shown is recorded** (``record_shown``): the rows as rendered and the language, on
the deal (``indicative_terms_shown``, ``indicative_terms_shown_at``) and in the audit trail
(``INDICATIVE_TERMS_SHOWN``), the first time the page shows them; the deal page prints them
under Pricing as "Shown to borrower". A refresh shows the same rows - the arrival run never
changes - and records nothing new.

Every write here takes an actor and writes ``audit_log``; nothing commits.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from schema.models import AnalysisStatus, AuditAction, PricingRow, Verdict
from services.audit import DEALS, jsonable, record_audit
from services.autorun import SYSTEM_ACTOR
from services.errors import VersionNotFound
from services.versions import get_version, version_results

# The verdicts a borrower may see terms on (SPEC §4.2); a Decline shows the plain page.
SHOWN_ON: frozenset[Verdict] = frozenset({Verdict.GO, Verdict.CONDITIONAL})
# ``secrets.token_urlsafe(32)`` is 43 characters; the column holds 64.
TOKEN_BYTES = 32
TOKEN_MAX = 64
# The version whose results the page shows: the first, and only when the system ran it.
ARRIVAL_VERSION = 1


@dataclass(frozen=True)
class IndicativeRow:
    """One row of the Indicative terms table, as the borrower sees it."""

    label: str  # "Requested", "-5%", ... (``PricingRow.label``)
    is_request: bool
    loan_amount: Decimal
    interest_rate: Decimal
    profit: Decimal | None
    cash_on_cash: Decimal | None

    @classmethod
    def of(cls, row: PricingRow, with_profit: bool) -> IndicativeRow:
        assert row.interest_rate is not None  # only a quoted row is shown
        return cls(
            label=row.label,
            is_request=row.is_request,
            loan_amount=row.loan_amount,
            interest_rate=row.interest_rate,
            profit=row.borrower_profit if with_profit else None,
            cash_on_cash=row.borrower_cash_on_cash if with_profit else None,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "is_request": self.is_request,
            "loan_amount": jsonable(self.loan_amount),
            "interest_rate": jsonable(self.interest_rate),
            "profit": jsonable(self.profit),
            "cash_on_cash": jsonable(self.cash_on_cash),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IndicativeRow:
        def number(key: str) -> Decimal | None:
            value = data.get(key)
            return None if value is None else Decimal(str(value))

        loan_amount, interest_rate = number("loan_amount"), number("interest_rate")
        assert loan_amount is not None and interest_rate is not None
        return cls(
            label=str(data["label"]),
            is_request=bool(data["is_request"]),
            loan_amount=loan_amount,
            interest_rate=interest_rate,
            profit=number("profit"),
            cash_on_cash=number("cash_on_cash"),
        )


@dataclass(frozen=True)
class IndicativeTerms:
    """The table: its rows, and whether the profit and yield columns are on it."""

    rows: list[IndicativeRow]
    with_profit: bool
    language: str | None = None  # set on a record of what was shown; None on a fresh table

    def as_json(self, language: str) -> dict[str, Any]:
        return {
            "language": language,
            "with_profit": self.with_profit,
            "rows": [row.as_json() for row in self.rows],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IndicativeTerms:
        return cls(
            rows=[IndicativeRow.from_json(row) for row in data.get("rows", [])],
            with_profit=bool(data.get("with_profit")),
            language=data.get("language"),
        )


# --- the token --------------------------------------------------------------------------------


def issue_thanks_token(
    session: Session,
    deal: Deal,
    *,
    actor: str,
    config: Config | None = None,
    now: datetime | None = None,
) -> str:
    """Give the deal its thank-you link's token, live for ``thanks_token_hours``. Flushes.

    The audit row records when the link expires and never the token itself.
    """
    hours = (config or get_config()).web_intake.thanks_token_hours
    issued = now or datetime.now(UTC)
    deal.thanks_token = secrets.token_urlsafe(TOKEN_BYTES)
    deal.thanks_token_expires_at = issued + timedelta(hours=hours)
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.THANKS_TOKEN_ISSUED,
        table_name=DEALS,
        row_id=deal.id,
        after={"expires_at": deal.thanks_token_expires_at.isoformat()},
    )
    return deal.thanks_token


def deal_by_thanks_token(session: Session, token: str, now: datetime | None = None) -> Deal | None:
    """The deal a live token was issued to; None on an unknown or expired one."""
    if not token or len(token) > TOKEN_MAX:
        return None
    deal = session.scalar(select(Deal).where(Deal.thanks_token == token))
    if deal is None or deal.thanks_token_expires_at is None:
        return None
    if deal.thanks_token_expires_at <= (now or datetime.now(UTC)):
        return None
    return deal


# --- the table --------------------------------------------------------------------------------


def indicative_terms(session: Session, deal: Deal) -> IndicativeTerms | None:
    """What the thank-you page may show this borrower; None when nothing qualifies.

    The rows read the request first and then each smaller loan, largest to smallest (SPEC
    §4.2) - the other way round from the team's Pricing section, which runs most equity
    first (SPEC §8.10): a borrower is shown what they asked for before what less would cost.
    """
    try:
        version = get_version(session, deal.id, ARRIVAL_VERSION)
    except VersionNotFound:
        return None
    if version.actor != SYSTEM_ACTOR:
        return None
    results = version_results(session, version)
    if results.screen is None or results.screen.verdict not in SHOWN_ON:
        return None
    if results.underwrite is None or results.underwrite.pricing is None:
        return None
    matrix = results.underwrite.pricing
    with_profit = matrix.borrower_status is AnalysisStatus.EVALUATED
    quoted = [row for row in matrix.rows if row.cleared]
    quoted.sort(key=lambda row: (not row.is_request, -row.loan_amount))
    rows = [IndicativeRow.of(row, with_profit) for row in quoted]
    if not rows:
        return None
    return IndicativeTerms(rows=rows, with_profit=with_profit)


def record_shown(
    session: Session,
    deal: Deal,
    terms: IndicativeTerms,
    *,
    language: str,
    actor: str,
    now: datetime | None = None,
) -> bool:
    """Record that the page showed ``terms``; False when it already had. Flushes."""
    if deal.indicative_terms_shown_at is not None:
        return False
    shown = terms.as_json(language)
    deal.indicative_terms_shown_at = now or datetime.now(UTC)
    deal.indicative_terms_shown = shown
    session.flush()
    record_audit(
        session,
        actor=actor,
        action=AuditAction.INDICATIVE_TERMS_SHOWN,
        table_name=DEALS,
        row_id=deal.id,
        after=shown,
    )
    return True


def shown_terms(deal: Deal) -> IndicativeTerms | None:
    """The table the borrower was shown, read back off the deal; None when nothing was."""
    if deal.indicative_terms_shown is None:
        return None
    return IndicativeTerms.from_json(deal.indicative_terms_shown)
