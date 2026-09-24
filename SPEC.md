# GLENWOOD Underwriting Platform — SPEC v0.5

Status: **v0.5**, 2026-09-23, engine `1.1.0`. Owner: Logan. Client: GLENWOOD (hard money lender, OK + CO).

**v0.5** is one change to the leverage tests and a pass over the §8.1 form: **LTV is the commitment over the estimated sale price and is the only value ratio** — LTARV is gone, the as-is value is gone with it, and there is no fallback denominator (§7.4, §8.2). The underwrite runs without a sale price (§8.4). The §8.1 Overview is who the borrower is and the loan's own terms moved into Deal Economics; the team form asks for the term rather than the bucket, drops the county and the separate guarantor name, and takes its numbers masked — `$425,000`, `12%`, `555-123-4567` — with the four config-defaulted economics pre-filled and tagged.

**v0.4's successor, the §8 rewrite** (engine `1.0.0`), is the release under it: the underwrite calc layer and its inputs were replaced on 2026-09-23. §8 is now a dated monthly cash-flow ledger with a true XIRR, plus three analyses — Flip, Rental and Take-Back. Gone with the previous §8: the term-level average-outstanding model, the rate solve and target IRR, the sensitivity grid, the minimum-interest and extension arithmetic, the DSCR takeout, and the REO liquidation downside. Intake, the review queue, persistence, the audit trail, auth and the deploy are unchanged; the screen keeps its structure and changes only its LTC denominator. Its own header said v0.3, going backwards by mistake; the number above is the correction.

Companion file: `CLAUDE.md` (conventions for Claude Code).

The engine math in §8 is the monthly ledger: the lender's dated cash flows from closing and their XIRR.

---

## 1. Purpose

GLENWOOD receives a meaningful volume of loan inquiries, mostly via phone/SMS (LinkedPhone), and underwrites each manually. This system:

1. Takes a deal in with minimum friction from the borrower ("tell us the deal, we do the rest")
2. Enriches it from GLENWOOD's data sources
3. Screens it fast and cheaply (no paid pulls) to Go / Conditional / Decline
4. Fully underwrites the deals that clear the screen (paid pulls, valuation, the monthly ledger)
5. Produces a credit memo and a pre-populated LOI
6. Hands off accepted deals to Mortgage Automator for closing, draws, and servicing

Mortgage Automator is downstream only. This system is the system of record from inquiry through LOI.

**Non-goals for v1:** borrower-facing web page (goes on GLENWOOD's site later), automated outbound SMS, MLS/portfolio monitoring, public-filing monitoring on the existing book, servicing.

**Standalone.** No code dependency on any other project. Patterns may be copied, never imported.

---

## 2. Architecture

```
Borrower / Team
   │
   ▼
[ INTAKE ]  SMS (LinkedPhone) · listing link · contract photo/PDF · team entry
   │        parsers → normalizer → IntakeRecord
   ▼
[ ENRICH ]  property data · deed history · filings · court records · MA borrower match
   │
   ▼
[ SCREEN ]  borrower score + implied leverage vs caps → Go / Conditional / Decline (+ reasons)
   │        ── Decline: team notified, record kept
   │        ── Conditional / Go: enters team review queue
   ▼
[ UNDERWRITE ]  paid pulls (credit, valuation) · sizing · monthly ledger + XIRR · flip · rental · take-back
   │
   ▼
[ OUTPUT ]  screen summary · credit memo (pass/fail flags) · LOI (docx) · MA push
```

Stack: Python 3.12, FastAPI, Postgres, SQLAlchemy + Alembic, Pydantic, pytest. Deployed as a small service (webhook receiver + review queue page + engine). See `CLAUDE.md`.

---

## 3. Products

| Product | Structure | Interest | Typical use |
|---|---|---|---|
| `NO_DRAW` | Single note, full principal at close | On full principal from close | Purchase only, no rehab funding |
| `SPLIT_DRAW` | Single note: purchase portion at close, rehab holdback drawn over the rehab period | On **full commitment** from close | Fix-and-flip |
| `SPLIT_PRINCIPAL` | Two notes: **Principal Note** (purchase, at close) + **Tranche A** (rehab, drawn) | Principal Note from close; Tranche A on the **drawn balance at the start of each month** | Fix-and-flip, larger rehab |
| `WHOLETAIL` | Single note, full principal at close | On full principal from close | Buy below market, minimal work, retail resale; short term |

Common to all products:

- Interest paid current, monthly interest-only, at the deal's `interest_rate` (§8.1). No accrual, no deferral.
- A stated loan term. The loan runs to `payoff_date` and pays off there: v0.3 models no early payoff, and there is no minimum-interest exercise and no extension in the math.
- Origination fee `origination_fee_pct` of the commitment (default 2.0%, config): half at close, half at payoff.
- The rehab side is drawn straight-line over `rehab_months = term_months − listing_months` (config, placeholder 3), floored at 0 — the last months of the term are listing and sale. A term at or inside the listing period leaves no rehab period, and the rehab money is advanced at close instead (`NO_REHAB_PERIOD`, §8.8).
- `NO_DRAW` and `WHOLETAIL` carry no rehab portion: `loan_rehab_portion` is 0 and the whole commitment is funded at close.

Borrower intent and exit are inferred from term and asset type, then confirmed by the team.
`asset_type` is `SFR | UNITS_2_4 | UNITS_5_PLUS | OTHER`, captured at intake. The engine
applies these rules in order and records `exit_source`:

| # | Rule | Exit | `exit_source` |
|---|---|---|---|
| 1 | Team states an exit (anything but `UNKNOWN`) | as stated | `STATED` |
| 2 | Term ≤ 9 months and asset type is `SFR` or `UNITS_2_4` | resale: `WHOLETAIL` for the `WHOLETAIL` product, else `FLIP` | `INFERRED` |
| 3 | Term ≥ 12 months | `HOLD` | `INFERRED` |
| 4 | Neither fires (e.g. a 10-month term, or a ≤ 9-month term on `UNITS_5_PLUS`) | `UNKNOWN` | `INFERRED` |

A team-stated exit always wins, including a stated `HOLD` on a short term. The exit type
gates nothing and flags nothing. All it does is set the **default** state of the two
analysis toggles (§8.1): a resale exit turns the Flip analysis on, a hold exit turns the
Rental analysis on, and the Take-Back analysis runs on every deal whatever the exit says.

Each product's row above is shown as a one-line definition under the Loan Type box and beside
the chosen product on the deal page, so a person picking one is told what they are picking.
Every enum a person reads — the loan type, the asset type, the loan purpose and the exit — is
rendered in title case with spaces (`SPLIT_DRAW` is "Split Draw") wherever it appears
(`schema/labels.py`); the stored value does not change.

---

## 4. Intake

### 4.1 Minimum viable intake (any channel)

Five things. Everything else is derived or requested later, only if the deal clears the screen.

| # | Field | Notes |
|---|---|---|
| 1 | Property | Address, or a listing/auction link (address is extracted from the link) |
| 2 | Purchase price | Dollars |
| 3 | Rehab costs | Dollars; 0 allowed |
| 4 | Loan Amount | Dollars. Borrower-driven — the engine derives leverage from this |
| 5 | How long do you need the loan? | The term. The borrower channels ask it as a bucket (3 / 6 / 9 / 12 / 12+ months) which seeds `term_months`; the team form asks for the term in months or the payoff date directly (§8.1), because a person with the whole deal in front of them knows it |
| 6 | Who you are | Guarantor name, entity (if any), phone (optional); **credit range** (pick one of the five tranches, §7.1); **real estate experience** (deals completed in last 3 years: 0 / 1–2 / 3–5 / 6+); **repeat borrower** yes/no |

(Numbered as six rows because "who you are" is one question with sub-fields.)

The phone is the borrower match key when there is one (§5) and is not itself required: a deal
that arrives by email has a name and no number, and refusing to screen it for want of one
would be a gate on the wrong thing. It is stored as digits and shown and typed as
`###-###-####`.

There is one name on a deal and it is the guarantor's. The separate `guarantor_name` the deal
used to carry beside the borrower's is gone: two name columns, one optional and neither saying
which was which, is what that arrangement actually was.

Self-reported credit and experience are used for the screen only and are verified in underwrite.

### 4.2 Channels

| Channel | Mechanism | Parser |
|---|---|---|
| SMS via LinkedPhone | Inbound webhook (capability TBD — confirm in recon). Fallback: polling or manual paste | LLM extraction to `IntakeRecord`; missing-field list generated for the team |
| Listing / auction link | Borrower texts or pastes a URL | URL → address only. No page scraping (see §4.4) |
| Contract photo / PDF | Borrower texts a photo or sends a PDF of the purchase agreement | OCR + LLM extraction: address, price, close date, buyer entity, seller concessions |
| Team entry | Review-queue page, same fields with extra ones unlocked | Direct |

Borrower-facing web page is out of scope for v1; the intake schema is designed so GLENWOOD's site can post to the same endpoint later.

### 4.3 SMS handling

Inbound messages create or update an `IntakeRecord` and land in the **team review queue**. Replies to the borrower are sent by a team member through LinkedPhone. The system drafts a suggested reply (e.g., "Got it — what's the rehab budget?") but **never sends automatically**. No outbound SMS to anyone who has not texted in first.

### 4.4 Listing links

Zillow, Redfin, Realtor.com, Hubzu, Auction.com, Xome, etc. prohibit scraping in their terms and actively block it. The system:

- Extracts the address from the URL slug (reliable on all major sites)
- Pulls property data from licensed sources (§6)
- Does **not** fetch or parse the listing page
- Stores the original URL so a team member can open it manually for photos and remarks

### 4.5 `IntakeRecord` (Pydantic; canonical schema in `schema/intake.json`)

```
IntakeRecord
  id, created_at, channel: SMS | LINK | CONTRACT | TEAM
  raw_payload                    # original message / file ref / form data
  borrower:
    name, phone?, email?, entity_name?     # name is the guarantor's; phone is digits (§4.1)
    credit_range?: T1..T5        # self-reported; the screen needs one and names it when absent
    experience_bucket: 0 | 1_2 | 3_5 | 6_PLUS   # self-reported
    repeat_borrower: bool
  property:
    address_raw, address_normalized?, listing_url?
    city?, county?, state: OK | CO | OTHER     # county comes from enrichment; no box asks for it
    units?, structures?, sf?, year_built?, year_renovated?
    beds?, baths?, garage_spaces?                        # descriptive; no math reads them
  deal:
    loan_purpose?: PURCHASE | REFINANCE | CASH_OUT | CONSTRUCTION
    purchase_price, rehab_costs, loan_requested          # "Loan Amount" wherever it is shown
    loan_purchase_portion?, loan_rehab_portion?          # §8.2, split products; the first is
                                                         #   "Advance at Closing"
    term_bucket?: 3 | 6 | 9 | 12 | 12_PLUS               # borrower channels only; it seeds term_months
    term_months?, payoff_date?   # enter either; the other derives from closing_date (§8.1)
    closing_date?                # month 0 of the ledger (§8.3)
    interest_rate?               # annual; required to underwrite (§8.1)
    contingency_pct?, closing_costs_usd?, holding_costs_total_usd?, origination_fee_pct?
                                 # §8.1 deal economics; each falls back to its config default
    flip_analysis?, rental_analysis?   # §8.1 toggles; null leaves the §3-derived default
    asset_type?: SFR | UNITS_2_4 | UNITS_5_PLUS | OTHER   # drives the §3 exit inference
    stated_exit?: FLIP | HOLD | WHOLETAIL | UNKNOWN
  team overrides (§6):           # stand-ins for enrichment, entered by hand
    estimated_sale_price_team?, monthly_rent?
    court_records_status?: NOT_CHECKED | CLEAN | FLAGS
    court_records_as_of?         # the day the team searched; required for CLEAN and FLAGS
    court_records_team: [..]     # one typed matter per entry, §7.2
  missing_fields: [..]           # what the team still needs to ask for
  status: NEW | NEEDS_INFO | SCREENED | IN_REVIEW | UNDERWRITING | LOI_SENT | HANDED_OFF | DECLINED | DEAD
```

### 4.6 Status transitions

Two moves are automatic; every other move on the lifecycle is a team action in the review
queue.

| Trigger | From | To |
|---|---|---|
| Screen (§7), any verdict but Decline | `NEW` | `SCREENED` |
| Screen (§7), Decline verdict | `NEW`, `SCREENED`, `IN_REVIEW` | `DECLINED` |
| Screen (§7), Decline verdict | `UNDERWRITING` and later | unchanged; the Hard flags are recorded on the `screens` row |
| Underwrite (§8) | `SCREENED`, `IN_REVIEW` | `UNDERWRITING` |

A Decline closes a deal nobody has started pricing, wherever in those three states it sits:
a re-screen that turns up a Hard flag on a deal sitting in review is exactly the case worth
acting on. From `UNDERWRITING` onwards it does not — the deal is being worked, a person owns
it, and the flags are recorded for them to read rather than yanked out from under them.

Everything else keeps the status it has: a non-Decline re-screen never drags a deal
backwards, and re-underwriting one already in `UNDERWRITING` is a no-op. **A `DECLINED` or
`DEAD` deal cannot be underwritten** — the run is refused and nothing is priced, until a
person re-opens it.

**An unscreened deal is screened on the way in.** The underwrite is Stage 2: it runs on
deals that cleared Stage 1, so `run_underwrite` on a `NEW` deal runs the screen first and
proceeds only if that verdict is not a Decline. The screen it runs is a real one — its row
is recorded and it moves the status like any other — so a Decline there stops the underwrite
with the deal left `DECLINED` and the screen kept.

---

## 5. Data model (Postgres)

Tables (one-line intent each; full DDL via Alembic migrations):

- `borrowers` — person-level; phone is the primary match key; links to MA borrower id when matched
- `entities` — LLCs etc.; many-to-many with borrowers
- `intake_submissions` — every inbound message/file/form, immutable, raw
- `deals` — one per property × borrower inquiry; current `IntakeRecord` state lives here
- `properties` — normalized address, city, parcel, county, state, and the descriptive facts (§8.1); reused across deals
- `enrichment_runs` — one row per adapter call: source, timestamp, status, raw response ref, parsed result
- `screens` — screen inputs, score components, verdict, reasons; one per run (re-screen creates a new row)
- `underwrites` — full underwrite inputs and outputs (JSONB, the monthly ledger included), version of engine used
- `documents` — credit reports, valuations, contracts, generated memos/LOIs; file storage ref + hash
- `ma_sync` — handoff log to Mortgage Automator: payload, MA ids, status
- `users` — the people who sign in to the review queue; what `audit_log.actor` resolves to
- `audit_log` — who changed what; required because credit and court data are in here

Money stored as `NUMERIC(14,2)`. Rates as `NUMERIC(7,5)`. All enrichment raw responses retained.

`screens` and `underwrites` are append-only: a re-run writes a new row and nothing is
updated in place. Each row records `engine_version` and `config_hash` (§10), so a result can
always be traced to the code and the tunables that produced it. The engine result is stored
whole in JSONB — `screens.score_components` holds components, sizing, and flags;
`underwrites.outputs` holds the whole `UnderwriteResult`, ledger included — so a stored row
rebuilds the exact `ScreenResult` / `UnderwriteResult`. `underwrites.irr` is a `NUMERIC(7,5)`
copy of the XIRR for querying; the JSONB carries full precision.

A result shape is part of what `engine_version` records. The v0.3 §8 has no `UnderwriteResult`
in common with the one before it — no solved rate, no grid, no takeout, no downside — and no
ledger could be reconstructed from a row that never had one, so migration `0010` deletes
every `screens` and `underwrites` row written before `1.0.0`. Migration `0011` does the same
for `1.1.0`, for the same reason and on a smaller change: `SizingResult` has lost `ltv_basis`
and `as_is_value_source`, `MetricCheck` has lost `basis`, and `LeverageMetric` no longer has
`LTV_AS_IS` or `LTARV`, so a stored row carrying any of them cannot be rebuilt into a model
that forbids extras. The deals themselves keep their intake, their overrides, their status and
their audit trail, and are simply re-run.

`0011` also drops `deals.as_is_value_team` and `deals.guarantor_name` (§7.4, §8.1) and makes
`borrowers.phone` nullable, rewriting every NANP number already in it from `+15551234567` to
`5551234567` (§4.1).

---

## 6. Enrichment adapters

Each adapter implements a common `Protocol` (see `CLAUDE.md`), returns typed results, and records an `enrichment_runs` row. Status column reflects what is known as of this draft; recon in progress.

| Source | Purpose | Access | Stage | Status |
|---|---|---|---|---|
| Mortgage Automator | Borrower match (repeat), prior loan history, handoff target | API (open) | Screen; Output | Known |
| Forecasa | Lien/filing history on borrower entities and on the subject property | API | Screen | Known |
| OSCN (oscn.net) | Oklahoma court records: judgments, tax liens, foreclosures, civil litigation | Web search, no official API; HTML fetch within ToS | Screen | Known |
| Colorado courts | Same for CO | CoCourts (paid) or county-level; likely manual step in v1 | Screen | TBD |
| PACER | Bankruptcy (both states), federal civil | PACER Case Locator; paid per query | Screen | TBD |
| PropStream | Property detail, owner history, **deed history under borrower entities** (experience verification), lien lookup | No public API known; CSV export or manual in v1 | Screen | Recon |
| Credco | Tri-merge credit report | API or PDF, depends on account | Underwrite only, requires signed authorization | Recon |
| RicherValues | Paid valuation (the estimated sale price) | API or PDF, depends on account | Underwrite only | Recon |
| Property data API (ATTOM / RentCast / similar) | Programmatic valuation, rent estimate, comps when PropStream is manual | API, paid | Both | Optional |
| LinkedPhone | Inbound SMS/call capture | Webhook or polling | Intake | Recon |

Experience verification: count buy→sell pairs in the last 36 months across the borrower's known entities from deed history. Self-reported bucket is displayed next to the verified count; mismatch is a flag, not a fail.

### 6.1 Team overrides (the interim source)

Until an adapter exists for a value, the team is the source. The intake form takes a
hand-entered `estimated_sale_price_team` and `monthly_rent`, and a
`court_records_status` of
`NOT_CHECKED` / `CLEAN` / `FLAGS` with one typed matter per entry (each carrying the facts
§7.2 tests that code on: the date for a lookback code, the amount for a threshold code, the
lien facts for a subject-property encumbrance — so the config thresholds, not the team,
decide the outcome).

Precedence is fixed: **an adapter value always wins over a team value**, and the team value
stays on the deal either way so a later reader can see what was entered by hand and what
superseded it. Every value the engine ran on records its source, `ADAPTER` or `TEAM`, on the
stored screen and underwrite (§5) — a Go that rests on a hand-entered valuation and a
hand-done court search is a different thing from a Go that rests on a pull, and the verdict
alone does not say which it is.

`NOT_CHECKED` is not `CLEAN`: the first is reported as an INFO flag, the second is a clean
record dated the day the team searched.

Both the screen and the underwrite raise `TEAM_SOURCED_VALUES` (Info, fixed in code) when
any value they ran on carries source `TEAM`, and the message names which — the estimated sale
price, the court records, or both. It never moves a verdict; it is there so the reason
survives into the credit memo (§9.2), where a reader is deciding how much weight to put on
a Go. Both can be named at either stage: the underwrite re-runs the §7.2 tests on the
record in force at underwrite time (§8.1), so a hand search reaches it too — named as the
team's only where no adapter has superseded it by then.

---

## 7. Screen (Stage 1)

No paid pulls. Runs automatically on every complete intake. Target: verdict in minutes.

### 7.1 Credit tranches (self-reported at screen, verified at underwrite)

| Tranche | FICO |
|---|---|
| T1 | 740+ |
| T2 | 700–739 |
| T3 | 660–699 |
| T4 | 620–659 |
| T5 | < 620 |

Floor tranche is config (`config/glenwood.yaml`), placeholder T4.

### 7.2 Court and filing flags

| Flag | Source | Severity (config) |
|---|---|---|
| Bankruptcy within lookback (placeholder 4 yrs) | PACER | Hard |
| Active foreclosure as owner | OSCN / CO / Forecasa | Hard |
| Unsatisfied judgment > threshold | OSCN / CO | Hard |
| Tax lien, open (any amount) | Forecasa / county | Hard |
| Active civil litigation as defendant, > threshold | OSCN / CO | Soft |
| Satisfied judgment / released lien within lookback | OSCN / Forecasa | Soft |
| Landlord-tenant matters (as landlord) | OSCN / CO | Info |
| Subject property: existing liens, lis pendens | Forecasa / PropStream | Hard if senior and unresolved at close |

Thresholds and lookbacks are config, not code. Where no adapter covers a state yet, the team
records what it found by hand (§6.1); the same thresholds then apply to it unchanged.

### 7.3 Experience tiers

| Tier | Verified deals, last 36 mo |
|---|---|
| E0 | 0 |
| E1 | 1–2 |
| E2 | 3–5 |
| E3 | 6+ |

Repeat GLENWOOD borrower with clean payoff history is a positive override (config).

### 7.4 Implied leverage (from loan requested)

```
rehab_adj    = rehab_costs × (1 + contingency_pct)        # input, config default 0.00
total_cost   = purchase_price + rehab_adj + closing_costs_usd   # input, config default 1,000
LTC          = loan_requested / total_cost
LTV          = loan_requested / estimated_sale_price      # if unavailable: NOT_AVAILABLE, flagged,
                                                          #   and the screen goes Conditional
```

**Two ratios, and one value in the second.** LTARV is gone and the as-is value is gone with
it. They were two opinions of the same thing, and the price the deal exits at is the one the
lender is lending against; a second ratio against a second valuation added a number to
maintain and no test the first did not already make.

**There is no fallback denominator.** A purchase price is what the borrower agreed to pay, not
what the property is worth after the work, and standing it in reported a leverage figure
nobody had measured. Without a sale price, LTV is `NOT_AVAILABLE`,
`ESTIMATED_SALE_PRICE_MISSING` (Soft) is raised and the screen goes Conditional — which is
what it did before, under a different flag.

There is no borrower buy-side closing percentage any more: the LTC denominator carries the
lender's own `closing_costs_usd` (§8.1), which is an input with a config default rather than
a percentage of the price.

Compare to caps in `config/glenwood.yaml`, keyed by product × credit tranche × experience tier.
Each cell is `ltc` and `ltv`. Placeholder grid ships with obvious dummy values — the LTC
placeholder is **100%**, which is no cap at all until GLENWOOD fills it; GLENWOOD fills.

### 7.5 Verdict

- **Decline**: any Hard flag, or credit below floor tranche, or any leverage metric above cap by more than the tolerance band (config, placeholder 5 pts)
- **Conditional**: Soft flags, leverage within tolerance band above cap, missing estimated sale price, self-reported vs. verified mismatch, state = OTHER
- **Go**: none of the above

Every verdict carries a list of `reasons[]` in plain language for the team and a `suggested_reply` draft for the borrower (never auto-sent).

---

## 8. Underwrite (Stage 2)

Runs on Conditional/Go deals when a team member advances them. Paid pulls happen here, credit only after `credit_authorization_signed = true` on the deal.

§8 is a **dated monthly cash-flow ledger**. The lender's flows are laid out month by month from `closing_date`, and the headline number is their XIRR on the actual dates. There is no rate solve, no sensitivity grid, no target IRR, no minimum-interest or extension arithmetic, and no average-utilization constant: the ledger states the balances directly rather than summarizing them.

Three analyses sit beside the ledger, each asking a different question about the same deal. **Flip** (§8.4): does the project make money if it is sold? **Rental** (§8.5): does it carry a takeout loan if it is held? **Take-Back** (§8.6): does it carry GLENWOOD's own cost if the lender ends up owning it? Flip and Rental are toggles; Take-Back is always on and replaces the liquidation downside entirely.

### 8.1 Inputs

Grouped and labelled as the team-entry form and the deal page's override block group them.

**Overview** — who the borrower is, and nothing else. In this order, which is the order the
form asks and the deal page shows:

| Input | Notes |
|---|---|
| `entity_name` | Optional. The LLC the deal is being done in |
| `guarantor_name` | The one name a deal carries. Stored on `borrowers.name`; the box used to be captioned "Borrower Name" and the separate guarantor field is gone (§4.1) |
| `phone` | Optional. The borrower match key when there is one (§5). Stored as digits, shown and typed `###-###-####` |
| `email` | Optional |
| `credit_range` | Optional on the form. One of the five tranches (§7.1), shown with its FICO range (`schema/labels.py`). The screen needs one for the floor check and the caps lookup and names it by hand when it is absent |
| `experience_bucket` | Deals in last 36 months: 0 / 1–2 / 3–5 / 6+ |
| `repeat_borrower` | Yes / no |

The loan's own terms are not here. Its purpose, its type, its closing date and its term are
Deal Economics, beside the money they describe.

**Property Overview**

`address`, `city`, `state` (§4.5), `units`, `structures`, `sf`, `year_built` (optional), `year_renovated` (optional), `beds`, `baths`, `garage_spaces`.

Descriptive: captured, stored on `properties`, and reported. None of them feeds the math, so none of them is required to run anything.

**Deal Economics**

| Input | Default | Notes |
|---|---|---|
| `loan_purpose` | — | `PURCHASE` \| `REFINANCE` \| `CASH_OUT` \| `CONSTRUCTION`, team-selected. Recorded and reported; it feeds no math |
| `loan_type` | — | The §3 product, labelled "Loan Type" wherever a person reads it, with that product's §3 one-liner under the box |
| `closing_date` | — | Month 0 of the ledger. **Required to underwrite** |
| `term_months` / `payoff_date` | — | Enter either and the other derives; enter both and they have to agree. **Required to underwrite** |
| `purchase_price` | — | |
| `rehab_costs` | — | 0 allowed |
| `loan_requested` | — | Labelled **Loan Amount** wherever it is shown |
| `loan_purchase_portion`, `loan_rehab_portion` | — | §8.2; `commitment` is their sum. The first is labelled **Advance at Closing**, the second **Rehab Portion**. The rehab portion is 0 on `NO_DRAW` and `WHOLETAIL` |
| `interest_rate` | — | Annual. **Required to underwrite** |
| `contingency_pct` | `fees.contingency_default_pct` (0.00) | `rehab_adj = rehab_costs × (1 + contingency_pct)` |
| `closing_costs_usd` | `fees.closing_costs_default_usd` (1,000) | The lender's closing costs. It replaces the 3%-of-price borrower closing assumption, which is gone |
| `holding_costs_total_usd` | `fees.holding_costs_default_pct_of_cost` × (`purchase_price` + `rehab_costs`) (2%) | **Total over the hold**; the monthly figure is `holding_costs_total_usd / term_months` |
| `origination_fee_pct` | `fees.origination_default_pct` (2.0%) | Half at close, half at payoff, both on the commitment |

`payoff_date = closing_date + term_months` calendar months, clamped to the end of a short
month (31 January + 1 month is 28 February). Entering a payoff date derives the term the same
way, and a date that is not exactly a whole number of months after closing is refused, naming
the two nearest dates that are — the ledger's rows are months, so a term of "5½ months" is a
number the engine cannot price. A term and a payoff date that disagree are refused too, naming
both: that is a person having edited one box and left the other behind.

`term_bucket` (§4.1) is the borrower's own answer to "how long do you need the loan?" and
still seeds `term_months` on a borrower-channel intake. **The team form does not ask it**: a
person with the deal in front of them knows the term, and a bucket beside it would be a second
number to keep in step with the first. It is not the priced term wherever it exists: the
team's `term_months` / `payoff_date` is, and it is allowed to differ from the bucket.

The lender funds `rehab_adj` through the rehab portion, and the rehab portion is capped at
it (§8.2): the lender does not hold back more than the contingency-adjusted rehab cost could
ever draw.

**What a person types is not what the deal stores.** On the team form and the deal page's
override block, money is typed and shown `$425,000`, a percent `12%` (the deal carries the
fraction `0.12`), and a phone `555-123-4567` (the deal carries the digits). The four economics
with a config default — the contingency, the closing costs, the holding costs and the
origination fee — are **pre-filled with it, tagged "default", and editable**, with a reset
link beside each. A box still holding its default stores nothing: the column stays null, the
engine reads config, and the readiness checklist still says `DEFAULT` rather than claiming
somebody chose it. Small dependency-free client-side script formats what is typed; the server
parses either shape, so a browser that runs none of it still posts a deal that saves.

**Valuation and rent**

| Input | Notes |
|---|---|
| `estimated_sale_price` | The ARV. LTV is computed on it (§7.4, §8.2) and the Flip analysis sells at it. Optional: without one, LTV is `NOT_AVAILABLE` with `ESTIMATED_SALE_PRICE_MISSING` (Soft) and the Flip analysis is `NOT_EVALUATED` with `SALE_PRICE_MISSING` (Info) — the run itself goes ahead |
| `monthly_rent` | Optional. Rental and Take-Back are computed on it; without it both report `NOT_EVALUATED` and the underwrite raises `MONTHLY_RENT_MISSING` (Info) rather than running either on a zero |

The as-is value is gone from the engine, from the form and from `deals` (migration `0011`).
With one value ratio there is nothing left for it to feed.

**Analysis toggles**

| Toggle | On by default when |
|---|---|
| `flip_analysis` | The §3 exit is a resale — `FLIP` or `WHOLETAIL` |
| `rental_analysis` | The §3 exit is `HOLD`, or a `monthly_rent` has been entered |
| Take-Back | Always. Not a toggle |

A toggle the team sets by hand wins over its default, in either direction. Both are a visible
three-state control — Default / On / Off — on the team form and on the deal page's override
block, and each analysis panel shows the state it ran under. Take-Back has no toggle anywhere,
because it has none at all.

Also from intake and enrichment, unchanged: `asset_type` and the team-stated exit (the §3
inference), the verified `credit_score` and deal count, and `court_records` — the §7.2 court
and filing tests are re-run here on the source in force at underwrite time, adapter over
team (§6.1). They are not copied from the `screens` row: weeks can pass between the two
stages and a pull that has since landed supersedes the hand search Stage 1 ran on.

### 8.2 Sizing

Unchanged from §7.4 in structure, on verified values, with one changed denominator:

```
rehab_adj  = rehab_costs × (1 + contingency_pct)
total_cost = purchase_price + rehab_adj + closing_costs_usd        # was + 3% of the price
LTC        = commitment / total_cost
LTV        = commitment / estimated_sale_price      # NOT_AVAILABLE with no price (§7.4)
```

The two split products carry an explicit `loan_purchase_portion` ("Advance at Closing") and `loan_rehab_portion` ("Rehab Portion"), entered by the team and adding up to the Loan Amount (§8.1). Nothing is derived: there is no default advance at closing and no override of one.

- `NO_DRAW`, `WHOLETAIL`: `commitment = loan_requested`; no split
- `SPLIT_DRAW`: `commitment = loan_requested`; `holdback = min(rehab_adj, loan_rehab_portion)`; `funded_at_close = commitment − holdback`
- `SPLIT_PRINCIPAL`: `principal_note = loan_purchase_portion`; `tranche_a = min(rehab_adj, loan_rehab_portion)`; `commitment = principal_note + tranche_a`
- Either split product with **no split entered**: `commitment = loan_requested` and no split is reported. That is a borrower-channel intake nobody has divided yet; the screen runs, the underwrite refuses (§8.1)

Whenever the `rehab_adj` cap bites, `REHAB_PORTION_EXCEEDS_BUDGET` (Info) names the portion entered and the budget it was capped at. On `SPLIT_PRINCIPAL` the cap lowers the commitment below the request, which `COMMITMENT_BELOW_REQUEST` (Info) reports as well; on `SPLIT_DRAW` it does not, because there is one note — the money above the cap is advanced at close instead of held back, so only the timing moves.

Output: pass/fail on each cap, with the cap and the actual.

### 8.3 Return Overview — the lender's ledger and its IRR

The lender's dated monthly cash flows from `closing_date`, and their XIRR. Signs are from the lender's side: money out is negative, money in is positive.

```
rehab_months   = max(0, term_months − listing_months)
draw_per_month = rehab_portion / rehab_months                      # rehab_months > 0
month m date   = closing_date + m calendar months, m = 0 .. term_months
```

**Month 0 — closing.**

```
funding  = −funded_at_close        # the purchase portion on a split product;
                                   # the whole commitment on NO_DRAW and WHOLETAIL
fees     = +commitment × origination_fee_pct / 2        # the close half
```

When `rehab_months = 0` there is no rehab period to draw over, so the rehab portion is advanced at close: `funding = −commitment` and no draws are scheduled at all (`NO_REHAB_PERIOD`, §8.8).

**Months 1 .. rehab_months — Future Draws.**

```
draws = −draw_per_month
```

One negative flow per month, in the month it is drawn.

**Months 1 .. term_months — interest.** Interest for month `m` is a positive flow on the balance at the **start** of that month, so a draw taken in month `k` first earns interest in month `k + 1`:

```
interest(m) = balance(m) × interest_rate / 12

balance(m)  = commitment                                        # NO_DRAW, WHOLETAIL, SPLIT_DRAW
            = principal_note + tranche_a × min(m − 1, rehab_months) / rehab_months
                                                                # SPLIT_PRINCIPAL
```

`SPLIT_DRAW` pays on the full commitment from close even though the holdback has not gone out yet — that is the product (§3), and the ledger shows it as a real difference from `SPLIT_PRINCIPAL` rather than a constant.

**Month `term_months` — payoff.**

```
payoff = +outstanding principal      # funded_at_close + every draw = commitment
fees  += +commitment × origination_fee_pct / 2        # the payoff half
```

**Outputs.** The ledger as a table — `date`, `month`, `funding`, `draws`, `interest`, `fees`, `payoff`, `net` — plus:

```
total_interest = Σ interest
total_fees     = Σ fees                                # both origination halves
total_profit   = Σ net = total_interest + total_fees   # funding + draws cancel the payoff
irr            = XIRR(net, date)
```

`irr` is a true XIRR on the actual dates, annualized and compounded, on the Excel convention — the rate `r` at which

```
Σ net_i / (1 + r) ^ ((date_i − closing_date) / 365)  =  0
```

solved by Newton's method with a bisection fallback (`engine/calc/irr.py`). It is `null` on
the degenerate ledger where no flow is negative and no rate solves it, which a positive
purchase portion makes unreachable in practice.

### 8.4 Flip analysis

On by default for a resale exit (§8.1). It sells at the `estimated_sale_price`, and without one it is `NOT_EVALUATED` — the whole cost stack is still reported, the four figures the sale price feeds are `null`, and `SALE_PRICE_MISSING` (Info, fixed in code, §8.8) says so. The run itself goes ahead: the ledger, the economics and the Take-Back analysis do not read a sale price, and refusing the whole underwrite for a figure three quarters of it does not need was a gate on the wrong thing.

```
broker_costs    = estimated_sale_price × broker_selling_pct          # config, 4%
contingency     = rehab_costs × contingency_pct
financing_costs = total_interest + both origination halves           # from §8.3
net_profit      = estimated_sale_price
                  − broker_costs
                  − (purchase_price + closing_costs_usd)
                  − holding_costs_total_usd
                  − rehab_costs
                  − contingency
                  − financing_costs
total_costs     = purchase_price + closing_costs_usd + holding_costs_total_usd
                  + rehab_costs + contingency + financing_costs
profit_yield    = net_profit / total_costs
```

`profit_yield` is a **project margin, not an annualized return**, and is labelled "Yield (Profit / Costs)" everywhere it is shown so nobody reads it as an IRR. The broker's cut is subtracted from the sale price and is deliberately not in `total_costs`: it is a cost of selling, not a cost of the project.

No floor and no pass/fail flag. The Flip analysis is information; `SALE_PRICE_MISSING` reports what was not computed, not something that fell short.

### 8.5 Rental analysis

On by default for a hold exit, or when a `monthly_rent` has been entered (§8.1).

```
expenses           = monthly_rent × rental.expenses_pct_of_rent      # config, 35%
holding_monthly    = holding_costs_total_usd / term_months
net_monthly_income = monthly_rent − expenses − holding_monthly

debt_service       = level monthly payment on the commitment
                     at rental.takeout_rate (6.5%), rental.amortization_years (30)
dscr               = net_monthly_income / debt_service
```

Flag `DSCR_BELOW_FLOOR` (severity config, placeholder Soft) when `dscr < rental.dscr_floor` (placeholder 1.20).

With no `monthly_rent` the analysis is `NOT_EVALUATED`: `expenses`, `net_monthly_income` and `dscr` are all null, `DSCR_BELOW_FLOOR` is not tested, and `MONTHLY_RENT_MISSING` (Info, fixed in code) is raised instead. The loan amount, the takeout rate and the debt service do not depend on the rent and are reported regardless.

### 8.6 Take-Back analysis

Always on. It replaces the liquidation downside entirely: there is no REO haircut, no foreclosure cost, no foreclosure months by state, and no recovery-over-exposure cover. The question is no longer "what would a forced sale return" but "if GLENWOOD takes the property back and rents it, does the rent carry what the loan cost GLENWOOD".

```
loan_amount   = commitment
lost_interest = commitment × interest_rate / 12 × take_back.lost_interest_months   # config, 3
legal_costs   = take_back.legal_costs_usd                            # config, 5,000
total_cost    = loan_amount + lost_interest + legal_costs

debt_service  = level monthly payment on total_cost
                at the deal's own interest_rate, take_back.amortization_years (30)
dscr_at_loan_cost = net_monthly_income / debt_service      # net_monthly_income as in §8.5
```

Flag `TAKE_BACK_DSCR_BELOW_FLOOR` (severity config, placeholder Hard) when `dscr_at_loan_cost < take_back.dscr_floor` (placeholder 1.00).

Like the Rental analysis it is `NOT_EVALUATED` without a `monthly_rent`, and shares the one `MONTHLY_RENT_MISSING` flag with it. `loan_amount`, `lost_interest`, `legal_costs`, `total_cost` and `debt_service` stand on their own and are reported regardless. The Rental toggle does not gate it: a flip deal with a rent on it still gets a Take-Back.

### 8.7 Underwrite result

```
UnderwriteResult
  engine_version, config_hash
  loan_purpose?, closing_date, payoff_date, term_months, rehab_months
  exit: {type, exit_source}                                    # §3, informational
  sizing: {LTC, LTV, caps, pass/fail each, commitment, split}   # §8.2
  economics: {interest_rate, contingency_pct, rehab_adj, closing_costs,
              holding_costs_total, holding_costs_monthly, origination_fee_pct,
              origination_at_close, origination_at_payoff, commitment, funded_at_close}
  return_overview: {ledger: [{month, date, funding, draws, interest, fees, payoff, net}],
                    total_funding, total_draws, total_interest, total_fees,
                    total_payoff, total_profit, irr}           # §8.3
  flip: {status, estimated_sale_price, broker_costs, financing_costs,
         total_costs, net_profit, profit_yield}                # §8.4
  rental: {status, monthly_rent, expenses, holding_costs_monthly, net_monthly_income,
           loan_amount, debt_service_monthly, dscr, dscr_floor, passed}     # §8.5
  take_back: {status, loan_amount, lost_interest, legal_costs, total_cost,
              debt_service_monthly, net_monthly_income, dscr, dscr_floor, passed}   # §8.6
  flags: [ {code, severity, message} ]
```

All three analyses are always present, and `status` says what happened to each:

| `status` | Means |
|---|---|
| `EVALUATED` | it ran; every figure is there |
| `OFF` | the toggle is off, so it was not asked for (Flip and Rental only) |
| `NOT_EVALUATED` | the input it runs on is missing - the monthly rent, or the estimated sale price. The flag that names it is `MONTHLY_RENT_MISSING` or `SALE_PRICE_MISSING` (§8.8) |

An analysis that did not run still reports everything that does not depend on the missing
input: the Flip's whole cost stack, the Rental's debt service, the Take-Back's cost. What the
input feeds is `null`, `passed` included - a DSCR nobody could compute has not failed.

### 8.8 Underwrite flags

The §7.2 court codes and the credit, experience and leverage codes the screen raises appear here too, on the underwrite's own inputs and with the same severities. The five below are the underwrite's own: the two DSCR codes take their severity from config; the other three are fixed `Info` in code and config must not grade them.

| Code | Severity | Raised when |
|---|---|---|
| `DSCR_BELOW_FLOOR` | config | §8.5, the Rental DSCR is below `rental.dscr_floor` |
| `TAKE_BACK_DSCR_BELOW_FLOOR` | config | §8.6, the Take-Back DSCR is below `take_back.dscr_floor` |
| `MONTHLY_RENT_MISSING` | Info (fixed) | §8.5, §8.6: no `monthly_rent` on the deal, so neither DSCR is tested |
| `SALE_PRICE_MISSING` | Info (fixed) | §8.4: the Flip analysis is on and there is no `estimated_sale_price`, so it is `NOT_EVALUATED`. Not raised when the toggle is off — that is a decision, not a gap — and the screen's own `ESTIMATED_SALE_PRICE_MISSING` (Soft) reports the same absence on the leverage side either way |
| `NO_REHAB_PERIOD` | Info (fixed) | §8.3: a split product whose term leaves `rehab_months = 0`, so the rehab portion is advanced at close instead of drawn |

---

## 9. Outputs

One order, everywhere a run is shown — the deal page, the CLI report, and the workbook:

**Overview · Property Overview · Deal Economics · Return Overview · Flip Analysis · Rental Analysis · Take-Back Analysis · Flags**

The first three are the §8.1 input groups, shown back as the run read them; the rest are the
result. The deal page keeps its own furniture around that block — the run buttons and the
readiness checklist, the team-entry block, the screen summary, and the audit trail — and the
screen summary keeps its structure unchanged (§7).

### 9.1 Screen summary
One page in the review queue: intake facts, enrichment hits, score components, verdict, reasons, suggested reply, missing fields.

### 9.2 Readiness checklist
Above the Run underwrite button, one row per §8.1 input with the value in force, where it came from (`ADAPTER` / `TEAM` / `DEFAULT` / `MISSING`), whether the run needs it, and what the run does without it. The required set is four things and no more: `interest_rate`, `closing_date`, the term (`term_months` or `payoff_date`), and the loan split on a split product. `monthly_rent` is listed as optional, noted "without it the Rental and Take-Back analyses are not evaluated"; `estimated_sale_price` is optional too, noted for the LTV and the flip that go without it (§7.4, §8.4). It is derived from the same rules `services/assemble.py` refuses a run on, so the disabled button and the refusal behind it cannot name different things.

### 9.3 Credit memo
Generated from `UnderwriteResult` into GLENWOOD's template (to be supplied; docx). Sections: borrower, property, deal structure, sizing vs caps, the return overview (ledger and IRR), flip, rental, take-back, flags with pass/fail, recommendation. Every flag shows the threshold it was tested against.

### 9.4 LOI
docx merge from GLENWOOD's LOI template (to be supplied). Fields: borrower/entity, guarantor, property, loan purpose, product, commitment (and split for the two split products), interest rate, closing date, term and payoff date, origination split, conditions from flags. Generated only on team action.

### 9.5 Mortgage Automator handoff
On "LOI accepted": create borrower (if not matched), property, and loan in MA via API with the structured data; attach memo and LOI; record `ma_sync` row. MA is not written to before this point.

### 9.6 Verification tools (internal, not client deliverables)

`uv run glenwood run <fixture.json> [--underwrite]` prints a run — verdict, reasons, sizing
against caps, then the §9 sections in order: the deal economics, the full monthly ledger and
its IRR, the flip, the rental, the take-back, and the flags.

`uv run glenwood export <fixture.json> <out.xlsx>` writes the same run as a workbook, sheets
`Inputs`, `Return Overview`, `Flip`, `Rental`, `Take-Back`, `Flags`. Every figure is a number
under a currency, percent or date format — never preformatted text — so the cells add up and
compare. The `Return Overview` sheet carries the ledger with its dates and its `net` column
and an **`XIRR` formula over them**, so the workbook recomputes the IRR itself and a reader
can see the engine's number and Excel's agree rather than taking the engine's word for it.

Both read a fixture off disk and call the pure engine: no database, no network. They exist
so the math can be checked by hand against a spreadsheet; neither is shown to a borrower.

---

## 10. Configuration

`config/glenwood.yaml` — everything a GLENWOOD person might want to change lives here, nothing in code. The loader rejects a missing key, an **unknown key**, or an out-of-range value at startup, so a key this list does not name is a key the service will not start with.

- credit floor tranche; tranche cutoffs
- leverage caps: product × tranche × experience tier (placeholder grid). Each cell is `ltc` and `ltv` — two caps, since LTARV and the as-is LTV are gone (§7.4). The LTC placeholder is **100%**: no cap at all until GLENWOOD fills it
- screen tolerance band; court and filing flag severities, lookbacks, thresholds
- experience: repeat-borrower minimum tier
- fees, each the **default for a §8.1 input** except the broker's:
  - `origination_default_pct` (2.0%, half at close and half at payoff)
  - `contingency_default_pct` (0.00)
  - `closing_costs_default_usd` (1,000) — the lender's closing costs, inside the LTC denominator (§7.4, §8.2)
  - `holding_costs_default_pct_of_cost` (2%) of `purchase_price + rehab_costs`, total over the hold
  - `broker_selling_pct` (4%) — the flip's cost of selling; not an input
- `draws.listing_months` (3): `rehab_months = term_months − listing_months`
- exit inference term boundaries (resale max term, hold min term, §3)
- rental takeout: `expenses_pct_of_rent` (35%), `takeout_rate` (6.5%), `amortization_years` (30), `dscr_floor` (1.20)
- take-back: `lost_interest_months` (3), `legal_costs_usd` (5,000), `amortization_years` (30), `dscr_floor` (1.00)
- underwrite flag severities: `DSCR_BELOW_FLOOR`, `TAKE_BACK_DSCR_BELOW_FLOOR`. The informational codes (§8.8) are fixed `Info` in code and the loader refuses to grade them
- states served and court-record adapter per state

Gone with the v0.2 §8, and rejected by the loader if a stale yaml still carries them:
`returns` (target IRR, rate grid, month window), `takeout` (DSCR takeout LTV, rate,
amortization, floor, and the opex defaults for taxes / insurance / utilities / vacancy /
management / maintenance), `downside` (REO haircut, foreclosure cost, foreclosure months by
state, cover floor), `fees.origination_at_close_pct` / `origination_at_payoff_pct` (the split
is 50/50 in code), `fees.extension_default_pct`, `fees.selling_cost_pct` (now
`broker_selling_pct`), `fees.borrower_closing_pct_of_price` (now the `closing_costs_usd`
input), `draws.draw_avg_utilization` (the ledger makes it unnecessary), and — with v0.5 —
`leverage_caps.*.ltarv` and `leverage_caps.*.ltv_as_is`, which one `ltv` replaces (§7.4).

Config is versioned; each `screens`/`underwrites` row records the config hash used.

---

## 11. Compliance

- **Credit:** no hard pull without a signed borrower authorization on file (`credit_authorization_signed`); intake uses self-reported range only. Credit data is stored encrypted at rest, access logged.
- **SMS:** replies only to inbound senders; sent by a human via LinkedPhone; no automated outbound. Opt-out honored.
- **Listing sites:** address extraction from URLs only; no page scraping.
- **Business-purpose lending:** intake and LOI language reflect business-purpose loans; no consumer-purpose features.
- **Court/lien data:** used for underwriting decisions on business-purpose loans; retained with source and timestamp.
- **Access:** the review queue is behind a session cookie and nothing it serves is public. No
  self-signup and no password reset in v1 — a user is created and deactivated from the command
  line, so the list of people who can read credit and court findings is maintained on purpose.
  Deactivating ends every live session at once, because the user row is read on each request.
  Every form post carries a CSRF token bound to the signed-in user and expiring with their
  session; a post without one is refused and writes nothing. Every service write takes an
  actor and records an `audit_log` row, sign-in and sign-out included.

---

## 12. Phases

| Phase | Deliverable | Depends on |
|---|---|---|
| 0 | Recon of the six sources; adapter capability table finalized; config placeholder grid; this spec approved | — |
| 1 | Repo scaffold, schema, Postgres migrations, `IntakeRecord`, team-entry path only | 0 |
| 2 | Engine v1: sizing, screen scoring, the monthly ledger and its XIRR, flip, rental, take-back; unit tests on synthetic fixtures | 1 |
| 3 | Adapters: MA match, Forecasa, OSCN, URL-address parser; PropStream import | 0, 1 |
| 4 | Review queue page; SMS ingestion (LinkedPhone) with LLM parser; contract OCR | 1, 3 |
| 5 | Credco + RicherValues adapters; credit memo + LOI generation from templates | 2, templates |
| 6 | MA handoff | 5 |
| 7 | Back-test on 8–10 historical deals; calibrate the config placeholders against them | 2, fixtures |

Phase 5 replaced the underwrite calc layer and its inputs with the §8 above (engine `1.0.0`).
Phase 5a (engine `1.1.0`) followed it with the single value ratio (§7.4), the underwrite that
runs without a sale price (§8.4), and the §8.1 form as it now stands. The credit memo and LOI
generation Phase 5 also names wait on GLENWOOD's templates (§13).

Phase 4's review queue is built: sign-in and `users`, the queue list, the deal page, the team
actions, and the team-entry form. Its SMS ingestion and contract OCR wait on the LinkedPhone
recon (§13) — there is nothing to parse until the webhook shape is known, and a parser written
against a guess is a parser rewritten.

The queue list pins one thing above the status groups: a deal that picked up a Hard flag
**after** it reached `LOI_SENT` or `HANDED_OFF`. Past that point a Decline no longer closes a
deal (§4.6) — a person owns it and the flags are recorded for them to read — so nothing else
surfaces a Hard flag raised that late. "After" is measured against the `audit_log` row that
recorded the move into that status.

---

## 13. Open items

- LinkedPhone webhook availability
- PropStream export path
- Credco / RicherValues API vs PDF
- Colorado court-record source decision (CoCourts vs manual)
- GLENWOOD leverage/pricing caps (placeholder grid to be filled; the LTC cell is 100%, which
  is no cap at all, and the LTV cell is a 75% placeholder)
- LOI and credit memo templates
- Historical deals for fixtures
- The config placeholders the mechanics walkthrough left open: the contingency, holding-cost
  and broker percentages, the rental expense ratio and takeout rate, the two DSCR floors, and
  the take-back's lost-interest months and legal costs
