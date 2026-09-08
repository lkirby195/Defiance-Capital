# GLENWOOD Underwriting Platform — SPEC v0.1

Status: draft for review. Owner: Logan. Client: GLENWOOD (hard money lender, OK + CO).
Companion file: `CLAUDE.md` (conventions for Claude Code).

Sections marked **[v1 — revisit in mechanics walkthrough]** contain calculation assumptions that are placeholders until the model mechanics session.

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
- Minimum interest period = full loan term. Early payoff collects remaining scheduled interest as a lump at payoff.
- Origination fee 2.0% of commitment: 1.0% at close, 1.0% at payoff.
- Extension fee: input, default 0.0% of commitment, charged if payoff month > term.
- Draw curve for Tranche A: S-curve over the rehab period (see §8.3).

Borrower intent and exit are inferred from term and asset type, then confirmed by the team:

- Term ≤ 9 months + SFR/small multi → flip or wholetail (resale exit)
- Term ≥ 12 months, or borrower states hold → hold (refi/DSCR exit); run DSCR
- Every deal also gets the REO downside (§8.6) regardless of stated exit

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
    stated_exit?: FLIP | HOLD | WHOLETAIL | UNKNOWN
  missing_fields: [..]           # what the team still needs to ask for
  status: NEW | NEEDS_INFO | SCREENED | IN_REVIEW | UNDERWRITING | LOI_SENT | HANDED_OFF | DECLINED | DEAD
```

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
- `underwrites` — full underwrite inputs, outputs, sensitivity grids (JSONB), version of engine used
- `documents` — credit reports, valuations, contracts, generated memos/LOIs; file storage ref + hash
- `ma_sync` — handoff log to Mortgage Automator: payload, MA ids, status
- `audit_log` — who changed what; required because credit and court data are in here

Money stored as `NUMERIC(14,2)`. Rates as `NUMERIC(7,5)`. All enrichment raw responses retained.

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
| Tax lien, open | Forecasa / county | Hard |
| Active civil litigation as defendant, > threshold | OSCN / CO | Soft |
| Satisfied judgment / released lien within lookback | OSCN / Forecasa | Soft |
| Landlord-tenant matters (as landlord) | OSCN / CO | Info |
| Subject property: existing liens, lis pendens | Forecasa / PropStream | Hard if senior and unresolved at close |

Thresholds and lookbacks are config, not code.

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
total_cost   = purchase_price + rehab_adj + est_closing   # est_closing: config, placeholder 2% of price
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

**[v1 — revisit in mechanics walkthrough]** — the whole of §8.3–8.6 is the simple, term-level model. No monthly ledger. Structured so `engine/calc/` can be swapped for a monthly version without changing inputs or outputs.

### 8.1 Inputs

From intake + enrichment, plus:

- `as_is_value`, `arv` (RicherValues or team override)
- `credit_score` (Credco, replaces self-reported tranche)
- `market_rent` (RentCast/PropStream/team), used for DSCR and downside
- `monthly_holding_cost` (taxes, insurance, utilities; team input or estimate)
- `rehab_months` (team input; default from config by product)
- `extension_fee_pct` (default 0)
- `term_months` (from bucket; 12+ → team sets)
- Config: `orig_fee_pct = 0.02` (split 50/50), `selling_cost_pct = 0.06`, `contingency_pct = 0.10`, `target_irr = 0.175`, `coc_floor = 0.20`, rate grid, DSCR assumptions

### 8.2 Sizing

Same metrics as §7.4 with verified values. Product-specific:

- `NO_DRAW`, `WHOLETAIL`: `commitment = loan_requested`; LTV on as-is
- `SPLIT_DRAW`: `commitment = loan_requested`; `holdback = min(rehab_adj, commitment − purchase_portion)`; purchase portion defaults to `commitment − rehab_adj`, team can override
- `SPLIT_PRINCIPAL`: `principal_note = purchase portion`; `tranche_a = rehab portion`; `commitment = principal_note + tranche_a`

Output: pass/fail on each cap, with the cap and the actual.

### 8.3 Average outstanding balance **[v1]**

Needed because Tranche A accrues on drawn balance and the simple model has no monthly ledger.

- `NO_DRAW`, `WHOLETAIL`, `SPLIT_DRAW`: `avg_outstanding = commitment` for all months
- `SPLIT_PRINCIPAL`: Principal Note full from close. Tranche A follows an S-curve over `rehab_months`; the S-curve's average utilization over the rehab period is a config constant (placeholder 0.50 for a symmetric logistic curve), then 100% from rehab completion to payoff.

```
tranche_a_avg(m) = tranche_a × [ (rehab_months × s_avg) + max(0, m − rehab_months) ] / m
avg_outstanding(m) = principal_note + tranche_a_avg(m)
```

where `m` = payoff month.

### 8.4 Lender return **[v1]**

Simple annualized yield on average funded capital, unlevered. Called "IRR" in outputs for continuity with GLENWOOD's language; the monthly XIRR replaces it in the ledger version.

For payoff at month `m`, rate `r`:

```
interest_actual(m)   = avg_outstanding(m) × r × m / 12
interest_min         = avg_outstanding(term) × r × term / 12
interest_collected   = max(interest_actual(m), interest_min)        # min interest = full term
fees                 = commitment × orig_fee_pct
                     + commitment × extension_fee_pct × [m > term]
lender_yield(m, r)   = (interest_collected + fees) / avg_outstanding(m) × 12 / m
```

**Rate solve:** find `r*` such that `lender_yield(term, r*) = target_irr` (0.175). Linear in `r`, closed form.

### 8.5 Sensitivity grids **[v1]**

Two grids, same axes:

- Columns: rate 10.0% → 15.0% in 50 bps (11 columns), plus `r*` inserted if not on the grid
- Rows: month `term − 1` → `term + 6`

Grid A: `lender_yield(m, r)`; cells ≥ target flagged.
Grid B: `borrower_coc(m, r)` (§8.6); cells ≥ floor flagged.

Stored as JSONB on `underwrites`; rendered in the credit memo.

### 8.6 Borrower economics **[v1]**

Resale exit (flip, wholetail):

```
exit_net      = exit_price × (1 − selling_cost_pct)     # exit_price = arv (flip) or team-set retail price (wholetail)
interest_paid = interest_collected(m, r)
fees_paid     = fees(m)
carry         = monthly_holding_cost × m
total_cost    = purchase_price + rehab_adj + est_closing
profit        = exit_net − total_cost − interest_paid − fees_paid − carry
cash_in       = total_cost + interest_paid + fees_paid + carry − commitment    # simple: ignores draw reimbursement timing
borrower_coc  = profit / cash_in
```

Flag if `borrower_coc < coc_floor` at `(term, r*)`.

Hold exit (DSCR takeout), when intent is hold or term ≥ 12:

```
gross_rent   = market_rent × 12
noi          = gross_rent × (1 − vacancy) − taxes − insurance − mgmt − maintenance   # defaults in config
takeout_ltv  = config (placeholder 0.75)
takeout_rate = config (placeholder 7.5%), 30-yr amort
dscr_floor   = config (placeholder 1.20)
max_takeout  = min( arv × takeout_ltv,  loan amount where DSCR = dscr_floor at takeout_rate )
refi_covers  = max_takeout ≥ commitment + payoff fees
```

Flag if `refi_covers = false`; report the shortfall.

REO downside (all deals):

```
income_value    = noi / cap_rate                          # cap_rate: config by market, placeholder
liquidation     = min(as_is_value, income_value) × (1 − reo_haircut)   # placeholder 0.15
recovery        = liquidation × (1 − selling_cost_pct) − foreclosure_costs   # placeholder
exposure        = commitment + unpaid fees
downside_cover  = recovery / exposure
```

Flag if `downside_cover < 1.0`.

### 8.7 Underwrite result

```
UnderwriteResult
  sizing: {LTV, LTC, LTARV, caps, pass/fail each}
  solved_rate
  grid_lender, grid_borrower
  borrower_coc_at_solve, lender_yield_at_solve
  exit: {type, dscr, max_takeout, refi_covers, shortfall}
  downside: {liquidation, recovery, exposure, cover}
  flags: [ {code, severity, message} ]
  engine_version
```

---

## 9. Outputs

### 9.1 Screen summary
One page in the review queue: intake facts, enrichment hits, score components, verdict, reasons, suggested reply, missing fields.

### 9.2 Credit memo
Generated from `UnderwriteResult` into GLENWOOD's template (to be supplied; docx). Sections: borrower, property, deal structure, sizing vs caps, pricing (solved rate + grids), exit, downside, flags with pass/fail, recommendation. Every flag shows the threshold it was tested against.

### 9.3 LOI
docx merge from GLENWOOD's LOI template (to be supplied). Fields: borrower/entity, property, product, commitment (and split for `SPLIT_PRINCIPAL`), rate, term, origination split, extension fee, min interest language, conditions from flags. Generated only on team action.

### 9.4 Mortgage Automator handoff
On "LOI accepted": create borrower (if not matched), property, and loan in MA via API with the structured data; attach memo and LOI; record `ma_sync` row. MA is not written to before this point.

---

## 10. Configuration

`config/glenwood.yaml` — everything a GLENWOOD person might want to change lives here, nothing in code:

- credit floor tranche; tranche cutoffs
- leverage caps: product × tranche × experience tier (placeholder grid)
- tolerance band, flag severities, lookbacks, thresholds
- fees: origination, split, extension default, selling cost, contingency, est. closing
- target IRR, CoC floor, rate grid, month window
- S-curve average utilization, default rehab months by product
- DSCR takeout assumptions, opex defaults, cap rates by market, REO haircut, foreclosure costs
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
- Mechanics walkthrough: S-curve constant, cash_in definition, yield vs true IRR, minimum-interest treatment on Tranche A
