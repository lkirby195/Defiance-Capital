# GLENWOOD Underwriting Platform — SPEC v0.4

Status: v0.4 — §8.3–8.6 settled in the mechanics walkthrough (2026-09-10); the Phase 2b review decisions (one buy-side closing number, opex defaults on the as-is value, §3 exit inference, the two informational underwrite flags) folded in 2026-09-16; team overrides (§6.1) and the automatic status transitions (§4.6) added the same day. Owner: Logan. Client: GLENWOOD (hard money lender, OK + CO).
Companion file: `CLAUDE.md` (conventions for Claude Code).

The engine math in §8 is the simple, term-level model (no monthly ledger); a monthly version is a Phase 7 decision (§12).

---

## 1. Purpose

GLENWOOD receives a meaningful volume of loan inquiries, mostly via phone/SMS (LinkedPhone), and underwrites each manually. This system:

1. Takes a deal in with minimum friction from the borrower ("tell us the deal, we do the rest")
2. Enriches it from GLENWOOD's data sources
3. Screens it fast and cheaply (no paid pulls) to Go / Conditional / Decline
4. Fully underwrites the deals that clear the screen (paid pulls, valuation, sensitivity)
5. Produces a credit memo and a pre-populated LOI
6. Hands off accepted deals to Mortgage Automator for closing, draws, and servicing

Mortgage Automator is downstream only. This system is the system of record from inquiry through LOI.

**Non-goals for v1:** monthly cash-flow ledger, borrower-facing web page (goes on GLENWOOD's site later), automated outbound SMS, MLS/portfolio monitoring, public-filing monitoring on the existing book, servicing.

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
[ UNDERWRITE ]  paid pulls (credit, valuation) · sizing · IRR solve · sensitivity · exit/DSCR · downside
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
| `SPLIT_DRAW` | Single note, purchase portion + rehab holdback | On **full commitment** from close | Fix-and-flip |
| `SPLIT_PRINCIPAL` | Two notes: **Principal Note** (purchase) + **Tranche A** (rehab) | Principal Note from close; Tranche A on **drawn balance only** | Fix-and-flip, larger rehab |
| `WHOLETAIL` | Single note, full principal at close | On full principal from close | Buy below market, minimal work, retail resale; short term |

Common to all products:

- Interest paid current, monthly interest-only. No accrual, no deferral.
- Minimum interest period = full loan term. A stated loan term; v1 math models no early payoff, so it is never exercised (§8.4).
- Origination fee 2.0% of commitment: 1.0% at close, 1.0% at payoff.
- Extension fee: input, default 0.0% of commitment, charged if payoff month > term.
- Draws on Tranche A: straight-line over the rehab period, fully drawn at rehab completion (see §8.3).

Borrower intent and exit are inferred from term and asset type, then confirmed by the team.
`asset_type` is `SFR | UNITS_2_4 | UNITS_5_PLUS | OTHER`, captured at intake. The engine
applies these rules in order and records `exit_source`:

| # | Rule | Exit | `exit_source` |
|---|---|---|---|
| 1 | Team states an exit (anything but `UNKNOWN`) | as stated | `STATED` |
| 2 | Term ≤ 9 months and asset type is `SFR` or `UNITS_2_4` | resale: `WHOLETAIL` for the `WHOLETAIL` product, else `FLIP` | `INFERRED` |
| 3 | Term ≥ 12 months | `HOLD` | `INFERRED` |
| 4 | Neither fires (e.g. a 10-month term, or a ≤ 9-month term on `UNITS_5_PLUS`) | `UNKNOWN` | `INFERRED` |

A team-stated exit always wins, including a stated `HOLD` on a short term. The exit type is
informational: the DSCR takeout and the REO downside (§8.6) both run on every deal
regardless of it.

---

## 4. Intake

### 4.1 Minimum viable intake (any channel)

Five things. Everything else is derived or requested later, only if the deal clears the screen.

| # | Field | Notes |
|---|---|---|
| 1 | Property | Address, or a listing/auction link (address is extracted from the link) |
| 2 | Purchase price | Dollars |
| 3 | Rehab budget | Dollars; 0 allowed |
| 4 | Loan requested | Dollars. Borrower-driven — the engine derives leverage from this |
| 5 | How long do you need the loan? | Buckets: 3 / 6 / 9 / 12 / 12+ months |
| 6 | Who you are | Name, entity (if any), phone; **credit range** (pick one of the five tranches, §7.1); **real estate experience** (deals completed in last 3 years: 0 / 1–2 / 3–5 / 6+); **repeat borrower** yes/no |

(Numbered as six rows because "who you are" is one question with sub-fields.)

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
    name, phone, email?, entity_name?
    credit_range: T1..T5         # self-reported
    experience_bucket: 0 | 1_2 | 3_5 | 6_PLUS   # self-reported
    repeat_borrower: bool
  property:
    address_raw, address_normalized?, listing_url?
    county?, state: OK | CO | OTHER
  deal:
    purchase_price, rehab_budget, loan_requested
    term_bucket: 3 | 6 | 9 | 12 | 12_PLUS
    asset_type?: SFR | UNITS_2_4 | UNITS_5_PLUS | OTHER   # drives the §3 exit inference
    stated_exit?: FLIP | HOLD | WHOLETAIL | UNKNOWN
  team overrides (§6):           # stand-ins for enrichment, entered by hand
    as_is_value_team?, arv_team?
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
| Screen (§7) | `NEW` | `SCREENED`, or `DECLINED` on a Decline verdict |
| Underwrite (§8) | `SCREENED`, `IN_REVIEW` | `UNDERWRITING` |

A deal outside those starting states keeps the status it has: re-screening a deal the team
has already moved on does not drag it backwards, and re-underwriting one already in
`UNDERWRITING` is a no-op. **A `DECLINED` or `DEAD` deal cannot be underwritten** — the run is
refused and nothing is recorded, until a person re-opens it.

---

## 5. Data model (Postgres)

Tables (one-line intent each; full DDL via Alembic migrations):

- `borrowers` — person-level; phone is the primary match key; links to MA borrower id when matched
- `entities` — LLCs etc.; many-to-many with borrowers
- `intake_submissions` — every inbound message/file/form, immutable, raw
- `deals` — one per property × borrower inquiry; current `IntakeRecord` state lives here
- `properties` — normalized address, parcel, county, state; reused across deals
- `enrichment_runs` — one row per adapter call: source, timestamp, status, raw response ref, parsed result
- `screens` — screen inputs, score components, verdict, reasons; one per run (re-screen creates a new row)
- `underwrites` — full underwrite inputs, outputs, sensitivity grid (JSONB), version of engine used
- `documents` — credit reports, valuations, contracts, generated memos/LOIs; file storage ref + hash
- `ma_sync` — handoff log to Mortgage Automator: payload, MA ids, status
- `audit_log` — who changed what; required because credit and court data are in here

Money stored as `NUMERIC(14,2)`. Rates as `NUMERIC(7,5)`. All enrichment raw responses retained.

`screens` and `underwrites` are append-only: a re-run writes a new row and nothing is
updated in place. Each row records `engine_version` and `config_hash` (§10), so a result can
always be traced to the code and the tunables that produced it. The engine result is stored
whole in JSONB — `screens.score_components` holds components, sizing, and flags;
`underwrites.outputs` holds everything but the grid, which has its own column — so a stored
row rebuilds the exact `ScreenResult` / `UnderwriteResult`. `underwrites.solved_rate` is a
`NUMERIC(7,5)` copy of `r*` for querying; the JSONB carries full precision.

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
| RicherValues | Paid valuation (as-is, ARV) | API or PDF, depends on account | Underwrite only | Recon |
| Property data API (ATTOM / RentCast / similar) | Programmatic as-is value, rent estimate, comps when PropStream is manual | API, paid | Both | Optional |
| LinkedPhone | Inbound SMS/call capture | Webhook or polling | Intake | Recon |

Experience verification: count buy→sell pairs in the last 36 months across the borrower's known entities from deed history. Self-reported bucket is displayed next to the verified count; mismatch is a flag, not a fail.

### 6.1 Team overrides (the interim source)

Until an adapter exists for a value, the team is the source. The intake form takes a
hand-entered `as_is_value_team` and `arv_team`, and a `court_records_status` of
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
rehab_adj    = rehab_budget × (1 + contingency)          # contingency default 10%
buy_closing  = purchase_price × borrower_closing_pct      # 3%, borrower cash (§8.6); one number, screen and underwrite
total_cost   = purchase_price + rehab_adj + buy_closing
LTC          = loan_requested / total_cost
LTV_as_is    = loan_requested / as_is_value               # as_is from enrichment; if unavailable, purchase_price with a flag
LTARV        = loan_requested / arv                       # arv from enrichment/estimate; if unavailable, flagged and screen goes Conditional
```

Compare to caps in `config/glenwood.yaml`, keyed by product × credit tranche × experience tier. Placeholder grid ships with obvious dummy values; GLENWOOD fills.

### 7.5 Verdict

- **Decline**: any Hard flag, or credit below floor tranche, or any leverage metric above cap by more than the tolerance band (config, placeholder 5 pts)
- **Conditional**: Soft flags, leverage within tolerance band above cap, missing ARV/as-is, self-reported vs. verified mismatch, state = OTHER
- **Go**: none of the above

Every verdict carries a list of `reasons[]` in plain language for the team and a `suggested_reply` draft for the borrower (never auto-sent).

---

## 8. Underwrite (Stage 2)

Runs on Conditional/Go deals when a team member advances them. Paid pulls happen here, credit only after `credit_authorization_signed = true` on the deal.

§8.3–8.6 are the simple, term-level model settled in the mechanics walkthrough (2026-09-10). No monthly ledger. Structured so `engine/calc/` can be swapped for a monthly version without changing inputs or outputs.

### 8.1 Inputs

From intake + enrichment, plus:

- `as_is_value`, `arv` (RicherValues or team override); both required to underwrite
- `credit_score` (Credco, replaces self-reported tranche); verified deal count (deed history)
- `market_rent` (RentCast/PropStream/team), monthly, used for the DSCR takeout
- `annual_taxes`, `annual_insurance` (team actuals; config defaults as % of **as-is value** when absent) and `annual_utilities` (team input) → holding costs and the REO carry
- `asset_type` (from intake) and the team-stated exit → the §3 exit inference
- `term_months` (from bucket; 12+ → team sets)
- `rehab_months` = `term_months − listing_months` (config, placeholder 3; the last months of the term are listing and sale; floored at 0). Not a team input.
- `extension_fee_pct` (default from config, 0)
- `exit_price` (team-set retail price for wholetail; default `arv`)
- Config: `origination_pct = 0.02` (split 50/50), `selling_cost_pct = 0.06`, `contingency_pct = 0.10`, `borrower_closing_pct_of_price = 0.03`, `target_irr = 0.175`, rate grid, month window, DSCR and REO assumptions

### 8.2 Sizing

Same metrics as §7.4 with verified values. Product-specific:

- `NO_DRAW`, `WHOLETAIL`: `commitment = loan_requested`; LTV on as-is
- `SPLIT_DRAW`: `commitment = loan_requested`; `holdback = min(rehab_adj, commitment − purchase_portion)`; purchase portion defaults to `commitment − rehab_adj`, team can override
- `SPLIT_PRINCIPAL`: `principal_note = purchase portion`; `tranche_a = rehab portion`; `commitment = principal_note + tranche_a`

Output: pass/fail on each cap, with the cap and the actual.

### 8.3 Average outstanding balance

Needed because Tranche A accrues on drawn balance and the simple model has no monthly ledger.

- `rehab_months = max(0, term − listing_months)`; the last `listing_months` (config, placeholder 3) of the term are listing and sale.
- `NO_DRAW`, `WHOLETAIL`, `SPLIT_DRAW`: `avg_outstanding(m) = commitment` for all months.
- `SPLIT_PRINCIPAL`: Principal Note full from close. Tranche A is drawn straight-line over `rehab_months`, so its average utilization over the rehab period is `draw_avg_utilization` (config, 0.50), then it is fully drawn from rehab completion to payoff.

```
tranche_a_avg(m)   = tranche_a × [ rehab_months × u + (m − rehab_months) ] / m      # requires m ≥ rehab_months
avg_outstanding(m) = principal_note + tranche_a_avg(m)
```

where `m` = payoff month and `u` = `draw_avg_utilization`. Grid rows never run below `term` (§8.5), so `m ≥ rehab_months` always holds; the engine rejects a smaller `m`.

### 8.4 Lender return

Annualized yield on the **full commitment**, unlevered. Called "IRR" in outputs for continuity with GLENWOOD's language; a monthly XIRR replaces it in the ledger version. No early payoff is modelled: the minimum-interest term (§3) stays in the loan documents but is not exercised in v1 math.

Interest received is what the borrower pays. For payoff at month `m`, rate `r`:

```
interest(m, r)     = avg_outstanding(m) × r × m / 12
                   = commitment × r × m / 12                                             # NO_DRAW, SPLIT_DRAW, WHOLETAIL
                   = principal_note × r × m / 12
                     + tranche_a × r × (rehab_months × u + (m − rehab_months)) / 12      # SPLIT_PRINCIPAL
fees(m)            = commitment × origination_pct                                        # 1.0% at close + 1.0% at payoff, both on total commitment
                   + commitment × extension_fee_pct × [m > term]                         # default 0%; no rate step-up in extension
lender_yield(m, r) = (interest(m, r) + fees(m)) / commitment × 12 / m
```

**Rate solve:** find `r*` such that `lender_yield(term, r*) = target_irr` (0.175). Linear in `r`, closed form:

```
r* = ( target_irr × commitment × term / 12 − fees(term) ) / ( avg_outstanding(term) × term / 12 )
```

### 8.5 Sensitivity grid

One grid (there is no borrower grid; borrower economics are reported at `(term, r*)` only, §8.6):

- Columns: rate 10.0% → 15.0% in 50 bps (11 columns), plus `r*` inserted in rate order if not already on the grid
- Rows: month `term` → `term + 6`
- Cell: `lender_yield(m, r)`; cells ≥ `target_irr` flagged

Stored as JSONB on `underwrites`; rendered in the credit memo.

### 8.6 Borrower economics, takeout, downside

**Borrower economics** — information only (no floor, no flag), reported at `(term, r*)`:

```
rehab_adj           = rehab_budget × (1 + contingency_pct)       # lender funds and borrower spends the full contingency
buy_closing         = purchase_price × borrower_closing_pct      # 3%, borrower cash; the same number the screen puts in total_cost (§7.4)
total_project_cost  = purchase_price + rehab_adj + buy_closing
interest_paid       = interest(m, r)
fees_paid           = fees(m)
holding_costs       = (annual_taxes + annual_insurance + annual_utilities) / 12 × m
exit_price          = arv (flip), or team-set retail price (wholetail)
exit_net            = exit_price × (1 − selling_cost_pct)
profit              = exit_net − total_project_cost − interest_paid − fees_paid − holding_costs
cash_in             = total_project_cost + interest_paid + fees_paid + holding_costs − commitment    # ignores draw reimbursement timing
borrower_coc        = profit / cash_in                            # not reported when cash_in ≤ 0
```

**DSCR takeout** — runs on every deal regardless of stated exit:

```
gross_rent    = market_rent × 12
opex          = gross_rent × (vacancy + management + maintenance) + annual_taxes + annual_insurance
noi           = gross_rent − opex
takeout_ltv   = 0.75; takeout_rate = 7.5%, 30-yr amortization; dscr_floor = 1.20        # config
dscr_loan     = loan whose annual debt service at takeout_rate equals noi / dscr_floor
max_takeout   = min( arv × takeout_ltv, dscr_loan )
payoff_due    = commitment + payoff fees                                                  # the origination portion due at payoff
refi_covers   = max_takeout ≥ payoff_due
shortfall     = max(0, payoff_due − max_takeout)
```

Report `max_takeout`, `refi_covers`, `shortfall`, and the DSCR at `payoff_due`. Flag `REFI_SHORTFALL` (severity config) when `refi_covers` is false. Taxes and insurance are team actuals when supplied, else config defaults as % of the **as-is value** — the property is taxed and insured as it stands, not at its repaired value. The same two figures feed the holding costs above and the REO carry below, so a deal uses one tax number and one insurance number everywhere.

**REO downside** — every deal; no income or cap-rate valuation:

```
recovery_basis   = min( as_is_value + rehab_adj, arv )
liquidation      = recovery_basis × (1 − reo_haircut)                                    # 0.15
monthly_holding  = (annual_taxes + annual_insurance + annual_utilities) / 12
recovery         = liquidation × (1 − selling_cost_pct) − foreclosure_cost_usd − foreclosure_months[state] × monthly_holding
exposure         = commitment + unpaid fees                                              # the origination portion due at payoff
downside_cover   = recovery / exposure
```

Flag `DOWNSIDE_COVER_BELOW_FLOOR` (severity config) when `downside_cover < cover_floor` (1.0). `foreclosure_months` is config by state (placeholders OK 8, CO 4).

### 8.7 Underwrite result

```
UnderwriteResult
  engine_version, config_hash
  term_months, rehab_months
  sizing: {LTV, LTC, LTARV, caps, pass/fail each}              # §8.2 with verified values
  solved_rate                                                  # r*
  lender_yield_at_solve                                        # lender_yield(term, r*) = target_irr
  grid_lender                                                  # §8.5
  borrower_at_solve: {profit, cash_in, coc, ...}               # §8.6, information only
  exit: {type, exit_source, noi, dscr_at_payoff, max_takeout, payoff_due, refi_covers, shortfall}
  downside: {recovery_basis, liquidation, recovery, exposure, cover}
  flags: [ {code, severity, message} ]
```

Underwrite flag codes. `REFI_SHORTFALL` and `DOWNSIDE_COVER_BELOW_FLOOR` take their severity
from config; the two informational codes are fixed `Info` in code and config must not grade them:

| Code | Severity | Raised when |
|---|---|---|
| `REFI_SHORTFALL` | config | §8.6, `refi_covers` is false |
| `DOWNSIDE_COVER_BELOW_FLOOR` | config | §8.6, `downside_cover < cover_floor` |
| `NO_REHAB_PERIOD` | Info (fixed) | `SPLIT_PRINCIPAL` with `term ≤ listing_months`, so `rehab_months = 0` and Tranche A is fully drawn from close (§8.3) |
| `SOLVED_RATE_BELOW_GRID` | Info (fixed) | `r*` lands below `rate_grid.min`, including a negative `r*` where the fees alone exceed the target income at that term (§8.4). `r*` is reported as computed and inserted into the grid in rate order |

---

## 9. Outputs

### 9.1 Screen summary
One page in the review queue: intake facts, enrichment hits, score components, verdict, reasons, suggested reply, missing fields.

### 9.2 Credit memo
Generated from `UnderwriteResult` into GLENWOOD's template (to be supplied; docx). Sections: borrower, property, deal structure, sizing vs caps, pricing (solved rate + grid), exit, downside, flags with pass/fail, recommendation. Every flag shows the threshold it was tested against.

### 9.3 LOI
docx merge from GLENWOOD's LOI template (to be supplied). Fields: borrower/entity, property, product, commitment (and split for `SPLIT_PRINCIPAL`), rate, term, origination split, extension fee, min interest language, conditions from flags. Generated only on team action.

### 9.4 Mortgage Automator handoff
On "LOI accepted": create borrower (if not matched), property, and loan in MA via API with the structured data; attach memo and LOI; record `ma_sync` row. MA is not written to before this point.

### 9.5 Verification tools (internal, not client deliverables)

`uv run glenwood run <fixture.json> [--underwrite]` prints a run — verdict, reasons, sizing
against caps, `r*`, the yield grid, borrower economics, the DSCR takeout and the downside
cover — and `uv run glenwood export <fixture.json> <out.xlsx>` writes the same run as a
workbook (sheets `Inputs`, `Sizing`, `Lender`, `Grid`, `Borrower`, `Exit`, `Downside`,
`Flags`) with every figure as a number under a currency or percent format. Both read a
fixture off disk and call the pure engine: no database, no network. They exist so the math
can be checked by hand against a spreadsheet; neither is shown to a borrower.

---

## 10. Configuration

`config/glenwood.yaml` — everything a GLENWOOD person might want to change lives here, nothing in code:

- credit floor tranche; tranche cutoffs
- leverage caps: product × tranche × experience tier (placeholder grid)
- tolerance band, flag severities, lookbacks, thresholds
- fees: origination, split, extension default, selling cost, contingency, borrower buy-side closing (one number: screen LTC and underwrite, §7.4, §8.6)
- target IRR, rate grid, month window (rows after term)
- draw average utilization, listing months (rehab_months = term − listing months)
- exit inference term boundaries (resale max term, hold min term, §3)
- DSCR takeout assumptions (LTV, rate, amortization, DSCR floor), opex defaults (rent percentages on gross rent; taxes and insurance as percentages of the as-is value)
- REO haircut, foreclosure cost, foreclosure months by state, downside cover floor
- underwrite flag severities (refi shortfall, downside cover)
- states served and court-record adapter per state

Config is versioned; each `screens`/`underwrites` row records the config hash used.

---

## 11. Compliance

- **Credit:** no hard pull without a signed borrower authorization on file (`credit_authorization_signed`); intake uses self-reported range only. Credit data is stored encrypted at rest, access logged.
- **SMS:** replies only to inbound senders; sent by a human via LinkedPhone; no automated outbound. Opt-out honored.
- **Listing sites:** address extraction from URLs only; no page scraping.
- **Business-purpose lending:** intake and LOI language reflect business-purpose loans; no consumer-purpose features.
- **Court/lien data:** used for underwriting decisions on business-purpose loans; retained with source and timestamp.

---

## 12. Phases

| Phase | Deliverable | Depends on |
|---|---|---|
| 0 | Recon of the six sources; adapter capability table finalized; config placeholder grid; this spec approved | — |
| 1 | Repo scaffold, schema, Postgres migrations, `IntakeRecord`, team-entry path only | 0 |
| 2 | Engine v1: sizing, screen scoring, yield solve, grids, borrower econ, DSCR, downside; unit tests on synthetic fixtures | 1 |
| 3 | Adapters: MA match, Forecasa, OSCN, URL-address parser; PropStream import | 0, 1 |
| 4 | Review queue page; SMS ingestion (LinkedPhone) with LLM parser; contract OCR | 1, 3 |
| 5 | Credco + RicherValues adapters; credit memo + LOI generation from templates | 2, templates |
| 6 | MA handoff | 5 |
| 7 | Back-test on 8–10 historical deals; calibrate config; mechanics walkthrough → monthly ledger if warranted | 2, fixtures |

---

## 13. Open items

- LinkedPhone webhook availability
- PropStream export path
- Credco / RicherValues API vs PDF
- Colorado court-record source decision (CoCourts vs manual)
- GLENWOOD leverage/pricing caps (placeholder grid to be filled)
- LOI and credit memo templates
- Historical deals for fixtures
- Monthly ledger (true XIRR, draw timing in cash_in) if the Phase 7 back-test warrants it
