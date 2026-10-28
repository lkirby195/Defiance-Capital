# GLENWOOD Underwriting Platform — SPEC v0.19

Status: **v0.19**, 2026-10-28, engine `2.1.0`. Owner: Logan. Client: GLENWOOD (hard money lender, OK + CO).

**v0.19** replaces the two DSCR tests with the takeout and the lender's downside (engine `2.1.0`). The **Rental analysis** is sized from the exit value down (§8.5): the takeout loan is the **exit value** - a new per-deal input on the Underwriting Assumptions panel, the estimated sale price by default - times `rental.takeout_ltv` (placeholder 75%); its debt service is shown amortizing over `rental.amortization_years` and interest-only; the **required NOI** is the amortizing debt service times `rental.target_dscr` (placeholder 1.20, the renamed `dscr_floor`); the **required rent** is that NOI grossed up by the expense ratio; and beside it the market rent, the gap, the DSCR at the market rent on both bases and the **spread to loan** (the takeout loan less our payoff due). Holding costs are out of it. `MARKET_RENT_BELOW_REQUIRED` and `TAKEOUT_BELOW_PAYOFF` (both config, placeholder Soft) replace `DSCR_BELOW_FLOOR`; without a market rent the required rent and the spread still compute and only the market-rent lines are `NOT_EVALUATED`. The **Take-Back analysis** is the lender's own downside solved on its XIRR (§8.6): a default at the end of the rehab period, `lost_interest_months` of nothing, the legal bill, `take_back.foreclosure_months[state]` of carry at the deal's monthly holding cost, then a sale net of the broker. **Block A** bisects the sale price at which the lender's XIRR over the whole loan reaches `pricing.target_lender_irr` and reports it as a haircut from the projected sale price, a share of the purchase price and against the new optional **as-is value**; `TAKE_BACK_PRICE_ABOVE_PURCHASE` (config, placeholder Hard) when it is above the purchase price. **Block B**, per `take_back.hold_months` (12, 24, 36), solves the rent at the projected sale price and the sale price at the market rent for the same XIRR after a hold. Every solve carries its bounds and says "no solution" when it hits one. `TAKE_BACK_DSCR_BELOW_FLOOR`, `take_back.amortization_years` and `take_back.dscr_floor` are gone; a stored 2.0 row's two blocks still read. Migration `0022` adds `deals.rental_exit_value` and `deals.as_is_value`. The deal page cards, the workbook sheets and the CLI blocks follow; 10b moves the cards into the Scenarios tab.

**v0.18** settles three of the v0.17 questions. The pricing de-risk line's X is the **plain loan reduction** against the requested row - "Adding $19,000 of equity brings the rate to 13.63%" on a row $19,000 below the request - rather than the difference in borrower equity (§8.10, engine `2.0.1`). The borrower's thank-you page lists the **requested row first**, then each smaller loan from largest to smallest; the team's Pricing section keeps most equity first (§4.2). And the deal page's Underwriting Assumptions panel loses its Advance at Closing and Rehab Portion boxes for the team form's own two controls - the **Loan Structure** radio and the **Rehab portion** box it reveals - so the structure is edited there too and the advance is always the rest of the loan (§8.1, §9).

**v0.17** collapses the products to **two loan structures** and puts the borrower's side of the deal in full. `ACQUISITION` - "100% at close", the old `NO_DRAW` - advances the whole loan at closing; `ACQUISITION_REHAB` - "Holdback", the old `SPLIT_DRAW` - advances the purchase side at closing and holds the rehab portion back, drawn over the rehab period; both charge interest on the full commitment from close (§3). `SPLIT_PRINCIPAL` (the two-note ledger with Tranche A's as-drawn interest) and `WHOLETAIL` are gone from the engine, the caps grid and every form; migration `0021` maps the deals across (`SPLIT_PRINCIPAL` to a holdback whose rehab portion is its Tranche A, `WHOLETAIL` to 100% at close) and stored rows that name a retired code read as the structure it became. The team form asks for the structure as a radio right after the Loan Amount; Holdback reveals a **Rehab portion** box, by a stylesheet rule and no script, defaulting to the rehab budget times one plus the contingency, capped at the loan amount (§8.1). The **Flip and Rental toggles are gone**: all three analyses run on every deal, and a missing input is `NOT_EVALUATED` (§8.1). The Flip Analysis is printed line by line - sale price, broker, purchase, closing, rehab, contingency, holding, then the financing as interest and origination, to the profit, and the cost stack to the **total deal cost** - with the borrower's **equity** (total deal cost less the loan), **cash-on-cash** (profit over equity) against `pricing.borrower_coc_target` (placeholder 20%, `BORROWER_COC_BELOW_TARGET`, Soft) and the **project yield** (profit over total deal cost; the renamed "Yield (Profit / Costs)") (§8.4). The broker's default is **6%**. The Borrower Sensitivity's cells show the project yield (§8.9). New **Sources & Uses**: the advance at closing, the rehab holdback and the borrower's equity against the cost stack, equal to the cent (§8.11). Each Pricing row says "Adding $X of equity brings the rate to Y%", and the rows run most equity first (§8.10). Engine `2.0.0`.

**v0.16** settles four of the v0.15 questions. The deal page's **Actions** card opens on one line of small text, the status and the verdict ("Status: Underwriting · Verdict: Go"), above Progress / Pause / Kill; the header is unchanged except that "Loan Request" is whole dollars (§9). A Version name typed on the New Deal form makes the arrival version **human-selected**, so it holds the Working mark against the automatic runs after it. The arrival name's last name drops a Jr., Sr., II, III or IV before taking the last word, and a state outside OK and CO is the one typed in the address (`TX`, `Texas`) rather than left out (§5). No engine change.

**v0.15** is the deal page cleared down and the default rate put on the grid. The header is the address, four chips - the borrower, the entity, "Loan Request: $X" and the IRR to two decimals (a Draft chip beside them only while the page shows a version that is not Working) - and the phone on the line under them; the loan type, the term, the verdict, the status, the Working chip, the entered/via line and the verdict line are gone from it, and so are the version facts beside the switcher (when, who, engine, chosen by a person), which the History keeps. Every (?) definition is gone from the page, every IRR on it is two decimals, the sensitivity rows show the dollar amount without the step under it, the line of run facts above the ledger is gone and the ledger is **Cash Flows**, and **What the ledger runs on** is gone: the Underwriting Assumptions panel is the one list of what the ledger runs on (§9, §9.2). The two sensitivity tables' columns are the config grid alone - the deal's own rate is no longer inserted - and `interest.default_annual_rate` is **12.5%**, the grid's first column, so a deal on the default has its cell marked and one priced off the grid has none (§8.1, §8.9, engine `1.11.0`; the workbook and the CLI follow). The version a deal **arrives with** is named for it: `{last name}; {street number} {street name}, {state} - Request` on a web deal, `... - Team` on a team one unless the New Deal form's new optional **Version name** box says otherwise; every later run keeps `v<n>` / `Auto v<n>` / a typed name (§5).

**v0.14** steps the pricing matrix by percent and shows the borrower their indicative terms. The pricing rows are shares of the request - `pricing.loan_amount_pct_steps`, placeholder 0, -5%, -10% - each stepped row's amount rounded to the nearest `pricing.loan_amount_rounding` (placeholder $1,000), the request row never rounded, and each row labelled "Requested", "-5%", "-10%" (§8.10, engine `1.10.0`); the loader refuses the old dollar steps by name. A web submission is sent to `/apply/thanks/<token>`, a random link live for `web_intake.thanks_token_hours` (placeholder 24), and that page shows one table, **Indicative terms** - the quoted pricing rows' loan amount and rate, with the estimated profit and the cash-on-cash yield beside them when the borrower gave a sale price - under the required disclaimer, only when the arrival run said Go or Conditional, with rows above the maximum quoted rate left out and the plain thank-you when nothing qualifies. Nothing else of the analysis is shown. What was shown is recorded on the deal (`indicative_terms_shown`, migration `0020`) and printed on the deal page under Pricing as "Shown to borrower" (§4.2, §5).

**v0.13** is the deal page turned over: outputs on top, inputs below, on one visual system (one inline stylesheet, a system font stack, one accent, neutral grays, chips for the verdicts and severities, cards with a 1px border). The header is the address, one row of chips (borrower, loan type, loan amount, term, verdict, IRR) and the controls on the right - the version switcher with Download Excel, Run Analysis with its optional name, Edit Intake. Then Pricing; the two sensitivities side by side; Flip, Rental and Take-Back as three cards led by their headline figure; the Return Overview as one line with the ledger collapsed under it. Under the outputs the Underwriting Assumptions panel, then the intake as a read-only summary with the sizing table and the score components, then the collapsed sections. The CLI report and the workbook keep the §9 order. No engine, service or schema change (§9).

**v0.12** answers three of the v0.11 questions. A **maximum quoted rate** (`pricing.max_quoted_rate`, placeholder 18%): a pricing row whose solved rate is above it is not a quote and reads "exceeds max quoted rate (X% needed)" (§8.10, engine `1.9.0`). **Who holds the Working mark**: an automatic run - on arrival, after Edit Intake, after a court search save - becomes the Working version unless the Working version is one a person chose, by naming it as they ran it or with Mark as Working; then it is a draft. Each version records whether it was chosen by a person (`versions.human_selected`, migration `0019`) (§5). And a **Download Excel** button beside the version switcher exports the version in view as `<address>-<version>.xlsx` (§9).

**v0.11** is a solved price and versions. The **pricing matrix solves its rate** rather than reading it off the grid: for each loan amount step it bisects on the ledger for the lowest rate, to 0.01%, at which the lender's XIRR reaches the target, and reports that rate (two decimals of a percent, rounded up so it still clears), the IRR there and the borrower's profit and cash-on-cash there; the grid stays for the two sensitivity tables (§8.10, engine `1.8.0`). And every analysis run is a **version** (§5, §9): a name, the person or `system` it ran for, its screen and its underwrite, and a snapshot of every input it read, so it can be reproduced and its inputs restored whatever is edited later. Exactly one version per deal is **Working** - the first run is, and Mark as Working moves it - and the rest are drafts. The deal page shows the Working version, a switcher at the top of the outputs shows any other read-only with a Draft badge in the header, Restore inputs loads a version's snapshot back into the deal and runs a new version, and Home shows the Working version's verdict and IRR. Run Analysis and Save & Run take an optional Version name. History lists the versions, and the intake edits no run has picked up. The CLI prints or exports any version of a stored deal, the Working one by default (§9.6). Migration `0018` adds the `versions` table and backfills one version per stored run pair.

**v0.10** is the borrower's side and a pricing answer. The sensitivity table is the **Lender Sensitivity** everywhere it is shown, and a **Borrower Sensitivity** sits under it on the same rows and columns: each cell the Flip Analysis's net profit at that loan amount and rate and the borrower's cash-on-cash, `NOT_EVALUATED` when the flip is off or there is no sale price (§8.9). A **Pricing** section under both names, per `pricing.loan_amount_steps` entry, the lowest grid rate at which the lender's IRR reaches `pricing.target_lender_irr`, with the borrower's profit and cash-on-cash there and one plain-English line per row (§8.10, engine `1.7.0`). A read-only **Defaults** page (`/defaults`, linked from the top bar) lists every value in `config/glenwood.yaml`, grouped, with its dotted key (§10). And a fix: a team-form deal with no phone kept neither the guarantor's name nor the entity, because the borrower row they live on was only ever created for a phone; a phoneless borrower now gets its row (§5).

**v0.9** is the **Underwriting Assumptions** panel. The collapsed Inputs section is gone; directly under Deal Economics, open by default, sits a two-column panel of every number the ledger and the three analyses run on that the team may set by hand - the §8.1 economics with their config defaults, the loan split, the valuation, the rent, the two toggles and, new, the **five analysis assumptions** that only config carried until now: the broker's selling percentage (§8.4), the rental's expense ratio and takeout rate (§8.5), the take-back's legal costs and lost-interest months (§8.6). Each of the five is a column on `deals` (migration `0017`), populated with the config value and tagged `DEFAULT` like the economics, and the engine reads the deal's number when it carries one and config otherwise (engine `1.6.0`). Every box shows its value, carries the "default" tag while it holds its default, and has a reset of its own; one **Save & Run** saves the panel and runs the analysis as the person; **Reset all to defaults** puts every one back after a confirmation page and runs again. The court search is its own collapsed section with its own Save; the readiness checklist is collapsed below it (§9).

**v0.8** is one button and a leaner deal page. **Run Analysis** runs the screen and the underwrite together, as the person who pressed it, and the two stages are not named anywhere a person reads (§4.6, §9); the automatic runs stay. The deal page keeps the verdict line and loses the Flags, the Reasons and the Suggested Reply, which stay on the stored results, the CLI report and the workbook. Everything the team enters by hand is one collapsed **Inputs** section with Save at the bottom; a collapsed **History** lists every intake version, screen and underwrite with its actor and engine, and each version can be **restored** as a new submission (§9.1). The **closing date is optional on every channel** and defaults to the last day of the month `closing.default_lead_days` (14) after the deal came in, stored and tagged `DEFAULT` (§8.1), so the term is the one input that can keep a deal from being priced. A **sensitivity table** - IRR by loan amount and rate, every cell a full ledger re-run (§8.9, engine `1.5.0`) - sits under the Return Overview and in the workbook.

**v0.6** is the queue running on its own. Every deal is **populated with its §8.1 defaults at intake** — the config rate (new: `interest.default_annual_rate`, placeholder 12%), the four config fees and, on a split product, the §8.2 formula loan split — stored on the deal, tagged `DEFAULT`, editable and resettable; a `DEFAULT` counts as present, so only the closing date and the term can turn Run underwrite off (§8.1, §8.2, §9.2). A complete intake is **screened and priced on the way in** on every channel, and run again after every team edit, as the actor `system` (§4.6). The deal page is labels and values only, with every definition behind a (?) on its label (§9); the public form's consent is the line above its button (§4.2).

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

**Non-goals for v1:** automated outbound SMS, MLS/portfolio monitoring, public-filing monitoring on the existing book, servicing.

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

Two loan structures (v0.17). The team picks one with a radio on the team form; the
public form does not ask, and every channel infers one from the rehab budget when nobody has
picked - a holdback when there is a budget, 100% at close when there is none.

| Product | Read as | Structure | Interest |
|---|---|---|---|
| `ACQUISITION` | "100% at close" | One note, the whole loan advanced at closing | On the full commitment from close |
| `ACQUISITION_REHAB` | "Holdback" | One note: the **advance at closing**, then the **rehab portion** held back and drawn over the rehab period | On the **full commitment** from close |

Before v0.17 there were four: `NO_DRAW` and `WHOLETAIL` (both now `ACQUISITION`), `SPLIT_DRAW`
(now `ACQUISITION_REHAB`) and `SPLIT_PRINCIPAL` - two notes, a Principal Note at close and a
Tranche A drawn, with interest on Tranche A's drawn balance only - which is gone with its
two-note ledger. Migration `0021` maps every deal across (a `SPLIT_PRINCIPAL` deal becomes a
holdback whose rehab portion is its Tranche A), and a stored run, intake payload or version
snapshot that names a retired code reads as the structure it became
(`schema.models.LEGACY_PRODUCTS`); nothing writes one.

Common to all products:

- Interest paid current, monthly interest-only, at the deal's `interest_rate` (§8.1). No accrual, no deferral.
- A stated loan term in whole months. The loan runs to `payoff_date` - the last day of the month that is the closing month plus the term (§8.1) - and pays off there: v0.3 models no early payoff, and there is no minimum-interest exercise and no extension in the math.
- Origination fee `origination_fee_pct` of the commitment (default 2.0%, config): half at close, half at payoff.
- The rehab side is drawn straight-line over `rehab_months = term_months − listing_months` (config, placeholder 3), floored at 0 — the last months of the term are listing and sale. A term at or inside the listing period leaves no rehab period, and the rehab money is advanced at close instead (`NO_REHAB_PERIOD`, §8.8).
- 100% at close carries no rehab portion: `loan_rehab_portion` is 0 and the whole commitment is funded at close.

**There is no exit inference and no asset type** (v0.7). A deal used to carry an asset type
and a stated exit, and a resale exit turned the Flip analysis on while a hold exit turned the
Rental analysis on. Both columns are gone from the form, the deal page and the model
(migration `0016`). From v0.17 there are no analysis toggles either: the Flip, Rental and
Take-Back analyses all run on every deal, and one whose input is missing - the sale price, the
rent - says `NOT_EVALUATED` (§8.1).

Each product's row above is its definition in `schema/labels.py`; neither the deal page nor
the team form prints it (v0.15 took the deal page's (?) off), only the label.
Every enum a person reads is rendered in title case with spaces (`CASH_OUT` is "Cash
Out") wherever it appears (`schema/labels.py`), but for the two structures, which read as the
radio names them - "100% at close" and "Holdback"; the stored value does not change.

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
| 5 | How long do you need the loan? | The term. The borrower channels ask it as a bucket (3 / 6 / 9 / 12 / 12+ months) which seeds `term_months`; 12+ seeds 12 on every channel, the floor of the ask, and the deal page says the borrower asked for more so the team sets the real term. The team form asks for the term in months (§8.1), because a person with the whole deal in front of them knows it; the payoff date derives |
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
| Web form | The public page at `/apply`, no sign-in; linked from GLENWOOD's site, a flyer, a partner. English and Spanish | Direct; `intake/parsers/web_form.py` |

**The web form** asks the §4.1 minimum in plain language and in the borrower's own
language - every string on the page is in a translation table with an English and a
Spanish column, the page defaults from `Accept-Language` and has a toggle, and nothing it
stores changes with the language. It asks no loan type: the normalizer infers one from the
rehab budget as it does on every channel, and the team confirms it. The listing link is
optional and, when the address boxes are left blank, the address is read off the link's
slug and never off the page (§4.4). The borrower's own guesses at the sale price after the
rehab and at the rent, if offered, are stored as the borrower's
(`estimated_sale_price_borrower`, `monthly_rent_borrower`, source `BORROWER`): the engine
runs on them only when no adapter and no team member has a number, flags the run
`BORROWER_SOURCED_VALUES` when it does, and a team entry replaces them as the value in force
without clearing them (§6.1).

Three things are recorded about where a web deal came from: the channel `WEB`; the
`intake_source`, which is the `?src=` slug on the link the borrower followed; and the
`referral_note`, their answer to "how did you hear about us?". The deal lands in the queue
as the normalizer decides: `NEEDS_INFO` while something is missing, else **screened and
priced on the way in** (§4.6) on the config defaults (§8.1) and the borrower's own numbers,
with no court record. The 12+ term bucket seeds `term_months` at 12, as on
every channel (§4.1). Sending the form is the borrower's consent to being contacted: the
line above the button says so, in both languages, and the stored submission records it.

**The thank-you page** (v0.14). The submission is sent to `/apply/thanks/<token>`: a random,
unguessable token stored on the deal (`deals.thanks_token`) and live for
`web_intake.thanks_token_hours` (§10, placeholder 24); an expired or unknown token, and the
bare `/apply/thanks`, render the plain thank-you. The page says the team will be in touch
and, when the arrival run allows it, shows one table titled **Indicative terms**, one row per
pricing step (§8.10) that quotes a rate: Loan amount, Rate, Estimated profit, Yield
(cash-on-cash). The rows read the **requested row first, then each smaller loan from largest to
smallest** (v0.18) - what the borrower asked for before what less would cost; the team's
Pricing section runs the other way, most equity first. The rules:

- shown only when the automatic screen on arrival (version 1) said Go or Conditional, and the
  ledger ran; a Decline, an incomplete intake and a run that stood down show the plain page
- a row whose solved rate is above `pricing.max_quoted_rate` is left out, as is one nothing
  prices; when no row qualifies, the plain page
- when the borrower gave no sale price, the table has Loan amount and Rate only and a line
  under it: "Add an estimated sale price for a profit estimate."
- the results are the arrival run's, not the Working version's: what the team enters
  afterwards stays on the team's side until a person calls
- nothing else of the analysis is shown - no verdict, no IRR, no flags, no sensitivity

Above the table, in both languages: *"These figures are indicative only, based on the
information you provided and our standard assumptions. They are not an offer, commitment,
or guarantee of terms. A member of our team will review your request and contact you."*
Below it: *"For business-purpose loans only."* The first time the table is shown it is
recorded on the deal - the rows as rendered and the language (`indicative_terms_shown`,
JSONB) and when (`indicative_terms_shown_at`) - and in the audit trail
(`INDICATIVE_TERMS_SHOWN`, actor `web:<email>`); a refresh shows the same rows and records
nothing new. The deal page prints the record under Pricing as **Shown to borrower**, or
"Nothing shown to the borrower." on a web deal whose page never showed the table. The link's
issue is audited too (`THANKS_TOKEN_ISSUED`, the expiry and never the token).

What stands between the page and the queue is a honeypot field, which drops a post that
fills it without a word, and a per-address rate limit
(`web_intake.submissions_per_hour_per_ip`, §10) that turns the next post away with a polite
line. No CAPTCHA. The page carries no CSRF token: there is no session to bind one to, and
nothing a forged post could do that an honest one cannot (§11).

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
  id, created_at, channel: SMS | LINK | CONTRACT | TEAM | WEB
  raw_payload                    # original message / file ref / form data
  intake_source?, referral_note? # the web form's provenance (§4.2); on no other channel
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
    loan_purchase_portion?, loan_rehab_portion?          # §8.2, a holdback only; the first is
                                                         #   "Advance at Closing"
    term_bucket?: 3 | 6 | 9 | 12 | 12_PLUS               # borrower channels only; it seeds term_months
    term_months?, term_stub_days?, payoff_date?
                                 # the term in whole months; payoff_date derives: the last day
                                 #   of the month that is the closing month plus the term
                                 #   (§8.1). term_stub_days is kept for a future actual-payoff
                                 #   entry and no form writes it
    closing_date?                # any date; the ledger's month 0 is the end of its month (§8.3)
    interest_rate?               # annual; required to underwrite (§8.1)
    contingency_pct?, closing_costs_usd?, holding_costs_pct_of_cost?, origination_fee_pct?
                                 # §8.1 deal economics; each falls back to its config default
  borrower estimates (§4.2):     # the public form's own; never overwritten, source BORROWER
    estimated_sale_price_borrower?, monthly_rent_borrower?
  team overrides (§6):           # stand-ins for enrichment, entered by hand
    estimated_sale_price_team?, monthly_rent?
    court_records_status?: NOT_CHECKED | CLEAN | FLAGS
    court_records_as_of?         # the day the team searched; required for CLEAN and FLAGS
    court_records_team: [..]     # one typed matter per entry, §7.2
  missing_fields: [..]           # what the team still needs to ask for
  status: NEW | NEEDS_INFO | SCREENED | IN_REVIEW | UNDERWRITING | LOI_SENT | HANDED_OFF | PAUSED | DECLINED | DEAD
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

**Both runs are automatic** (`services/autorun.py`). A deal that arrives complete, on any
channel, is screened and then priced before anyone opens it, on whatever it carries: the
§8.1 defaults, the borrower's own values, no court record. Saving the override block or
Edit Intake runs both again on what was saved. The underwrite stands down by name when the
deal is short of a closing date or a term, when the engine will not price what is there, or
when the screen declined it; the edit that triggered the run is kept either way, and the page
says what ran. Every automatic run is recorded against the actor `system` (§11). One button
stays - **Run Analysis** - which runs both again, as the person who pressed it, on any deal
a paused one included; the page names no stage, only the verdict and the ledger.

**The three controls on a Home row** (§9.1) are team actions, and each writes an `audit_log`
row (§5):

| Control | From | To |
|---|---|---|
| Progress | `SCREENED` | `IN_REVIEW`, as advancing to review always did |
| Progress | `PAUSED` | the status the deal was paused from, read off the `PAUSED` audit row's `before` |
| Pause | any live status (not `PAUSED`, `DECLINED`, `DEAD`) | `PAUSED` |
| Kill | anything but `DEAD`, after a confirmation page | `DEAD` |

**A `PAUSED` deal is set aside.** Nothing automatic runs on it - an edit on a paused deal is
stored and waits - a re-screen or a re-underwrite a person presses leaves its status alone,
and Progress returns it to where it was. It is not closed: it can be edited, declined or
killed, and it sits in its own section of the Home page until somebody brings it back.

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
- `versions` — one per analysis run: deal_id, number, name, created_at, actor, engine_version, config_hash, inputs_snapshot (JSONB), screen_id, underwrite_id, is_working
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

`0013` adds `WEB` to the `channel` enum and `deals.intake_source` / `deals.referral_note`
(§4.2). Its downgrade relabels every `WEB` row `TEAM` before rebuilding the enum without the
value, rather than deleting a deal a borrower typed in.

`0014` adds `deals.estimated_sale_price_borrower` and `deals.monthly_rent_borrower` (§4.2,
§6.1) and moves each untouched `WEB` deal's two numbers out of the team columns into them.
No run is deleted: engine `1.3.0` adds to two enums and gives `UnderwriteInputs` one optional
field, and every stored row still rebuilds.

`0020` adds the thank-you link and its record (§4.2): `deals.thanks_token` (unique) and
`thanks_token_expires_at`, and `indicative_terms_shown` (JSONB: the language, whether the
profit columns were on it, and the rows as rendered) with `indicative_terms_shown_at`. All
four are nullable and no row is backfilled: a deal stored before it was sent to the plain
page. None of the four is an input, so a version snapshot leaves them out and a restore
leaves them be. Engine `1.10.0` adds `pct_step` to a pricing row and no run is deleted: a
`1.9.0` row loads with it None and is labelled by its dollar step.

**Versions** (`0018`). A version is one analysis run - the screen and the underwrite Run
Analysis, Save & Run, Reset all, Restore inputs and every automatic run produce together
(§4.6) - numbered from 1 per deal, named, and recorded against the person or `system` it ran
for. `inputs_snapshot` is everything the run read: the intake submission in force (its id,
channel and payload), every `deals` column but the row's identity, channel, status,
public-form provenance and timestamps (so the §8.1 economics, the split, the valuation, the
rent, the five analysis assumptions, the court search and the `defaulted_fields`
tags all travel), and the borrower and property the deal pointed at. The column list is read
off the table, so a column added later is snapshotted without anyone remembering to.
`underwrite_id` is empty when the ledger stood down. **Exactly one version per deal is
Working**, and a partial unique index refuses a second Working row. Who holds the mark (v0.12):

| Run | Becomes |
|---|---|
| the first run on a deal, of any kind | Working |
| an automatic run (on arrival, after Edit Intake, after a court search save, after an intake restore) | Working, unless the Working version is human-selected; then a draft |
| a person's run (Run Analysis, Save & Run, Reset all, Restore inputs) | a draft |
| Mark as Working | Working, and human-selected |

**Names** (v0.15, v0.16). A person's run is the name they typed beside Run Analysis or Save &
Run, else `v<number>`; an automatic run is `Auto v<number>`. The one exception is the run a
deal **arrives with** on the web or the team channel, which is named for the deal:
`{last name}; {street number} {street name}, {state} - Request` on a `WEB` deal and
`... - Team` on a `TEAM` one (`Whitlock; 1412 S Trenton Ave, OK - Team`). The last name is
the last word of the borrower's full name once a trailing Jr., Sr., II, III or IV is taken
off (`Robert Smith Jr.` is Smith); the street is the address line up to its first comma; the
state is the two-letter code on an OK or CO deal and otherwise the state as typed in the
address after the street - a trailing two-letter code (`Amarillo, TX 79101` is TX) or, when a
city stands between them, the last part (`Amarillo, Texas`). A piece the deal does not have
is left out (the web form's line outside OK and CO is the street and the city, with no state
to give), the street is shortened to keep the name within 100 characters, and a deal with
neither a name nor an address is `Auto v1`. The New Deal form has an optional **Version
name** box above its button; what is typed there names the arrival version instead and,
being a person's choice, makes it **human-selected** (v0.16): it holds the Working mark
against the automatic runs after it, exactly as a named Run Analysis does. The deal's own
name is nobody's choice. The other channels keep `Auto v1`.

A version is **human-selected** (`versions.human_selected`, migration `0019`) when the person
who ran it typed its name, or a person marked it Working - marking the version already
Working records the choice too. A choice somebody made is not overwritten by the run after
the next edit; an unchosen Working version is, so the page and Home keep up with the inputs
until somebody picks a version to hold. `is_working` and `human_selected` are the two
columns on the table that ever change; `VERSION_RECORDED` names the version an automatic run
took the mark from (`replaced_working`). A run the screen refused wrote nothing and is no
version; the JSON stage routes (`POST /deals/{id}/screen`, `/underwrite`) run one stage and
record no version.

`0019` backfills `human_selected`: true on a version whose name is neither `v<number>` nor
`Auto v<number>` (a person typed it) and on one a `VERSION_MARKED_WORKING` row names.

`0018` backfills the runs already stored: each `screens` row with the first `underwrites` row
after it and before the next screen is one version, an underwrite left over is a version of
its own beside the screen before it, numbered in order, named `v<number>`, the latest
Working, the actor off the run's audit row. Their snapshot is `{"legacy": true}` and the
submission then in force - the runs never recorded the deal they came from - so a backfilled
version is listed and viewable but cannot be restored.

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

Precedence is fixed: **an adapter value always wins over a team value, and a team value
wins over the borrower's own** — the estimated sale price and monthly rent a borrower types
on the public form (§4.2), stored in their own columns. Every superseded value stays on the
deal, so a later reader can see what was entered by hand, what the borrower claimed, and what
superseded each: a team entry replaces a borrower value as the one the engine runs on and
does not clear it. Every value the engine ran on records its source, `ADAPTER`, `TEAM` or
`BORROWER`, on the stored screen and underwrite (§5) — a Go that rests on a hand-entered
valuation and a hand-done court search is a different thing from a Go that rests on a pull,
and a Go that rests on the applicant's own guess is a third, and the verdict alone does not
say which it is. The readiness checklist (§9.2), the deal page, the CLI report and the
workbook each say "Borrower" where that is where a value came from.

`NOT_CHECKED` is not `CLEAN`: the first is reported as an INFO flag, the second is a clean
record dated the day the team searched.

Both stages also raise `BORROWER_SOURCED_VALUES` (Info, fixed in code) when a value they ran
on carries source `BORROWER`, naming which — the estimated sale price, the monthly rent, or
both — and it sits beside `TEAM_SOURCED_VALUES` when one input is the team's and another the
borrower's. The court record is never the borrower's: the public form asks for no search.
The rent is named only by the borrower flag, on purpose: nothing pulls a rent, so a team rent
is not "by hand rather than pulled", while a borrower's rent is the one number on the deal
nobody at GLENWOOD has looked at.

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

Three analyses sit beside the ledger, each asking a different question about the same deal. **Flip** (§8.4): does the project make money if it is sold, and what does the borrower make on the money they put in? **Rental** (§8.5): what does it have to let for to refinance us out, and how far is the market rent from that? **Take-Back** (§8.6): what does it have to sell or let for, after a default, for the lender to still earn its target? All three run on every deal (v0.17; Flip and Rental were toggles before it), and the Take-Back is the lender's downside (v0.19; a DSCR test on the lender's own cost from v0.4 to v0.18). **Sources & Uses** (§8.11) lays the money out beside them.

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
| `product` | inferred from `rehab_costs` | The §3 structure: a radio on the team form, "100% at close" / "Holdback", right after the Loan Amount, labelled **Loan Structure** wherever a person reads it. Left unpicked, it is inferred - a holdback when there is a rehab budget, 100% at close when there is none - as on every channel |
| `closing_date` | the last day of the month `closing.default_lead_days` (14) after the deal came in | Optional on every channel. Any date; the model treats closing as the **last day of that month**: month 0 of the ledger is that month end (§8.3). Populated at intake when absent, stored and tagged `DEFAULT`, edited on the deal page; readiness never blocks on it |
| `term_months` | — | Whole months. **Required to underwrite** - the one input nothing stands in for; the team form requires the box, the public form seeds it from the bucket. `payoff_date` derives: the last day of the month that is the closing month plus the term, shown read-only on the deal page |
| `purchase_price` | — | |
| `rehab_costs` | — | 0 allowed |
| `loan_requested` | — | Labelled **Loan Amount** wherever it is shown |
| `loan_purchase_portion`, `loan_rehab_portion` | the §8.2 formula, on a holdback | §8.2; the Loan Amount is their sum. The first is the **Advance at Closing**, the second the **Rehab portion**. Only the rehab portion is typed: on the team form and on the deal page's Underwriting Assumptions panel alike (v0.18), Holdback reveals one box, **Rehab portion** (optional): typed, the advance is the rest of the loan, and a portion above the loan amount is refused under the box; blank, it is the default - the rehab budget times one plus the contingency, capped at the loan amount. On the panel the radio shows the deal's structure and the box its rehab portion, tagged "default" while it is. Nothing on 100% at close: one advance, the whole loan |
| `interest_rate` | `interest.default_annual_rate` (12.5%, placeholder; the sensitivity grid's first column) | Annual. **Required to underwrite**; the default stands until the team enters the deal's own rate |
| `contingency_pct` | `fees.contingency_default_pct` (0.00) | `rehab_adj = rehab_costs × (1 + contingency_pct)` |
| `closing_costs_usd` | `fees.closing_costs_default_usd` (1,000) | The lender's closing costs. It replaces the 3%-of-price borrower closing assumption, which is gone |
| `holding_costs_pct_of_cost` | `fees.holding_costs_default_pct_of_cost` (2%) | A **percentage of `purchase_price` + `rehab_costs`**, over the whole hold. The dollar figure is that percentage of that cost and is computed, never entered: `holding_costs_total = (purchase_price + rehab_costs) × holding_costs_pct_of_cost`, shown beside the box and everywhere holding costs appear. The monthly figure is `holding_costs_total / term_months` as a decimal (below) |
| `origination_fee_pct` | `fees.origination_default_pct` (2.0%) | Half at close, half at payoff, both on the commitment |

**The ledger anchors to month ends.** Closing is treated as the last day of its month,
whatever day the deal closes on, and every period after it is the last day of a later month:

```
anchor(m)    = the last day of the month that is closing_date's month + m      # m = 0 ..
payoff_date  = anchor(term_months) + term_stub_days days
term in months (decimal) = term_months + term_stub_days / interest.day_count_basis
```

A nine-month term on a deal closing 15 October pays off on 31 July. The team enters the term
in months and nothing else; the payoff date is derived and shown read-only. **No form
produces a stub.** The engine keeps the stub arithmetic — a payoff date between two anchors is
`term_months` whole periods plus `term_stub_days` days, measured from the month-end anchor — for
a future actual-payoff entry, and the JSON underwrite request can still name a payoff date for
it; a payoff date on or before the end of the closing month is refused, because the ledger's
month 0 is that month end. `term_months` may be 0 only alongside a stub; 0 months and 0 days is
not a term.

`term_stub_days` is a column on `deals` beside `term_months` (migration `0012`); the payoff
date is derived from the two and never stored.

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
fraction `0.12`), and a phone `555-123-4567` (the deal carries the digits). Small
dependency-free client-side script formats what is typed; the server parses either shape, so
a browser that runs none of it still posts a deal that saves.

**Analysis assumptions** — the five numbers the three analyses read that a deal may carry its
own value for (v0.9). Each has the config value as its default and follows the same rule as
the economics above.

| Input | Default | Notes |
|---|---|---|
| `broker_selling_pct` | `fees.broker_selling_pct` (6%; 4% before v0.17) | Of the sale price: the flip's cost of selling (§8.4). Labelled **Broker Selling Costs %** |
| `rental_expenses_pct_of_rent` | `rental.expenses_pct_of_rent` (35%) | Operating expenses as a share of the rent: the required rent grosses the required NOI up by it, the market rent nets down by it, and the take-back's hold collects rent net of it (§8.5, §8.6). **Rental Expense %** |
| `rental_takeout_rate` | `rental.takeout_rate` (6.5%) | The rate a takeout lender amortizes the commitment at (§8.5). **Rental Takeout Rate** |
| `take_back_legal_costs_usd` | `take_back.legal_costs_usd` (5,000) | The legal bill in the take-back's total cost (§8.6). **Take-Back Legal Costs** |
| `take_back_lost_interest_months` | `take_back.lost_interest_months` (3) | Months of interest GLENWOOD stops collecting, at the deal's own rate (§8.6). **Take-Back Lost Interest Months**, a whole number |

The takeout LTV, the amortization years, the target DSCR, the foreclosure months by state and the hold periods are config alone (§10).

**Every deal is populated with its defaults** (`services/defaults.py`). The five economics
with a config default — the interest rate, the contingency, the closing costs, the holding
costs and the origination fee — the five analysis assumptions above and, on a holdback,
the §8.2 formula loan split are written onto the deal at intake on every channel, so the
number the engine runs on is the number the page shows. `deals.defaulted_fields` names the
ones that are stand-ins rather than a person's choice; the Underwriting Assumptions panel
pre-fills each with its default, tags it "default", and offers a reset link, and the deal
page tags the value. **A value equal to the default is the default**: a box left holding the
number it was pre-filled with has not been chosen. A box typed over is the team's; a box left
blank gets the default back; **Reset all to defaults** puts every one back at once, after a
confirmation page - the sale price, the as-is value and the rent have no default and stay, and the exit value goes back to the sale price. A deal stored before this existed is populated the first time it is opened, once, with a
team value already on it never touched; until then the engine and the checklist read a blank
as the default it would be given. The readiness checklist (§9.2) says `DEFAULT` for each and
counts it as present.

**Valuation and rent**

| Input | Notes |
|---|---|
| `estimated_sale_price` | The ARV. LTV is computed on it (§7.4, §8.2) and the Flip analysis sells at it. Optional: without one, LTV is `NOT_AVAILABLE` with `ESTIMATED_SALE_PRICE_MISSING` (Soft) and the Flip analysis is `NOT_EVALUATED` with `SALE_PRICE_MISSING` (Info) — the run itself goes ahead |
| `monthly_rent` | Optional. The **market rent** (labelled Monthly Rent): the Rental analysis sets it against the required rent and the Take-Back solves the sale price at it; without it those lines report `NOT_EVALUATED` under `MONTHLY_RENT_MISSING` (Info) rather than running on a zero, and everything that does not read it still computes (§8.5, §8.6) |
| `as_is_value` | Optional (v0.19). The team's as-is valuation, typed on the Underwriting Assumptions panel as **As-Is Value**; nothing in the screen reads it. The Take-Back sets its haircut price beside it when there is one (§8.6) |
| `rental_exit_value` | Default: the estimated sale price in force (v0.19). What the takeout lends against (§8.5), typed on the panel as **Exit Value**. Nothing is written for the default - the column is `NULL` and the sale price is read where it is needed, so a sale price edited later moves it - and a typed value equal to the sale price is stored as `NULL` again; the box carries the "default" tag while it holds the sale price and Reset all puts it back. Without a sale price or a typed value the Rental analysis is `NOT_EVALUATED` |

The as-is value is gone from the engine, from the form and from `deals` (migration `0011`).
With one value ratio there is nothing left for it to feed.

**No analysis toggles** (v0.17). The Flip, Rental and Take-Back analyses all run on every
deal. One whose input is missing is `NOT_EVALUATED` - the flip without a sale price, the rental
without an exit value, the take-back without a projected sale price or foreclosure months for its
state, and the market-rent lines of both without a rent - and reports everything that does not
depend on it. The two
toggles that used to turn the Flip and the Rental on and off (`flip_analysis`,
`rental_analysis`, Default / On / Off on the panel) are gone from the form, the deal and the
engine (migration `0021`); a run stored before v0.17 still says what it ran under.

Also from intake and enrichment, unchanged: the verified `credit_score` and deal count, and
`court_records` — the §7.2 court and filing tests are re-run here on the source in force at
underwrite time, adapter over team (§6.1). They are not copied from the `screens` row: weeks
can pass between the two stages and a pull that has since landed supersedes the hand search
Stage 1 ran on.

**The team form** asks for the Overview, the Property Overview, and the Deal Economics in
this order: Purchase Price, Rehab Costs, Loan Amount, the **Loan Structure** radio ("100% at
close" / "Holdback"), Loan Purpose, Closing Date, Term (months). Holdback reveals one more box,
**Rehab portion**, under the radio: a stylesheet rule on the checked radio, no script - a
browser without `:has()` shows the box always, and the server ignores it unless the deal is a
holdback. Labels only, each marked Required or Optional in plain text. Everything else above -
the five defaulted economics, the valuation, the rent, the court search - populates from the
defaults when the deal is stored and is edited on the deal page, and Edit Intake leaves it
where it is. Edit Intake does write the split: its Rehab portion box shows a typed portion
(blank while the split is the default), and a blank box or 100% at close clears the split for
the default to stand in.

### 8.2 Sizing

Unchanged from §7.4 in structure, on verified values, with one changed denominator:

```
rehab_adj  = rehab_costs × (1 + contingency_pct)
total_cost = purchase_price + rehab_adj + closing_costs_usd        # was + 3% of the price
LTC        = commitment / total_cost
LTV        = commitment / estimated_sale_price      # NOT_AVAILABLE with no price (§7.4)
```

A holdback carries an explicit `loan_purchase_portion` ("Advance at Closing") and `loan_rehab_portion` ("Rehab Portion"), adding up to the Loan Amount (§8.1). Left blank, the deal takes the **default split** - `rehab_portion = min(rehab_adj, loan_requested)`, `purchase_portion = loan_requested − rehab_portion` - populated at intake and tagged as a default (§8.1); a typed split is the team's.

- `ACQUISITION` (100% at close): `commitment = loan_requested`; no split
- `ACQUISITION_REHAB` (Holdback): `commitment = loan_requested`; `holdback = min(rehab_adj, loan_rehab_portion)`; `funded_at_close = commitment − holdback`
- A holdback with **no split entered** carries the default split above, and both stages size on it (`services/defaults.py`). A deal stored before the default existed and not yet opened is sized on the same formula at run time

Both structures lend the request in full. Whenever the `rehab_adj` cap bites, `REHAB_PORTION_EXCEEDS_BUDGET` (Info) names the portion entered and the budget it was capped at; there is one note, so the money above the cap is advanced at close instead of held back and only the timing moves. (`COMMITMENT_BELOW_REQUEST` reported the retired `SPLIT_PRINCIPAL`'s capped Tranche A lowering the commitment; nothing raises it now.)

Output: pass/fail on each cap, with the cap and the actual.

### 8.3 Return Overview — the lender's ledger and its IRR

The lender's dated monthly cash flows from `closing_date`, and their XIRR. Signs are from the lender's side: money out is negative, money in is positive.

```
rehab_months   = max(0, term_months − listing_months)              # whole periods only
draw_per_month = rehab_portion / rehab_months                      # rehab_months > 0
month m date   = anchor(m), the last day of the closing month + m, m = 0 .. term_months
stub row       = month term_months + 1, dated payoff_date          # when term_stub_days > 0
```

Month 0 is dated the end of the closing month, not the closing date: a deal closing on the
15th and one closing on the 1st lay out the same ledger. The closing date itself is reported
beside it for the record.

**The stub.** A term whose payoff date falls between two anchors gets one more row after the
last one, dated the payoff date itself. It is a period like any other, only shorter: it accrues
interest on the balance standing at its start, prorated by its days, and it carries the payoff
and the fee's payoff half.

```
stub interest = balance × interest_rate / 12 × term_stub_days / interest.day_count_basis
```

`interest.day_count_basis` is config, placeholder 30 — the 30/360 convention, where an 11-day
stub accrues eleven thirtieths of a month. **The draw schedule is whole periods only**: a stub
is days at the end of the term, after the listing period in every case, so there is no draw to
schedule in it and `rehab_months` does not see it.

**Month 0 — closing.**

```
funding  = −funded_at_close        # the advance at closing on a holdback;
                                   # the whole commitment at 100% at close
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

balance(m)  = commitment                                        # both structures
```

A holdback pays on the full commitment from close even though the holdback has not gone out yet — that is the structure (§3). The as-drawn balance of the retired `SPLIT_PRINCIPAL` went with it (engine `2.0.0`).

**The last row — payoff.** Month `term_months` on a whole-month term, the stub row on any
other; either way it is dated the payoff date — the month end the term lands on, or the actual
date an eventual actual-payoff entry names.

```
payoff = +outstanding principal      # funded_at_close + every draw = commitment
fees  += +commitment × origination_fee_pct / 2        # the payoff half
```

**Outputs.** The ledger as a table — `date`, `month`, `stub_days`, `funding`, `draws`, `interest`, `fees`, `payoff`, `net` — plus:

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

Runs on every deal (§8.1). It sells at the `estimated_sale_price`, and without one it is `NOT_EVALUATED` — the whole cost stack and the borrower's equity are still reported, the figures the sale price feeds are `null`, and `SALE_PRICE_MISSING` (Info, fixed in code, §8.8) says so. The run itself goes ahead: the ledger, the economics and the Take-Back analysis do not read a sale price, and refusing the whole underwrite for a figure three quarters of it does not need was a gate on the wrong thing.

The profit, line by line, in the order every output prints it (v0.17):

```
sale price          = estimated_sale_price
− broker            = estimated_sale_price × broker_selling_pct    # the deal's own, else config 6%
− purchase          = purchase_price
− closing           = closing_costs_usd
− rehab             = rehab_costs
− contingency       = rehab_costs × contingency_pct
− holding           = holding_costs_total
− interest          = total_interest                                # §8.3
− origination       = both origination halves                       # §8.3
= net_profit
```

and the cost stack, the same lines but the sale price and the broker, to the **total deal
cost**:

```
total_costs     = purchase + closing + rehab + contingency + holding + interest + origination
financing_costs = interest + origination
```

Then the borrower's side:

```
borrower_equity = total_costs − loan amount (the commitment)        # no sale price needed
cash_on_cash    = net_profit / borrower_equity     (null when the equity is zero or less)
project_yield   = net_profit / total_costs
```

`project_yield` is a **project margin, not an annualized return**, and is labelled "Project
yield" everywhere it is shown so nobody reads it as an IRR (it was `profit_yield`, "Yield
(Profit / Costs)", before v0.17; a stored one still reads). The broker's cut is subtracted from
the sale price and is deliberately not in the total deal cost: it is a cost of selling, not a
cost of the project. The cash-on-cash is held to **`pricing.borrower_coc_target`** (placeholder
20%), printed beside it as its target, and `BORROWER_COC_BELOW_TARGET` (severity from config,
placeholder Soft, §8.8) is raised when it falls short. It is not tested without a sale price
(there is no profit) nor when the loan covers every cost (there is no equity to earn on).
`SALE_PRICE_MISSING` reports what was not computed, not something that fell short.

### 8.5 Rental analysis

Runs on every deal (§8.1); the takeout, sized from the exit value down (v0.19, engine `2.1.0`).

```
exit value                  = rental_exit_value, else estimated_sale_price     # §8.1
takeout loan                = exit value × rental.takeout_ltv                   # config 75%
debt service, amortizing    = level monthly payment on the takeout loan
                              at rental_takeout_rate (the deal's own, else config 6.5%)
                              over rental.amortization_years (30)
debt service, interest-only = takeout loan × takeout rate / 12
required NOI                = amortizing debt service × rental.target_dscr      # config 1.20
required rent               = required NOI / (1 − rental_expenses_pct_of_rent)  # deal's own, else 35%

market NOI                  = market rent × (1 − expenses)
gap                         = market rent − required rent
DSCR at market, amortizing  = market NOI / amortizing debt service
DSCR at market, IO          = market NOI / interest-only debt service
our payoff due              = commitment + the origination fee's payoff half    # §8.3
spread to loan              = takeout loan − our payoff due
```

The expense ratio and the takeout rate are §8.1 analysis assumptions (the deal's own where the
team typed one, config otherwise); the LTV, the amortization and the target DSCR are config
alone. **Holding costs are not in this analysis** (they were, in the DSCR test up to v0.18): a
takeout lender underwrites the rent against its own debt service, and the carry over our term is
the flip's cost. The result says where the exit value came from (the sale price, or entered).

Flags: `MARKET_RENT_BELOW_REQUIRED` (severity config, placeholder Soft) when the market rent is
below the required rent; `TAKEOUT_BELOW_PAYOFF` (config, placeholder Soft) when the spread is
negative - a refinance would not take us out.

Without an **exit value** - no estimated sale price and none typed - the analysis is
`NOT_EVALUATED`: nothing above can be sized, the five assumptions are still reported, and so is
the market rent when there is one. Without a **market rent** the takeout, the required rent, the
payoff due and the spread still compute - none of them reads the rent - and only the market-rent
lines (`market_rent_status`: the market NOI, the gap, the two DSCRs, the rent test) are
`NOT_EVALUATED`, with `MONTHLY_RENT_MISSING` (Info, fixed in code) saying so. Nothing stands in
for what a property lets for, so a missing rent is never a rent of zero.

### 8.6 Take-Back analysis

Runs on every deal (§8.1); the lender's own downside, solved on its XIRR (v0.19, engine
`2.1.0`, `engine/calc/take_back.py`). The question is what the property has to **sell** or
**let** for, after a default, for the lender to still earn `pricing.target_lender_irr` (20%)
over the whole loan. Every figure is bisected on one dated timeline laid out from the ledger's
own month-end anchors (§8.3):

```
D = default month = max(1, term_months − draws.listing_months)
L = take_back_lost_interest_months      (the deal's own, else config 3)
F = take_back.foreclosure_months[state] (config, by state; placeholders OK 6, CO 4)
S = REO sale month = D + L + F

month 0             −funded at close, +the origination fee's close half
months 1..rehab     −the draws, as the ledger schedules them (they finish by D)
months 1..D         +interest on the commitment at the deal's own rate
months D+1..D+L     nothing: the lost interest
month D+L           −take_back_legal_costs_usd (the deal's own, else config 5,000)
months D+L+1..S     −the deal's monthly holding cost, one per foreclosure month
month S             +P × (1 − broker_selling_pct)                       # the REO sale
```

The payoff half of the origination fee is never collected, and the stub days of a term that
has them are not a period the lender gets to. The timeline and its costs - the default month,
the interest received through it, the lost interest in dollars, the legal bill, the foreclosure
carry, the sale month - are reported on every run.

**Block A - haircut to target.** The sale price `P` at which the XIRR of the flows above equals
the target, bisected over whole dollars in **[0, 3 × projected sale price]**: the lowest dollar
that reaches it. Reported as `P`, the **haircut** from the projected sale price in dollars and as
a share of it, `P` as a share of the **purchase price**, and `P` against the **as-is value** when
the team gave one (§8.1). Flag `TAKE_BACK_PRICE_ABOVE_PURCHASE` (severity config, placeholder
Hard) when `P` is above the purchase price: the lender needs more than the house cost to come
out whole. The lender's flows at `P` are recorded with the result, so the workbook can XIRR them
itself.

**Block B - hold and rent.** For each `take_back.hold_months` entry `H` (12, 24, 36): after the
REO sale month the lender instead collects **rent × (1 − expenses)** in months `S+1..S+H` and
sells net of the broker at month `S+H`. Two solves per row, each for the lender's XIRR to reach
the target: (1) the **required monthly rent at the projected sale price**, bisected over
**[0, 10 × market rent]**, or **[0, $20,000]** when there is no market rent; (2) the **required
sale price at the market rent**, over the same price bounds as block A, `NOT_EVALUATED` without
a market rent.

**Every solve carries its bounds, and "no solution" is an answer**: when the upper bound does
not reach the target, or the lower bound already does (a year's hold at the full projected
price needs no rent at all), nothing inside the bounds is the answer and the solve says which
bound it hit rather than reporting the bound as a price. The lender's XIRR only rises with the
sale price and with the rent, which is what makes the bisection valid. The answer is defined on
the XIRR itself (`engine/calc/irr.py`, the function the Return Overview is solved with); the
engine's probes ask the same question more cheaply - the sign of the NPV at the target rate, its
discount factors worked out once per solve - and the dollar it lands on and the one below it are
then confirmed on the XIRR, with a fall-back to bisection on the XIRR alone should they disagree.

The analysis is `NOT_EVALUATED` without a **projected sale price** (the haircut and the bounds
are set against it; `SALE_PRICE_MISSING` says so) or without **foreclosure months for the deal's
state** (`FORECLOSURE_MONTHS_MISSING`, Info, fixed in code); the timeline is reported either
way. Without a market rent only column (2) stands down, under the shared `MONTHLY_RENT_MISSING`.

Every output of both analyses was checked against an independent script - its own float XIRR,
its own month ends, its own whole-dollar bisection, no code shared with the engine - on every
underwrite fixture before the figures were recorded (`tests/test_fixtures.py`,
`tests/test_take_back.py`): Wichita needs $198,132 (a 23.8% haircut from $260,000, 99.1% of the
purchase price) and Denver $241,852 (26.7% from $330,000, 120.9% of the purchase price, $11,852
over its $230,000 as-is value); Wichita's required rent is $2,275.44 against a $2,000 market
rent, Denver's $2,888.06 against $2,800.

### 8.7 Underwrite result

```
UnderwriteResult
  engine_version, config_hash
  loan_purpose?, closing_date, payoff_date, term_months, term_stub_days,
    term_months_decimal, rehab_months
  analyses?                     # a run before v0.17 only: the two toggles it ran under
  sizing: {LTC, LTV, caps, pass/fail each, commitment, split}   # §8.2
  economics: {interest_rate, contingency_pct, rehab_adj, closing_costs,
              holding_costs_pct_of_cost, holding_costs_basis, holding_costs_total,
              holding_costs_monthly, origination_fee_pct,
              origination_at_close, origination_at_payoff, commitment, funded_at_close}
  return_overview: {ledger: [{month, date, stub_days, funding, draws, interest,
                             fees, payoff, net}],
                    total_funding, total_draws, total_interest, total_fees,
                    total_payoff, total_profit, irr}           # §8.3
  sensitivity: {loan_requested, interest_rate, rates: [...],
                rows: [{loan_amount, reduction, loan_purchase_portion,
                        loan_rehab_portion, cells: [{interest_rate, irr, is_deal}]}]}  # §8.9
  borrower_sensitivity: {status, loan_requested, interest_rate, rates: [...],
                         rows: [{loan_amount, reduction,
                                 cells: [{interest_rate, net_profit, total_costs,
                                          project_yield, cash_invested,
                                          cash_on_cash, is_deal}]}]}           # §8.9
  pricing: {target_lender_irr, borrower_status, rate_precision, rate_ceiling,
            rows: [{step, loan_amount, interest_rate, lender_irr, borrower_profit,
                    borrower_cash_invested, borrower_cash_on_cash, sentence,
                    borrower_equity, equity_added, derisk}]}             # §8.10
            # a 1.7.0 matrix carries rates: [...] (the grid it searched) and no solve fields
  sources_and_uses: {advance_at_closing, rehab_holdback, borrower_equity, total_sources,
                     purchase_price, closing_costs, rehab_costs, contingency,
                     holding_costs, interest, origination, total_uses}    # §8.11
  flip: {status, estimated_sale_price, broker_costs, purchase_price, closing_costs,
         rehab_costs, contingency, holding_costs_total, interest, origination,
         financing_costs, total_costs, net_profit, project_yield, loan_amount,
         borrower_equity, cash_on_cash, cash_on_cash_target, cash_on_cash_passed}  # §8.4
  rental: {status, market_rent_status, expenses_pct, takeout_ltv, takeout_rate,
           amortization_years, target_dscr, exit_value, exit_value_source, takeout_loan,
           debt_service_amortizing, debt_service_interest_only, required_noi, required_rent,
           payoff_due, spread_to_loan, spread_passed, market_rent, market_noi, rent_gap,
           dscr_amortizing, dscr_interest_only, rent_passed}                 # §8.5
  take_back: {status, market_rent_status, loan_amount, interest_rate, term_months,
              default_month, interest_received, lost_interest_months, lost_interest,
              legal_costs, state, foreclosure_months, holding_costs_monthly,
              foreclosure_carry, sale_month, broker_selling_pct, expenses_pct, target_irr,
              hold_months, projected_sale_price, purchase_price, as_is_value,
              haircut_price: {status, value, irr, lower, upper, bound_hit},
              haircut_amount, haircut_pct, price_pct_of_purchase, price_above_purchase,
              price_vs_as_is, price_pct_of_as_is, haircut_flows: [{month, date, amount}],
              holds: [{hold_months, sale_month, required_rent: {...}, required_sale_price: {...}}]}
                                                                              # §8.6
  flags: [ {code, severity, message} ]
```

A run before engine `2.1.0` carries the two DSCR tests it ran instead (`LegacyRentalAnalysis`,
`LegacyTakeBackAnalysis`: the net monthly income, the debt service, the DSCR and its floor);
it still loads, the deal page and the workbook say the version predates the analyses, and
Run Analysis replaces it.

All three analyses are always present, and `status` says what happened to each:

| `status` | Means |
|---|---|
| `EVALUATED` | it ran; every figure is there |
| `OFF` | a run before v0.17 only, when the Flip and Rental had toggles and one was off |
| `NOT_EVALUATED` | the input it runs on is missing - the estimated sale price (the flip, the take-back and, by default, the rental's exit value) or the foreclosure months for the deal's state. The flag that names it is `SALE_PRICE_MISSING` or `FORECLOSURE_MONTHS_MISSING` (§8.8). The Rental and the Take-Back carry a second status, `market_rent_status`, for the lines that read the market rent (`MONTHLY_RENT_MISSING`) |

An analysis that did not run still reports everything that does not depend on the missing
input: the Flip's whole cost stack and the borrower's equity, the Rental's assumptions and
market rent, the Take-Back's timeline and costs. What the input feeds is `null`, the tests
included - a rent nobody could compare has not fallen short.

### 8.8 Underwrite flags

The §7.2 court codes and the credit, experience and leverage codes the screen raises appear here too, on the underwrite's own inputs and with the same severities. The eight below are the underwrite's own: the rental's two codes, the take-back's price code and the borrower's cash-on-cash take their severity from config; the other four are fixed `Info` in code and config must not grade them. `DSCR_BELOW_FLOOR` and `TAKE_BACK_DSCR_BELOW_FLOOR` (v0.4 to v0.18) are retired: a stored row still names one, nothing raises them, and the loader refuses a yaml that grades them.

| Code | Severity | Raised when |
|---|---|---|
| `MARKET_RENT_BELOW_REQUIRED` | config (placeholder Soft) | §8.5, the market rent is below the rent the takeout requires; not tested without a market rent or an exit value |
| `TAKEOUT_BELOW_PAYOFF` | config (placeholder Soft) | §8.5, the takeout loan is short of our payoff due; not tested without an exit value |
| `TAKE_BACK_PRICE_ABOVE_PURCHASE` | config (placeholder Hard) | §8.6, the sale price the lender needs is above the purchase price; tested only when it was solved |
| `BORROWER_COC_BELOW_TARGET` | config (placeholder Soft) | §8.4, the Flip's cash-on-cash is below `pricing.borrower_coc_target`; not tested without a sale price or with no equity |
| `MONTHLY_RENT_MISSING` | Info (fixed) | §8.5, §8.6: no `monthly_rent` on the deal, so the market-rent lines of both analyses are not evaluated |
| `FORECLOSURE_MONTHS_MISSING` | Info (fixed) | §8.6: config names no foreclosure months for the deal's state, so the Take-Back is not evaluated |
| `SALE_PRICE_MISSING` | Info (fixed) | §8.4: there is no `estimated_sale_price`, so the Flip analysis is `NOT_EVALUATED`, and so are the Take-Back's solves and, unless an exit value was typed, the Rental's takeout; the screen's own `ESTIMATED_SALE_PRICE_MISSING` (Soft) reports the same absence on the leverage side |
| `NO_REHAB_PERIOD` | Info (fixed) | §8.3: a holdback whose term leaves `rehab_months = 0`, so the rehab portion is advanced at close instead of drawn |

### 8.9 Lender and Borrower Sensitivity

**Lender Sensitivity** is IRR by loan amount and annual rate, on the deal page under the
Return Overview and as the `Lender Sensitivity` sheet of the workbook. **Every cell is a full ledger re-run**: the deal is
re-sized in the same caps cell and the §8.3 ledger laid out again at that loan amount and
that rate, and the cell is its XIRR. Nothing is interpolated.

```
rows     the request on the bottom row, then sensitivity.loan_steps rows above it,
         each sensitivity.loan_step_usd less (placeholders: 4 x $5,000, so -$20,000 .. request)
columns  sensitivity.rate_min .. rate_max in rate_step steps (placeholders: 12.5% .. 17.5% by 1%),
         and nothing else
```

**A reduction comes off the advance at closing**; the rehab portion is what the work costs
and is unchanged until the advance reaches zero, after which it gives dollar for dollar.
A single-note product simply lends less. A row whose reduced amount is nothing at all, or
whose commitment sizes to nothing, is left out. The columns are the grid on every deal
(v0.15; v0.8 to v0.14 inserted the deal's own rate): when the deal's rate is one of them,
the deal's own cell - the request at its actual rate - is marked and reproduces the Return
Overview's IRR exactly; when it is not, no cell is marked. The default rate is the grid's
first column. The page shows each row's loan amount alone; the workbook keeps a "Less than
request" column beside it.

**Borrower Sensitivity** sits directly under it on the deal page and is the `Borrower
Sensitivity` sheet: the same rows, the same columns, the same rows left out, and in each cell
the borrower's side of that same re-sized, re-ledgered deal:

```
net_profit    = the Flip Analysis's net profit (§8.4) at that loan amount and rate
total_costs   = the flip's total deal cost there
project_yield = net_profit / total_costs                          # what the cell shows
cash_invested = total_costs - the loan amount (the borrower's equity)
cash_on_cash  = net_profit / cash_invested     (none when cash_invested <= 0)
```

Only the financing costs move from cell to cell; the rest of the cost stack and the sale do
not depend on the loan. The table is `NOT_EVALUATED`, with no rows, when there is no estimated
sale price. The page and the CLI show the profit and the **project yield** in each cell
(v0.17; the cash-on-cash before it, which is still recorded); the workbook adds the total deal
cost so a reader can divide one by the other.

### 8.10 Pricing matrix

One row per `pricing.loan_amount_pct_steps` entry (placeholder `[0, -0.05, -0.10]`, engine
`1.10.0`), each a share of the request that only ever lends less. The row's loan amount is
the request times `1 + step`, **rounded half-up to the nearest `pricing.loan_amount_rounding`**
(placeholder $1,000) and never above the request; the request row (step 0) is never rounded,
so a $195,000 request prices as $195,000, $185,000 (185,250 rounded down) and $176,000
(175,500 rounded up). Each row is labelled by its step - "Requested", "-5%", "-10%" - and
carries the dollar change the share came to; the reduction is taken off the advance at
closing as a sensitivity row's is. A step of -1 leaves no loan and the row says so. Before
`1.10.0` the steps were dollar changes (`pricing.loan_amount_steps`, `[0, -10000, -20000]`);
the loader refuses that key by name. For each, the row is priced at the **lowest rate, to 0.01%,
whose lender XIRR reaches `pricing.target_lender_irr`** (placeholder 20%), **solved on the
ledger** (engine `1.8.0`): every candidate rate is a full ledger re-run at that loan amount,
exactly as a Lender Sensitivity cell is, and the search bisects on whole 0.01% steps, which is
valid because the lender's IRR only rises with the rate - the advance, the draws, the fee and
the payoff do not depend on it. The rate reported is that step: two decimals of a percent,
**rounded up, never down**, because a rate rounded down to the nearest 0.01% no longer
clears the target it was solved for. Beside it, the lender IRR at it and the borrower's
profit and cash-on-cash at it (§8.9).

The Lender Sensitivity's columns only set the starting bracket - the highest that falls
short below, the lowest that clears above - and stay what the two sensitivity tables show. A
row they never clear is searched up to 100%, the most the model accepts as a rate; one that
does not clear even there says so ("No rate up to 100% gets ..."). A step that leaves no loan
says it cannot be priced; the borrower's figures are empty whenever the Borrower Sensitivity
is not evaluated.

**A rate above `pricing.max_quoted_rate`** (placeholder 18%, engine `1.9.0`) **is not a
quote.** The solve still finds it; the row carries no rate, no IRR and no borrower figures,
records the solved rate as `rate_needed`, and says "exceeds max quoted rate (X% needed)" -
on the deal page in place of the figures, in the workbook's sentence and its `Rate needed`
column. A rate equal to the maximum is quoted. The matrix records the maximum it ran with.

Each row carries one plain-English sentence, written by the engine so every output words it
the same:

```
A $140,000 loan at 14.37% returns $21,400 profit (28.2% cash-on-cash).
A $150,000 loan exceeds max quoted rate (20.15% needed).
```

**The de-risk line** (v0.17; X redefined in v0.18). Each row also carries the borrower's
equity at its solved rate - the Flip Analysis's total deal cost less the loan amount (§8.4),
which needs no sale price - and every row but the request says what lending less does to the
rate:

```
equity_added = the requested loan amount - the row's loan amount          # the plain reduction

Adding $19,000 of equity brings the rate to 13.63%.
Adding $10,000 of equity brings the rate to 13.72%, above the 13.7% max quoted rate.
```

X is the cash the borrower brings at closing in place of the loan (engine `2.0.1`; `2.0.0`
recorded the difference in borrower equity, which nets off the interest and fee the smaller
loan saves - $16,399.85 rather than $19,000 on Denver). The rate is the row's solved rate - its
quote, or the rate it needs when that is above the maximum, which the line then says. A row
with no solved rate (no loan, or no rate up to the ceiling) has no line, and neither has the
request. **The rows run most equity first** on the deal page, in the workbook and in the CLI:
the smallest loan at the top, the request at the bottom. The thank-you page reads them the
other way round (§4.2).

The solve is checked against an independent one on two fixtures - Wichita's 100% at close note
and Denver's holdback draws - each a float secant root-find on the continuous rate,
rounded up to the next 0.01% and confirmed a step either side (`tests/test_pricing.py`):
Wichita $135,000 / $143,000 / $150,000 all at 16.34%, Denver $176,000 / $185,000 / $195,000
at 13.63% / 13.72% / 13.82%, with the equity 94,759 / 88,226.20 / 82,510 and 102,451.60 /
94,676.50 / 86,051.75 worked out by the same script; the lines add $15,000 / $7,000 and
$19,000 / $10,000.

### 8.11 Sources & Uses

Where the deal's money comes from and where it goes (v0.17, `engine/calc/sources_uses.py`), on
the deal page between the three analyses and the Return Overview, as the `Sources & Uses`
sheet of the workbook and as a block of the CLI report:

```
Sources                                    Uses
advance at closing   the ledger's month 0  purchase      purchase_price
rehab holdback       the ledger's draws    closing       closing_costs_usd
borrower equity      total deal cost       rehab         rehab_costs
                     - loan amount         contingency   rehab_costs × contingency_pct
                                           holding       holding_costs_total
                                           interest      total_interest (§8.3)
                                           origination   both origination halves
total sources                              total uses  = the total deal cost (§8.4)
```

The two advances add up to the loan amount, so the two totals are the same number: the
equity is defined as the difference. `tests/test_fixtures.py` holds every fixture to the
identity to the cent, each line against the independent script's figure, `tests/test_calc.py`
a handful of odd-cents deals, and the workbook's last row is Excel's own subtraction. The
holdback is what the ledger draws: zero at 100% at close, and zero on a holdback whose term
leaves no rehab period, where the rehab money goes out at closing (§8.3). Nothing in the table
reads a sale price, so it is there on every run.

The Pricing section sits under the Borrower Sensitivity on the deal page and is the `Pricing`
sheet of the workbook.

---

## 9. Outputs

One order wherever a run is printed — the CLI report and the workbook:

**Overview · Property Overview · Deal Economics · Return Overview · Lender Sensitivity · Borrower Sensitivity · Pricing · Flip Analysis · Rental Analysis · Take-Back Analysis · Sources & Uses · Flags**

The first three are the §8.1 input groups, shown back as the run read them; the rest are the
result. The CLI report and the workbook show all of it. The deal page shows every section but
the Flags - the flags, the screen's reasons and the suggested reply stay on the stored rows,
the CLI and the workbook - and puts the outputs first. Its header is the address, one row of
chips - the borrower, the entity (when there is one), "Loan Request: $X" in whole dollars and
the IRR to two decimals - and the phone under them, with the controls on the right: the version switcher,
the one **Run Analysis** button with its optional name, Download Excel and Edit Intake. The
loan structure and the term are in the intake summary below; the status and the verdict are one
line of small text atop the Actions card ("Status: Underwriting · Verdict: Go", the version
in view's verdict, a dash before any), above Progress / Pause / Kill (v0.16); when a version
ran, who ran it and on which engine are in the History. Then Pricing, full width, with each
row's borrower equity and, under its sentence, its de-risk line (§8.10); the Lender and
Borrower Sensitivity side by side; Flip, Rental and Take-Back as three cards, each led by its
headline figure (net profit, required rent, sale price needed; "Not evaluated" is a state, not
an error) - the Flip card prints the profit line by line, the cost stack to the total deal cost,
and the borrower's equity, cash-on-cash against its target and project yield (§8.4); the Rental
card the takeout from the exit value down to the required rent, the market rent with its gap and
two DSCRs, and the spread to loan (§8.5); the Take-Back card the timeline, the haircut block and
the hold-and-rent table, a solve with no solution reading "None needed" or "No solution" (§8.6);
10b moves the three into the Scenarios tab; then
**Sources & Uses**, the sources beside the uses (§8.11); and the Return Overview as one line
(IRR, total interest, total fees, total profit, payoff date) with the ledger collapsed under
it as **Cash Flows**. Under the outputs, open, sits the **Underwriting Assumptions** panel:
two columns of editable boxes - Interest Rate, Origination Fee, Contingency, Closing Costs,
Holding Costs %, the Loan Structure radio and the Rehab portion box it reveals on Holdback
(the team form's own two controls, v0.18; the advance at closing is the rest of the loan),
Estimated Sale Price, As-Is Value, Exit Value (tagged "default" while it holds the sale price,
v0.19), Monthly Rent, Broker Selling Costs %, Rental Expense %, Rental Takeout Rate, Take-Back
Legal Costs, Take-Back Lost Interest Months (the two analysis toggles that ended the list went
with v0.17) -
each showing what the deal carries, masked as everywhere else, tagged "default" while it
holds its default and with a reset of its own; one **Save & Run** at the bottom that saves
the panel and runs the analysis as the person who pressed it, and **Reset all to defaults**
beside it, which confirms on a page of its own, puts every box with a default back to it and
runs again. Under the panel the intake is a read-only summary - **Deal Economics and
Property**: Overview, Property Overview and Deal Economics side by side with an Edit Intake
link, the sizing table against the caps and the score components - and below it the rest:
**Actions**, the **court and filing search** (collapsed; the outcome, the date and the typed matters, its own Save), and
**History** (collapsed;
every version with its name, timestamp, actor, Working or Draft and engine version, a View on
each; every intake edit no version has run on as an "edit" row with a Restore; and the audit
trail under it). The page names no engine stage; the two it runs are an implementation.

**Versions on the page** (§5). Every result on the page - the verdict line, the score
components, and every output section - is one version's: the
**Working** one unless `?version=<number>` asks for another. The switcher names the
Working version ("v3 · Working") and the header carries a **Draft** chip whenever the page
is showing a different one. In the header, on the right, sits the **version switcher**: a dropdown of every version (name, Working or
Draft, when, who) with a Show button - a plain GET form, no script - and, for the version
shown, **Mark as Working** (when it is not already; audited `VERSION_MARKED_WORKING`) and
**Restore inputs**. Restore inputs loads the version's snapshot back into the deal - its
intake payload applied as a new submission (`INTAKE_RESTORED`), then every snapshotted column
put back and the deal re-pointed at the borrower and property it had (`VERSION_RESTORED`) -
and runs a new version on it as the person who pressed it; it is refused where an edit is
refused (§4.6) and on a backfilled version, which kept no inputs. Run Analysis and Save &
Run each take an optional **Version name** (at most 100 characters); a person's unnamed run
is `v<number>` and an automatic run `Auto v<number>`, but for the arrival version, which is
named for the deal (§5). A run lands on the version it recorded,
so the person sees what they ran - Working or a draft by the §5 rule - and the notice ends
"Saved as v4 (draft)." A shown version is read-only: the panel above it is the deal's current inputs, which the next run
reads.

**Download Excel** sits in the switcher's row, after Show, and exports the version in view -
not the dropdown's selection - as the §9.6 workbook (`GET
/queue/deals/{id}/versions/{n}/workbook`), named `<address>-<version>.xlsx` with the address
and the version's name each reduced to letters, digits and dashes
(`1412-S-Trenton-Ave-Tulsa-OK-74120-v3.xlsx`; `deal-<first 8 of the id>` with no address). A
version whose ledger did not run has no workbook: the button is off, and the route goes back
to the version saying so.

**The deal page is labels and values only.** No explanatory sub-line, parenthetical, inline
definition or (?) hover beside a value or a label (v0.15 took the last (?) off). Every IRR
on it is two decimals of a percent. A value that is a default carries the "default" tag
(§8.1). A web deal's provenance reads "Source: <slug>" and "How
they heard of us: <text>" (§4.2).

### 9.1 Screen summary
One page in the review queue: intake facts, enrichment hits, score components, verdict and
missing fields on the deal page; the reasons and the suggested reply on the stored `screens`
row, the CLI report and the workbook, where a person drafting a message reads them.

**Restore** on an edit row saves that intake as a new `intake_submissions` row - read back
through the parser for the channel it arrived on, applied exactly as Edit Intake applies a
form, audited as `INTAKE_RESTORED` naming the submission it came from - and runs the analysis
again. Nothing is rewound: the intake restored and the one it replaced are both still on the
deal afterwards. Refused where an edit is refused (§4.6). A version's **Restore inputs** is
the same thing for the whole snapshot (§9).

**A rejected form is a page, not an error.** Every `ValueError` and `ValidationError` raised
while handling the team-entry form, the deal page's Underwriting Assumptions panel and court
search section, Edit Intake or an
underwrite request re-renders the same form with what was typed still in it, and each complaint
sits **under the box it is about**; only the ones about more than one box — the term against
the payoff date against the closing date — go to the top of the page, because there is no
single box they belong under. A deal whose values are all present and which the engine will not
run on (a commitment of zero, §8.2) says so the same way rather than as a server error, and so
does an action on a deal id that no longer exists.

**Every enum a person reads is title case with spaces**, on the one convention `schema/labels.py`
sets for every code a person reads (the two loan structures excepted, which read as the team
form's radio names them): the court search's outcome, its matter codes and lien kinds, and a
flag's severity. The stored value is untouched — an `<option>` still posts `BANKRUPTCY_IN_LOOKBACK`
and a flag tag is still styled by its code — only the words change.

### 9.2 Readiness checklist
One row per §8.1 input - the five analysis assumptions included - with the value in force, where it came from (`ADAPTER` / `TEAM` / `BORROWER` / `DEFAULT` / `MISSING`), whether the run needs it and what the run does with or without it (`services/readiness.py`). The deal page no longer prints it as a section (v0.15): the Underwriting Assumptions panel is the one list of what the ledger runs on, and the banner over the outputs names what is missing while the ledger is off. The required set is four things and no more: `interest_rate`, `closing_date`, the term (`term_months`, shown as months and, where there is one, the stub days after them), and the loan split on a holdback. **A `DEFAULT` counts as present**: the rate, the closing date and the split always have one (§8.1, §8.2), so the one that can turn the ledger off is the term; readiness never blocks on the closing date. `monthly_rent` is listed as optional, noted "without it the market-rent lines of the Rental and Take-Back analyses are not evaluated"; `estimated_sale_price` is optional too, noted for the LTV and the flip that go without it (§7.4, §8.4); `rental_exit_value` reads `DEFAULT` with the sale price while nobody has typed one, and `as_is_value` is optional (v0.19). It is derived from the same rules `services/assemble.py` refuses a run on, so the disabled button and the refusal behind it cannot name different things.

### 9.3 Credit memo
Generated from `UnderwriteResult` into GLENWOOD's template (to be supplied; docx). Sections: borrower, property, deal structure, sizing vs caps, the return overview (ledger and IRR), flip, rental, take-back, flags with pass/fail, recommendation. Every flag shows the threshold it was tested against.

### 9.4 LOI
docx merge from GLENWOOD's LOI template (to be supplied). Fields: borrower/entity, guarantor, property, loan purpose, loan structure, commitment (and the split on a holdback), interest rate, closing date, term and payoff date, origination split, conditions from flags. Generated only on team action.

### 9.5 Mortgage Automator handoff
On "LOI accepted": create borrower (if not matched), property, and loan in MA via API with the structured data; attach memo and LOI; record `ma_sync` row. MA is not written to before this point.

### 9.6 Verification tools (internal, not client deliverables)

`uv run glenwood run <fixture.json> [--underwrite]` prints a run — verdict, reasons, sizing
against caps, then the §9 sections in order: the deal economics, the full monthly ledger and
its IRR, the Lender and Borrower Sensitivity, the pricing rows with their de-risk lines, the
flip line by line, the rental, the take-back, Sources & Uses, and the flags.

`uv run glenwood export <fixture.json> <out.xlsx>` writes the same run as a workbook, sheets
`Inputs`, `Return Overview`, `Lender Sensitivity`, `Borrower Sensitivity`, `Pricing`, `Flip`,
`Rental`, `Take-Back`, `Sources & Uses`, `Flags`. Every figure is a number
under a currency, percent or date format — never preformatted text — so the cells add up and
compare. The `Return Overview` sheet carries the ledger with its dates and its `net` column
and an **`XIRR` formula over them**, so the workbook recomputes the IRR itself and a reader
can see the engine's number and Excel's agree rather than taking the engine's word for it. The
`Take-Back` sheet does the same for the haircut (v0.19): the lender's flows at the sale price
needed, dated, with an `XIRR` formula over them beside the engine's, which is the target.

Both read a fixture off disk and call the pure engine: no database, no network. They exist
so the math can be checked by hand against a spreadsheet; neither is shown to a borrower.

`uv run glenwood deals run <deal-id> [--version V]` and `uv run glenwood deals export
<deal-id> <out.xlsx> [--version V]` are the same report and the same workbook for one
version of a **stored** deal (§5): the Working version unless `--version` names another by its
number or its exact name (a name two versions share is refused with both numbers). Nothing
is run again - the version's screen and underwrite are rebuilt off their rows - so the report
is what was recorded, engine version and all; a version whose ledger did not run has no
workbook to export.

---

## 10. Configuration

`config/glenwood.yaml` — everything a GLENWOOD person might want to change lives here, nothing in code. The loader rejects a missing key, an **unknown key**, or an out-of-range value at startup, so a key this list does not name is a key the service will not start with.

- credit floor tranche; tranche cutoffs
- leverage caps: loan structure (`ACQUISITION`, `ACQUISITION_REHAB`) × tranche × experience tier (placeholder grid); a grid still keyed by a retired product (`NO_DRAW`, `SPLIT_DRAW`, `SPLIT_PRINCIPAL`, `WHOLETAIL`) is refused by name. Each cell is `ltc` and `ltv` — two caps, since LTARV and the as-is LTV are gone (§7.4). The LTC placeholder is **100%**: no cap at all until GLENWOOD fills it
- screen tolerance band; court and filing flag severities, lookbacks, thresholds
- experience: repeat-borrower minimum tier
- fees, each the **default for a §8.1 input** except the broker's:
  - `origination_default_pct` (2.0%, half at close and half at payoff)
  - `contingency_default_pct` (0.00)
  - `closing_costs_default_usd` (1,000) — the lender's closing costs, inside the LTC denominator (§7.4, §8.2)
  - `holding_costs_default_pct_of_cost` (2%) — the default for `holding_costs_pct_of_cost`, itself a percentage of `purchase_price + rehab_costs` over the whole hold (§8.1)
  - `broker_selling_pct` (6%, placeholder; 4% before v0.17) — the flip's cost of selling, the default for the deal's own `broker_selling_pct` (§8.1). Migration `0021` moved every deal still carrying the 4% stand-in to 6%
- `draws.listing_months` (3): `rehab_months = term_months − listing_months`, whole periods only
- `interest.default_annual_rate` (12.5%, placeholder; the sensitivity grid's first column): the **default for `interest_rate`** (§8.1), populated on every deal until the team enters its own
- `interest.day_count_basis` (30): the days a whole monthly period counts as, for the final stub period's prorated interest (§8.3)
- `closing.default_lead_days` (14): the **default for `closing_date`** (§8.1) is the last day of the month this many days after the deal came in
- sensitivity (§8.9): `loan_step_usd` (5,000), `loan_steps` (4), `rate_min` (12.5%), `rate_max` (17.5%), `rate_step` (1%) - the shape of the IRR grid; every cell is a ledger re-run
- pricing (§8.10): `borrower_coc_target` (20%, placeholder, v0.17; the Flip's cash-on-cash target, §8.4), `target_lender_irr` (20%, placeholder), `max_quoted_rate` (18%, placeholder; above 0, at most 100%), `loan_amount_pct_steps` (`[0, -0.05, -0.10]`, placeholder; each between -1 and 0, no duplicates, at least one) and `loan_amount_rounding` ($1,000, placeholder; above 0). The old `loan_amount_steps` is refused by name
- rental takeout (§8.5): `expenses_pct_of_rent` (35%) and `takeout_rate` (6.5%), the defaults for the two per-deal assumptions; `takeout_ltv` (75%, placeholder, v0.19; above 0), `amortization_years` (30) and `target_dscr` (1.20, placeholder; the renamed `dscr_floor`, which is refused by name)
- take-back (§8.6): `lost_interest_months` (3) and `legal_costs_usd` (5,000), the defaults for the two per-deal assumptions; `foreclosure_months` by state (placeholders OK 6, CO 4; every served state needs an entry and `OTHER` cannot have one) and `hold_months` (`[12, 24, 36]`; distinct, ascending, 1 to 120). The DSCR test's `amortization_years` and `dscr_floor` are refused by name
- underwrite flag severities: `MARKET_RENT_BELOW_REQUIRED` and `TAKEOUT_BELOW_PAYOFF` (placeholder Soft), `TAKE_BACK_PRICE_ABOVE_PURCHASE` (placeholder Hard), `BORROWER_COC_BELOW_TARGET` (placeholder Soft, v0.17). The informational codes (§8.8) are fixed `Info` in code and the loader refuses to grade them, as it refuses the retired `DSCR_BELOW_FLOOR` and `TAKE_BACK_DSCR_BELOW_FLOOR`
- states served and court-record adapter per state
- `web_intake.submissions_per_hour_per_ip` (placeholder 5): posts to the public form from one address in a sliding hour before the next is turned away (§4.2)
- `web_intake.thanks_token_hours` (placeholder 24): how long a submission's thank-you link, with the borrower's indicative terms on it, stays live (§4.2)

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

Every value is shown, read-only, on the **Defaults** page (`/defaults`, signed in, linked from
the top bar), grouped Economics, Flip, Rental, Take-Back, Pricing, Sensitivity, Screen caps and
thresholds, Dates, each beside its dotted key (`fees.origination_default_pct`). The leverage
caps show as one table per loan type. A test reads the yaml itself and fails on any key the
page does not show, so a new key has to be placed in a group (`api/defaults_page.py`).

---

## 11. Compliance

- **Credit:** no hard pull without a signed borrower authorization on file (`credit_authorization_signed`); intake uses self-reported range only. Credit data is stored encrypted at rest, access logged.
- **SMS:** replies only to inbound senders; sent by a human via LinkedPhone; no automated outbound. Opt-out honored.
- **Listing sites:** address extraction from URLs only; no page scraping.
- **Business-purpose lending:** intake and LOI language reflect business-purpose loans; no consumer-purpose features.
- **Court/lien data:** used for underwriting decisions on business-purpose loans; retained with source and timestamp.
- **Indicative terms (§4.2):** the one thing a borrower reads back is the thank-you page's Indicative terms table, on an unguessable link that expires, under the disclaimer that the figures are indicative only and not an offer, commitment or guarantee, and above the line that the loans are business-purpose only. No verdict, IRR or flag reaches it, and what was shown is on the deal and in the audit trail.
- **Access:** the review queue is behind a session cookie and nothing it serves is public but the borrower's own form at `/apply` (§4.2), which writes one new deal, and its thank-you page, which reads back the borrower's own indicative terms and nothing else. No
  self-signup and no reset-by-email — a user is created and deactivated from the command
  line, so the list of people who can read credit and court findings is maintained on purpose.
  A signed-in user changes their own password at `/account/password` (the current password
  and the new one twice, the same 12-character floor); a forgotten one is set anew by
  `glenwood users reset-password`. Both are audited and both end every other session on the
  account at once: a session cookie is bound to the password hash it was issued under.
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
runs without a sale price (§8.4), and the §8.1 form as it now stands. Phase 5b (engine `1.2.0`)
removed the whole-month requirement on the payoff date — the term is whole periods plus a stub
(§8.1, §8.3) — made holding costs a percentage of the price plus the rehab (§8.1), and made
every form refusal a page with the message beside the box (§9.1). The credit memo and LOI
generation Phase 5 also names wait on GLENWOOD's templates (§13).

Phase 6b (migration `0015`) made the queue run on its own: every deal populated with its §8.1
defaults at intake and tagged (`services/defaults.py`), the screen and the underwrite run on
arrival and after every team edit as `system` (`services/autorun.py`, §4.6), the deal page
reduced to labels and values with definitions behind a (?), and the public form's consent
folded into the line above its button. It also made the database pool ping a connection
before handing it out, which is what turned the first request after a quiet spell into a
500 on the deployed service.

Phase 4's review queue is built: sign-in and `users`, the queue list, the deal page, the team
actions, and the team-entry form. Its SMS ingestion and contract OCR wait on the LinkedPhone
recon (§13) — there is nothing to parse until the webhook shape is known, and a parser written
against a guess is a parser rewritten.

The public borrower form (§4.2, migration `0013`) was built ahead of the Phase 6 handoff: it
is the first borrower channel, it needs nothing the recon is waiting on, and a deal it stores
is a deal the queue already knows how to work. The URL-address parser it reads a listing link
with (`adapters/listing_url.py`) is the pure half of Phase 3's LINK adapter, built early for
the same reason.

Phase 7 (engine `1.4.0`, migration `0016`) is the Home page and the month-end ledger. The
queue list is **Home**: four sections — Borrower Submissions (channel `WEB`), DCF New Deals
(every other channel), On Pause (`PAUSED`), Dead (`DEAD` or `DECLINED`) — most recently touched
first within each, every row carrying the borrower, the address, the loan amount, the loan
type, the latest verdict and IRR, the source and the last activity, and the three controls
(§4.6): Progress, Pause, Kill. The pinned-banner section is gone with the status groups.
`PAUSED` joined the status enum. The team form is labels and boxes and asks only for the
Overview, the Property Overview and seven Deal Economics boxes (§8.1); the asset type and the
exit are gone with the §3 inference; the ledger anchors to month ends (§8.3) and the payoff
date is read-only. `glenwood deals purge --all --confirm` is the one-time cleanup (README).

Phase 7a (engine `1.5.0`, no migration) is the one-button deal page. **Run Analysis** runs
the screen and the underwrite together as the person who pressed it (§4.6); the page names
no stage, keeps the verdict line, and drops the Flags section, the Reasons and the Suggested
Reply (they stay on the stored rows, the CLI and the workbook). The hand-entered values are
one collapsed **Inputs** section; a collapsed **History** lists every intake version, screen
and underwrite with actor and engine and restores any version as a new submission (§9.1).
Home rows carry an Open button. The closing date is optional on every channel and defaults
to the month end `closing.default_lead_days` after arrival, tagged `DEFAULT` (§8.1). The
sensitivity table (§8.9) is new: IRR by loan amount and rate, each cell a full ledger
re-run, config-shaped, on the page and in the workbook.

Phase 7b (engine `1.5.1`) puts the deal's own rate on the sensitivity grid as a column, so its
cell is always present and marked; adds Change password for the signed-in user
(`/account/password`) and `glenwood users reset-password` for an admin, both audited, both
ending every other session on the account (§11).

Phase 7c (engine `1.6.0`, migration `0017`) is the Underwriting Assumptions panel (§8.1, §9):
the collapsed Inputs section replaced by an open two-column panel under Deal Economics, one
Save & Run, one Reset all to defaults behind a confirmation page, the court search in its own
collapsed section. Five analysis assumptions that only config carried - the broker's selling
percentage, the rental's expense ratio and takeout rate, the take-back's legal costs and
lost-interest months - become per-deal columns with the config value as their default
(§8.4-§8.6), and the engine reads the deal's when present. Three audit codes are new:
`ASSUMPTIONS_SAVED`, `ASSUMPTIONS_RESET`, `COURT_SEARCH_SAVED`.

Phase 8e (engine `1.10.0`, migration `0020`) steps the pricing matrix by percent -
`pricing.loan_amount_pct_steps` and `pricing.loan_amount_rounding` replace the dollar steps,
each row labelled "Requested", "-5%", "-10%" (§8.10) - and sends a web submission to a
thank-you page of its own, `/apply/thanks/<token>`, that shows the borrower their indicative
terms under the required disclaimer when the arrival run said Go or Conditional, records what
it showed on the deal, and prints it on the deal page under Pricing (§4.2, §5). Two audit
codes are new: `THANKS_TOKEN_ISSUED`, `INDICATIVE_TERMS_SHOWN`.

Phase 8c prep (engine `1.9.0`, migration `0019`) adds `pricing.max_quoted_rate` and the row
that exceeds it (§8.10), the rule that lets an automatic run take the Working mark from a
version nobody chose and `versions.human_selected` (§5), and Download Excel on the deal page
(§9).

Phase 8b (engine `1.8.0`, migration `0018`) solves the pricing rate on the ledger to 0.01%
(§8.10) and makes every analysis run a version (§5, §9): a snapshot of its inputs, a name,
one Working version per deal, a switcher and a Draft badge on the deal page, Mark as Working,
Restore inputs, Home on the Working version, History as the version list, and the CLI's
`deals run` / `deals export` by version (§9.6). Three audit codes are new:
`VERSION_RECORDED`, `VERSION_MARKED_WORKING`, `VERSION_RESTORED`. It also fixes Edit Intake
moving a deal off a split product, which the database refused with a 500 because the old
split was flushed beside the new product.

Phase 8a (engine `1.7.0`, no migration) renames the table the Lender Sensitivity and adds the
Borrower Sensitivity (§8.9), the pricing matrix and its `pricing` config block (§8.10), and
the read-only Defaults page (§10). It fixes the names a phoneless team-form deal lost: the
guarantor's name, the email and the entity live on the `borrowers` row, which was created only
when there was a phone; a phoneless intake that names anybody now gets one, and an edit reuses
the deal's own phoneless row rather than minting another.

Phase 10a (engine `2.0.0`, migration `0021`, v0.17) collapses the four products to two loan
structures - 100% at close and Holdback - and takes the analysis toggles out; the Flip
Analysis prints its profit and its cost stack line by line with the borrower's equity,
cash-on-cash against a target and project yield; Sources & Uses is a new section; and each
pricing row carries its de-risk line, most equity first (§3, §8.1, §8.4, §8.10, §8.11).

---

## 13. Open items

- 10a-scenarios' open questions (v0.19):
  - **Our payoff due** is the commitment plus the origination fee's payoff half - what the
    ledger's last row collects - and not the last month's interest with it
  - The take-back's **legal bill lands in the month the lost-interest period ends** (D+L) and
    the carry runs one month at a time after it; an alternative puts both at the sale. The
    **stub days** of a stub term are ignored on the take-back timeline, and the payoff half of
    the fee is never collected
  - The **default month floors at 1** on a term inside the listing period, so the lender
    always collects one month of interest; a term of 0 whole months (all stub) defaults in
    month 1 as well
  - The solves are to the **whole dollar**, rounded up like the pricing matrix's 0.01%; the
    engine probes the NPV at the target rate and confirms the answer on the XIRR (§8.6).
    Confirm bisection on the XIRR alone is not wanted for its own sake - it is 7x the cost
    of the rest of the underwrite
  - A solve whose **lower bound already clears** reads "None needed" on the page and in the
    report (the brief said "no solution" for any bound; it is recorded as `NO_SOLUTION` with
    `bound_hit: LOWER`)
  - The **exit value is never stored as a default**: `NULL` means the sale price, and a typed
    value equal to the sale price - before or after the save - becomes `NULL`. It is the one
    defaultable column `populate` does not write
  - The **as-is value** came back as a plain optional input (dropped with the as-is LTV in
    v0.5); nothing in the screen reads it and no adapter supplies it
  - `foreclosure_months` has **no entry for `OTHER`**: a deal in a state outside the footprint
    has no take-back, with `FORECLOSURE_MONTHS_MISSING`
  - The market rent keeps its column and flag names (`monthly_rent`, `MONTHLY_RENT_MISSING`)
    and its Monthly Rent caption; the analyses call it the market rent
  - The take-back's hold collects **rent net of the rental's expense ratio** and nothing
    else: no holding cost over the hold, no vacancy
  - The Rental card's headline is the **required rent** and the Take-Back card's the **sale
    price needed**; 10b moves both cards into the Scenarios tab

- 10a's open questions (v0.17):
  - The requested row has no de-risk line (it adds nothing); every other row with a solved
    rate has one, an over-the-maximum row included ("..., above the 18% max quoted rate")
  - `BORROWER_COC_BELOW_TARGET` takes its severity from config (placeholder Soft) like the
    two DSCR codes, rather than being fixed in code; it is not tested without a sale price,
    nor when the loan covers every cost
  - A `SPLIT_PRINCIPAL` deal migrated with its Tranche A as the rehab portion as entered; a
    Tranche A above the budget, which used to lower the commitment, now moves the excess to
    the advance at closing. The deal's own interest is now on the whole commitment from
    close, so its next run prices higher than its last
  - Migration `0021` moved every deal carrying the 4% broker stand-in (tagged in
    `defaulted_fields`) to the new 6%; a typed 4% stays. The earlier move of the default rate
    (12% to 12.5%, v0.15) was not migrated this way
  - The structure's caption is **Loan Structure** on the form and the deal page, with the
    two labels exactly as the brief named them ("100% at close", "Holdback")
  - Neither radio is checked on a new deal: left unpicked, the structure is inferred from the
    rehab costs as before. On the deal page panel (v0.18) the radio always shows the deal's
    structure, inferred or chosen; saving it as it stands changes nothing, and picking the
    other one records it as the team's

- LinkedPhone webhook availability
- PropStream export path
- Credco / RicherValues API vs PDF
- Colorado court-record source decision (CoCourts vs manual)
- GLENWOOD leverage/pricing caps (placeholder grid to be filled; the LTC cell is 100%, which
  is no cap at all, and the LTV cell is a 75% placeholder)
- LOI and credit memo templates
- Historical deals for fixtures
- The public form's Spanish (`api/i18n.py`): written for a Latin American audience in the
  `usted` register and pending native-speaker review
- The public form's rate limit (`web_intake.submissions_per_hour_per_ip`, placeholder 5) and
  whether the count should survive a restart
- `interest.default_annual_rate`, placeholder 12.5% (12% until v0.15): the rate every deal is priced at until the
  team enters one. Two edges of the default rule to confirm: a typed value equal to the
  default reads as the default, and a stored default keeps its number if the config default
  later changes (the deal says `DEFAULT` with the old number; the reset link offers the new)
- The config placeholders the mechanics walkthrough left open: the contingency, holding-cost
  and broker percentages, the rental expense ratio, takeout LTV, takeout rate and target DSCR, the
  take-back's foreclosure months and hold periods, the
  take-back's lost-interest months and legal costs, and the stub period's day-count basis
  (`interest.day_count_basis`, placeholder 30 — 30/360; a lender who accrues actual/365 sets 365)
- Home's second section is every deal that did not come in on the public form, not only
  `channel = TEAM`: the SMS, link and contract channels do not exist yet, and a deal on one of
  them would otherwise be in no section at all. Rename or split it when they do
- The purge leaves one `audit_log` row (`DEALS_PURGED`, the counts it deleted) rather than
  none, so the one write that erases the record is itself on the record. Confirm that is wanted
- Edit Intake no longer touches the §8.1 economics, the valuation, the rent or the court
  search (they have no box on the form; it does write the loan split, v0.17); the JSON half of `POST /intake/team` still takes
  them at creation. Confirm the JSON body should go on accepting them
- The actual-payoff entry the stub arithmetic is kept for (§8.1): where it lives and what it
  records when it comes
- `closing.default_lead_days`, placeholder 14: the closing date a deal gets when nobody
  entered one, anchored to the day the deal first came in rather than to the latest edit
- The sensitivity grid's placeholders (§8.9): $5,000 steps, four rows above the request,
  12.5% to 17.5% by 1%; the deal's own rate joins the columns, so the default 12% shows as a
  first column beside them until the range is tuned
- The pricing placeholders (§8.10): a 20% target lender IRR and steps of 0, -5% and -10% of
  the request (v0.14), each stepped row rounded to the nearest $1,000. Steps only lend less;
  a larger loan has no defined advance and is refused by the loader. The solved rate (v0.11)
  is rounded **up** to the next 0.01% so it still clears the target.
  `pricing.max_quoted_rate` (v0.12) is an 18% placeholder for whatever cap GLENWOOD quotes
  under (a usury limit, say); the search itself still runs to 100%, so a row above the cap
  can say what it needs. Rounding is half-up and capped at the request, so a share too small
  to survive the unit prices as the request again; confirm half-up is the rounding wanted
- The thank-you page's indicative terms (v0.14, §4.2): `web_intake.thanks_token_hours` is a
  24-hour placeholder; the results shown are the arrival run's (version 1), never a later
  version's, so a team edit inside the window does not reach the borrower; a Conditional
  screen shows the table exactly as a Go does; the yield shown is the borrower's cash-on-cash
  as §8.9 defines it; the Spanish strings are pending the same native-speaker review as the
  rest of `api/i18n.py`; and a GET records the one write (what was shown) and commits it
- Versions (v0.11, v0.12): a person's own run is always a draft beside an existing Working
  version, even unnamed, while the automatic run after an edit can take the mark - confirm
  that asymmetry is wanted. Version names need not be unique (the CLI refuses an ambiguous
  one). Restore inputs runs as the person, like Save & Run, where an intake restore runs as
  `system`. The JSON stage routes record no version, and a deal priced only through them
  shows its latest runs on Home and the deal page. Backfilled versions (`0018`) cannot be
  restored. Download Excel uses the config the server runs today for the Inputs sheet's
  config notes, not the one the version ran on (its hash is on the version)
- Borrower cash-on-cash is profit over the borrower's equity, the total deal cost less the
  loan, with the total deal cost the flip's own stack - financing included, broker's selling
  costs not (§8.4). Confirm that is the cash GLENWOOD means; its 20% target
  (`pricing.borrower_coc_target`) is a placeholder
- The Underwriting Assumptions panel (v0.9) gave per-deal columns to the five analysis
  assumptions that had none; the takeout LTV, the amortization years, the target DSCR, the
  foreclosure months and the hold periods are still config
  alone. Confirm nothing else from §8.5/§8.6 should be per-deal. Save & Run and Reset all run
  as the person who pressed them, like Run Analysis; the court search's Save runs as `system`,
  like every other edit. Reset all leaves the sale price and the rent alone, since neither has
  a default to go back to; confirm that is the wanted reading of "all"
