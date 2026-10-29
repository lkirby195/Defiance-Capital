"""The indicative terms a web borrower is shown, and the record of it.  # SPEC §4.2

A deal posted on the public form is sent to ``/apply/thanks/<token>``: a random, unguessable
token stored on the deal (``issue_thanks_token``) and live for
``web_intake.thanks_token_hours``. The page it opens shows the borrower one table -
**Indicative terms** - and, on its Advanced view, their own cash flows and Sources & Uses;
nothing else of the analysis: no verdict, no IRR, no flags, no sensitivity. The rules, all
here so the route and the page cannot word them differently:

    shown at all     only when the run the page reads (below) was the system's, said Go or
                     Conditional, and its ledger ran
    the run          the arrival run (version 1) until the borrower saves an edit from the
                     page (``services/adjust.py``); then that edit's run, "Borrower edit n",
                     whose number the deal keeps on ``borrower_terms_version``. Never the
                     Working version: what the team enters - a rate, a valuation, a court
                     finding - is the team's side of the wall until a person calls
    one row per      pricing step (SPEC §8.10) that quotes a rate: a row whose solved rate is
                     above ``pricing.max_quoted_rate``, or that nothing prices, is left out
    the rate         the row's solved rate plus ``pricing.borrower_rate_cushion``, rounded
                     up to the next ``pricing.borrower_rate_rounding`` (v0.22,
                     ``engine/calc/quote.py``); the team's pages keep the solve
    the order        the request first, then each smaller loan, largest to smallest
    the columns      Loan amount and Rate always; Estimated profit, Project yield and Yield
                     (cash-on-cash) - each at the cushioned rate - only when the borrower
                     gave a sale price, which is what the run's borrower side was evaluated
                     on; otherwise a line asks for one
    Advanced         the first row's cash flows by month and its Sources & Uses, both at
                     its cushioned rate; the Simple view is the table alone and the default
    nothing to show  when no row qualifies: the page is the plain thank-you

**What was shown is recorded** (``record_shown``): the rows as rendered - the cushioned
figures, the solved rate beside each for the team - the language, the version read, the
cushion and the views opened, on the deal (``indicative_terms_shown``,
``indicative_terms_shown_at``) and in the audit trail (``INDICATIVE_TERMS_SHOWN``), the
first time the page shows a version and again the first time its Advanced view is opened;
the deal page prints the record under Pricing as "Shown to borrower". A refresh of a view
already recorded for the version shown records nothing new.

Every write here takes an actor and writes ``audit_log``; nothing commits.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import Config, get_config
from db.models import Deal
from engine.calc.quote import borrower_quote
from schema.models import (
    AnalysisStatus,
    AuditAction,
    BorrowerCashFlow,
    QuoteRow,
    SourcesAndUses,
    Verdict,
)
from services.audit import DEALS, jsonable, record_audit
from services.autorun import SYSTEM_ACTOR
from services.errors import VersionNotFound
from services.versions import get_version, version_results

# The verdicts a borrower may see terms on (SPEC §4.2); a Decline shows the plain page.
SHOWN_ON: frozenset[Verdict] = frozenset({Verdict.GO, Verdict.CONDITIONAL})
# ``secrets.token_urlsafe(32)`` is 43 characters; the column holds 64.
TOKEN_BYTES = 32
TOKEN_MAX = 64
# The version the page reads until a borrower edit moves it: the first, the system's.
ARRIVAL_VERSION = 1
# The thank-you page's two views (SPEC §4.2): the table alone, or with the two tables under it.
SIMPLE_VIEW = "simple"
ADVANCED_VIEW = "advanced"
VIEWS: tuple[str, ...] = (SIMPLE_VIEW, ADVANCED_VIEW)


@dataclass(frozen=True)
class IndicativeRow:
    """One row of the Indicative terms table, as the borrower sees it.

    ``interest_rate`` is the rate shown - the cushioned one from v0.22 - and ``solved_rate``
    the team's solve it was built from (None on a record from before). The three figures
    are at the shown rate.
    """

    label: str  # "Requested", "-5%", ... (``PricingRow.label``)
    is_request: bool
    loan_amount: Decimal
    interest_rate: Decimal
    profit: Decimal | None
    cash_on_cash: Decimal | None
    project_yield: Decimal | None = None
    solved_rate: Decimal | None = None

    @classmethod
    def of(cls, row: QuoteRow, with_profit: bool) -> IndicativeRow:
        return cls(
            label=row.label,
            is_request=row.is_request,
            loan_amount=row.loan_amount,
            interest_rate=row.borrower_rate,
            profit=row.profit if with_profit else None,
            cash_on_cash=row.cash_on_cash if with_profit else None,
            project_yield=row.project_yield if with_profit else None,
            solved_rate=row.solved_rate,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "is_request": self.is_request,
            "loan_amount": jsonable(self.loan_amount),
            "interest_rate": jsonable(self.interest_rate),
            "solved_rate": jsonable(self.solved_rate),
            "profit": jsonable(self.profit),
            "project_yield": jsonable(self.project_yield),
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
            project_yield=number("project_yield"),
            solved_rate=number("solved_rate"),
        )


class CashFlowTotals(NamedTuple):
    """The cash flow table's last row: each column added up."""

    purchase: Decimal
    rehab: Decimal
    holding: Decimal
    financing: Decimal
    sale: Decimal | None
    net: Decimal


def cash_flow_totals(rows: list[BorrowerCashFlow]) -> CashFlowTotals:
    zero = Decimal(0)
    sold = [row.sale for row in rows if row.sale is not None]
    return CashFlowTotals(
        purchase=sum((row.purchase for row in rows), zero),
        rehab=sum((row.rehab for row in rows), zero),
        holding=sum((row.holding for row in rows), zero),
        financing=sum((row.financing for row in rows), zero),
        sale=sum(sold, zero) if sold else None,
        net=sum((row.net for row in rows), zero),
    )


@dataclass(frozen=True)
class BorrowerAdvanced:
    """The Advanced view: the first row's two tables at its cushioned rate.  # SPEC §4.2"""

    loan_amount: Decimal
    interest_rate: Decimal
    cash_flows: list[BorrowerCashFlow]
    totals: CashFlowTotals
    sources_and_uses: SourcesAndUses


@dataclass(frozen=True)
class IndicativeTerms:
    """The table: its rows, whether the profit columns are on it, and what it was built on.

    ``version``, ``rate_cushion`` and ``rate_rounding`` say which run and which cushion the
    rows came from (None on a record from before v0.22, which read version 1 uncushioned);
    ``views`` are the views recorded for that version on a record of what was shown;
    ``advanced`` is the Advanced view's two tables on a fresh table, never on a record.
    """

    rows: list[IndicativeRow]
    with_profit: bool
    language: str | None = None  # set on a record of what was shown; None on a fresh table
    version: int | None = None
    rate_cushion: Decimal | None = None
    rate_rounding: Decimal | None = None
    views: tuple[str, ...] = ()
    advanced: BorrowerAdvanced | None = None

    def as_json(self, language: str, views: list[str]) -> dict[str, Any]:
        return {
            "language": language,
            "with_profit": self.with_profit,
            "version": self.version,
            "rate_cushion": jsonable(self.rate_cushion),
            "rate_rounding": jsonable(self.rate_rounding),
            "views": list(views),
            "rows": [row.as_json() for row in self.rows],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IndicativeTerms:
        def number(key: str) -> Decimal | None:
            value = data.get(key)
            return None if value is None else Decimal(str(value))

        version = data.get("version")
        return cls(
            rows=[IndicativeRow.from_json(row) for row in data.get("rows", [])],
            with_profit=bool(data.get("with_profit")),
            language=data.get("language"),
            version=int(version) if version is not None else None,
            rate_cushion=number("rate_cushion"),
            rate_rounding=number("rate_rounding"),
            views=tuple(str(view) for view in data.get("views", [SIMPLE_VIEW])),
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


def terms_version(deal: Deal) -> int:
    """The number of the version the thank-you page reads: the borrower's latest edit's run,
    else the arrival run's."""
    return deal.borrower_terms_version or ARRIVAL_VERSION


def indicative_terms(
    session: Session, deal: Deal, config: Config | None = None
) -> IndicativeTerms | None:
    """What the thank-you page may show this borrower; None when nothing qualifies.

    The rows read the request first and then each smaller loan, largest to smallest (SPEC
    §4.2) - the other way round from the team's Pricing section, which runs most equity
    first (SPEC §8.10): a borrower is shown what they asked for before what less would cost.
    Each is quoted at its cushioned rate under the config in force (``engine/calc/quote``).
    """
    try:
        version = get_version(session, deal.id, terms_version(deal))
    except VersionNotFound:
        return None
    if version.actor != SYSTEM_ACTOR:
        return None
    results = version_results(session, version)
    if results.screen is None or results.screen.verdict not in SHOWN_ON:
        return None
    underwrite, inputs = results.underwrite, results.underwrite_inputs
    if underwrite is None or underwrite.pricing is None or inputs is None:
        return None
    cfg = config or get_config()
    sizing = underwrite.sizing
    quote = borrower_quote(
        inputs, underwrite.pricing, sizing.credit_tranche, sizing.experience_tier, cfg
    )
    with_profit = quote.borrower_status is AnalysisStatus.EVALUATED
    rows = [IndicativeRow.of(row, with_profit) for row in quote.rows]
    if not rows:
        return None
    advanced = None
    if quote.cash_flows is not None and quote.sources_and_uses is not None:
        advanced = BorrowerAdvanced(
            loan_amount=rows[0].loan_amount,
            interest_rate=rows[0].interest_rate,
            cash_flows=quote.cash_flows,
            totals=cash_flow_totals(quote.cash_flows),
            sources_and_uses=quote.sources_and_uses,
        )
    return IndicativeTerms(
        rows=rows,
        with_profit=with_profit,
        version=version.number,
        rate_cushion=quote.rate_cushion,
        rate_rounding=quote.rate_rounding,
        advanced=advanced,
    )


def record_shown(
    session: Session,
    deal: Deal,
    terms: IndicativeTerms,
    *,
    language: str,
    actor: str,
    view: str = SIMPLE_VIEW,
    now: datetime | None = None,
) -> bool:
    """Record that the page showed ``terms`` on ``view``; False when it already had. Flushes.

    A record names the version it was shown for and the views opened on it. A new version
    starts the record over; a view not yet on the record is added to it, and the rows are
    written again as rendered now; anything else is a refresh and records nothing.
    """
    current: dict[str, Any] = deal.indicative_terms_shown or {}
    same_version = bool(current) and (
        int(current.get("version") or ARRIVAL_VERSION) == (terms.version or ARRIVAL_VERSION)
    )
    seen = [str(seen) for seen in current.get("views", [SIMPLE_VIEW])] if same_version else []
    if view in seen:
        return False
    seen.append(view)
    shown = terms.as_json(language, seen)
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
