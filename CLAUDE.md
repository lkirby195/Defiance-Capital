# CLAUDE.md — glenwood-uw

Read `SPEC.md` before doing anything. It is the source of truth for scope, math, and data model. If a task conflicts with the spec, stop and say so rather than improvising.

## What this is

Intake → enrichment → screen → underwrite → outputs pipeline for a hard money lender (Oklahoma and Colorado). Standalone project. **Do not import from or reference any other codebase**, including any other project owned by the same author. Copying a pattern by hand is fine; sharing code is not.

## Stack

- Python 3.12, `uv` for env and deps
- FastAPI (webhook receiver, review-queue API), Jinja2 templates for the queue page (no frontend framework in v1)
- Postgres via SQLAlchemy 2.x + Alembic migrations
- Pydantic v2 for all schemas and config
- pytest; `hypothesis` allowed for engine property tests
- `python-docx` for memo/LOI generation
- `ruff` for lint/format, `mypy --strict` on `engine/`, `schema/`, `config/`, `intake/`, `services/`, `api/`, `cli/` and `adapters/`

## Layout

```
glenwood-uw/
  CLAUDE.md
  SPEC.md
  README.md                # operations: run it, deploy it, make the first user, rotate the secret
  render.yaml              # Render blueprint: build with uv, migrate, then serve
  pyproject.toml
  config/
    glenwood.yaml          # all tunables; placeholder values until the client fills them
    config.py              # Pydantic model that loads and validates the yaml
  schema/
    intake.json            # canonical IntakeRecord JSON schema (generated from Pydantic, committed)
    models.py              # IntakeRecord, UnderwriteResult, enums (Product, Tranche, ExperienceTier, Verdict, ...)
    labels.py              # what a person is shown where the model stores a code (T3 -> "660-699")
    masks.py               # what a person types where the model stores a number ($#,###, 12%, digits)
  engine/
    sizing.py              # LTC / LTV, product-specific commitment split
    screen.py              # score components + verdict + reasons
    calc/                  # the v0.3 ledger model: terms.py, ledger.py, irr.py,
                           #   flip.py, rental.py, analyses.py, sensitivity.py
    version.py             # ENGINE_VERSION string, bump on any math change
  adapters/
    base.py                # Adapter Protocol + AdapterResult
    mortgage_automator.py
    forecasa.py
    oscn.py
    colorado_courts.py
    pacer.py
    propstream.py          # CSV import in v1
    credco.py
    richervalues.py
    linkedphone.py
    listing_url.py         # URL → address only; a pure function today, the LINK adapter later
    contract_ocr.py
  intake/
    parsers/               # sms_llm.py, contract.py, team_form.py, web_form.py (the public form)
    normalize.py           # → IntakeRecord, missing_fields
  db/
    models.py              # SQLAlchemy
    repository.py          # intake -> rows
    migrations/            # Alembic
  services/                # the seam: load a deal, run the pure engine, persist, return
    actions.py             # the team actions: progress, pause, decline, kill, reopen, note, overrides
    assemble.py            # deals row -> ScreenInputs / UnderwriteInputs; adapter-over-team
    audit.py               # append audit_log rows; read one deal's trail
    autorun.py             # the analysis: screen then underwrite, behind Run Analysis and on arrival / edit as `system`
    defaults.py            # the SPEC 8.1 defaults every deal is populated with, and which are defaults
    enrichment.py          # what the adapters produced (Phase 3); the precedence rule
    history.py             # every intake version, screen and underwrite on a deal, with its actor
    intake.py              # store an IntakeRecord with an actor; re-apply an edited one; restore an earlier one
    lifecycle.py           # the two automatic status transitions (SPEC 4.6)
    passwords.py           # PBKDF2 hash / verify; pure
    persistence.py         # append screens / underwrites rows; rebuild results from them
    home.py                # the Home page: the four sections, their membership, the ordering
    purge.py               # the one-time `glenwood deals purge`; every deal table, users kept
    readiness.py           # the SPEC 8.1 checklist: every input, its value, where it came from
    requests.py            # UnderwriteRequest / TeamOverrides: what the team supplies by hand
    runner.py              # run_screen, run_underwrite
    users.py               # create / deactivate / authenticate a queue user
  api/
    main.py, routes/       # auth, queue, intake, deals, apply (the public borrower form)
    security.py            # the signed session cookie and the who-is-signed-in dependencies
    forms.py, render.py    # HTML form parsing; the Jinja environment and its filters
    masks.py               # which box holds which kind of number, and the config defaults
    intake_form.py         # the team-entry form: its fields, which are required, a deal as one
    apply_form.py          # the public form: its fields, which are required, the complaints
    i18n.py                # every string the public form shows, English and Spanish
    ratelimit.py           # posts per address per hour on the public form
    templates/             # the review queue's own pages and the public form's (committed)
  cli/
    main.py, fixtures.py   # `glenwood run` / `glenwood export` on a fixture, no database;
                           #   `glenwood users`, `glenwood deals purge` open it
  outputs/
    console.py             # text report for the CLI
    excel.py               # .xlsx workbook for checking the math by hand
    screen_summary.py, credit_memo.py, loi.py, ma_handoff.py
  templates/               # client-supplied docx templates go here (gitignored until provided)
  fixtures/
    synthetic/             # hand-built deals for unit tests
    historical/            # client deals, gitignored, loaded locally only
  tests/
```

## Rules

**Engine is pure.** Nothing under `engine/` does I/O, touches the database, reads env vars, or calls the network. Functions take typed inputs and a `Config` object and return typed outputs. Every function in `engine/` has a unit test.

**Services own the I/O.** `services/` is the only place that loads a deal, runs the engine on it, and writes the result. `api/` calls services, never the engine directly; services never commit (the caller owns the transaction). `cli/` is the other caller: it reads a fixture off disk and runs the same pure functions with no database at all — a fixture with a `team_entry` block goes through `services/assemble.py` too, so the CLI and the API cannot diverge.

**An adapter value always beats a team value, and a team value beats the borrower's.** Anything the team enters by hand (SPEC §6.1) is a stand-in for a source that does not exist yet; anything the borrower typed on the public form (SPEC §4.2) is their own unverified claim and lives in its own columns. Nothing is overwritten - a team entry replaces a borrower value as the one in force and leaves it on the deal - and every value the engine ran on records whether it came from an `ADAPTER`, the `TEAM` or the `BORROWER`.

**No hardcoded thresholds.** Any number a lender might want to change (caps, floors, fees, lookbacks, defaults) lives in `config/glenwood.yaml` and is read through `Config`. If you find yourself typing `0.75` or `620` in `engine/`, stop and move it to config.

**Money and rates.** Use `Decimal` for money and rates inside `engine/`. Never `float` for dollar amounts. Round only at output boundaries.

**Adapters are thin and mockable.** Each adapter implements the `Adapter` Protocol in `adapters/base.py`, returns an `AdapterResult` (status, raw payload, parsed result, source, timestamp), and never raises on a remote failure — it returns `status=FAILED` with the error. All network calls go through `httpx` with timeouts. Every adapter has a fixture-backed test that does not hit the network.

**A deal says why a button is off.** Anything the review queue refuses to run, it says the
reason for *before* the person presses it. `services/readiness.py` derives the Underwrite
inputs checklist from the same rules `services/assemble.py` raises `DealNotReady` on, so a
page that calls a deal ready and a run that then refuses it cannot both exist. A new required
input goes in one place and both readers pick it up.

**A missing input is not a failing one.** Where an input has a defensible stand-in, the deal
is populated with it at intake and the source recorded as `DEFAULT` (`services/defaults.py`:
the rate, the four fees, the five analysis assumptions, the closing date - the month end
`closing.default_lead_days` after the deal came in - and the loan split on a split product).
The value is stored, so the
engine runs on what the page shows; `deals.defaulted_fields` is what says it was nobody's
choice, and a value equal to the default is the default. Where there is no stand-in - the
monthly rent is the example - report the thing it feeds as `NOT_EVALUATED`, with the figure
`None` rather than `False` or `0`. A DSCR nobody could compute has not fallen short, and a
zero would manufacture a shortfall on every deal whose rent nobody looked up. The one the
ledger cannot run without at all - the term - is named by the readiness checklist and refused
by name.

**The runs are automatic, the page has one button, and the page is labels and values.** A
complete intake is screened and priced on arrival on every channel and again after every
team edit, as the actor `system` (`services/autorun.py`, SPEC §4.6); a route that stores,
edits or restores a deal calls it. A paused deal is left out until Progress brings it back.
The one button is **Run Analysis**, which runs both stages as the person who pressed it; the
page never names a stage - it shows the verdict and the ledger - and the Flags, the Reasons
and the Suggested Reply are on the stored rows, the CLI and the workbook, not on it. The
hand-entered numbers are the **Underwriting Assumptions** panel, open, directly under Deal
Economics: two columns of boxes - the §8.1 economics with their config defaults, the loan
split, the valuation, the rent, the five §8.4-§8.6 analysis assumptions (which carry their
own `deals` columns since migration `0017`, config value as default, engine reads the deal's
when present) and the two toggles - each tagged "default" while it holds its default and
resettable one at a time, with one **Save & Run** at the bottom and a **Reset all to
defaults** beside it that goes through a confirmation page. The court search is its own
collapsed section with its own Save; the readiness checklist and the versions and runs
(History) are collapsed too, each a plain `<details>`; History restores any intake version
as a new submission (`services/intake.restore_intake`). The deal page prints no explanatory
prose beside a value: a definition that is still useful goes behind the (?) on its label, as
a hover title (`_fields.html`, `help`). The team form prints nothing but labels and boxes,
each marked Required or Optional in plain text, and asks only for the Overview, the Property
Overview and seven Deal Economics boxes; everything with a default populates from config and
is edited on the deal page, and Edit Intake writes only the columns the form has a box for
(`db/repository.form_columns`).

**The ledger anchors to month ends.** A deal closes on whatever date the team enters; the
model treats closing as the last day of that month, every period is a month end, and the
payoff date is the last day of the month that is the closing month plus the term
(`schema/dates.py`). The team enters the term in months and no form takes a payoff date; the
stub arithmetic stays in the engine for a future actual-payoff entry.

**Record everything.** Every enrichment call writes an `enrichment_runs` row with the raw response. Every screen and underwrite records `ENGINE_VERSION` and the config hash. Nothing is overwritten; re-runs create new rows.

**Every service write takes an actor and writes `audit_log`.** Credit and court data are in here (SPEC §11), so a write nobody is named for is not acceptable. `services/` never commits; the audit row lands in the caller's transaction beside the change it describes, so a rolled-back change cannot leave an audit row claiming it happened. A password, a hash, or anything else a person would not want in a log never goes in one.

**Config is validated on load.** `Config` must reject a yaml with missing keys or out-of-range values at startup, not at the first deal.

**Bump `ENGINE_VERSION`** on any change to math in `engine/`, and add or update a fixture test that demonstrates the change.

## Hard constraints (do not work around these)

- **No listing-site scraping.** `listing_url.py` extracts an address from the URL string only. Never fetch Zillow, Redfin, Realtor.com, Hubzu, Auction.com, Xome, or similar pages.
- **No automated outbound SMS.** The system drafts `suggested_reply`; a human sends it. Do not add any code path that sends a message to a borrower without a team action.
- **No credit pull without authorization.** `credco.py` must check `deal.credit_authorization_signed` and refuse otherwise. Do not add an override.
- **Mortgage Automator is write-only at handoff.** Do not write to MA before the "LOI accepted" action. Reads (borrower match) are fine anytime.
- **No dependency on any other repo.** See top of file.
- **No self-signup and no reset-by-email.** Users are created and deactivated from the command line; a signed-in user changes their own password at `/account/password` (current password, new one twice, the same floor), and an admin sets a forgotten one with `glenwood users reset-password`. Both are audited and both end every other session on the account (`api/security.py` binds the cookie to the password hash). Do not add a registration page, an invite link, or a reset-by-email flow to v1.
- **A code the model stores is never a label a person reads.** `Tranche` and
  `ExperienceBucket` are grid coordinates and stored values; `Product` and `LoanPurpose`
  are SCREAMING_SNAKE for the same reason. `schema/labels.py`
  turns each into the words somebody actually picked - the FICO range, the deal count, "Split
  Draw" - and every rendered page and flag message goes through it. The credit labels are
  derived from the config cutoffs, so moving a cutoff moves the label. The one exception is
  the caps cell (`SPLIT_DRAW/T3/E2`), which is a config grid coordinate a person looks up
  rather than a label they read.
- **A number the model stores is not the text a person types.** `schema/masks.py` and
  `api/masks.py` are the two halves: money is typed and shown `$425,000`, a percent `12%`
  (the deal carries `0.12`), a phone `555-123-4567` (the deal carries the digits). The
  conversion happens at the HTML boundary and nowhere else - the models, the database and the
  engine never see 12 - so the JSON half of `POST /intake/team` still speaks in what is
  stored.
- **No client-side JS in the queue beyond the input masks.**
  Server-rendered Jinja, plain form posts, POST-redirect-GET. The one exception is a
  convenience and not load-bearing: the masks in `base.html` that format a price, a percent
  and a phone as they are typed and toggle the "default" tag on the §8.1 inputs that have
  one, the two analysis toggles included. The server parses `$425,000`, `425000`, `12%` and
  `12` alike, and renders the tag hidden or shown itself, so a browser that runs none of it
  still posts a deal that saves and still reads the right tag. The collapsed sections on
  the deal page are the browser's own `<details>`, and Reset all to defaults confirms on a
  page of its own, as Kill does. Nothing client-side validates, fetches or decides. If a page
  seems to need script for anything else, it needs a different page. The public borrower form (`/apply`,
  SPEC §4.2) adds one more on the same terms: a few lines that keep the submit button off
  until every required box is filled and lift `required` off the address boxes while the
  listing link holds a value. Dependency-free, and the server checks every rule again
  (`api/apply_form.py`), so a browser without it posts a form that is answered the same way.
- **Every form post carries a CSRF token.** The guard is a router-level dependency (`api/security.py`), so a new route is covered by where it lives rather than by somebody remembering; every `<form method="post">` renders `{{ csrf.field(csrf_token) }}`. `tests/test_csrf.py` posts to every guarded route without one and asserts the refusal — do not add a route that needs an exemption without saying why there. The one exemption is the public borrower form: a token is bound to a signed-in user and a borrower has no account; the honeypot and the rate limit stand in front of it instead, and `tests/test_csrf.py` says so.
- **The borrower is told nothing.** `/apply/thanks` says the team will be in touch. Never a
  verdict, a rate, an amount or a status: the screen and the underwrite run on arrival
  (SPEC §4.6), and what they say stays on the team's side. The page's strings live in
  `api/i18n.py` in English and Spanish; the Spanish column is marked pending native-speaker
  review there and stays marked until one has read it.

## Style

- Small modules, explicit names, type hints everywhere. Prefer functions over classes in `engine/`.
- Docstrings state the formula or the source of a rule and reference the SPEC section (e.g., `# SPEC §8.4`).
- Every flag has a stable `code`, a `severity`, and a human message that names the threshold tested.
- Commit messages: `area: what changed` (`engine: add S-curve avg outstanding`, `adapters: forecasa lien parser`).
- Do not add features not in `SPEC.md`. If something seems necessary, note it in the PR description and ask.

## Commands

```
uv sync
uv run alembic upgrade head
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy engine schema config intake services api cli adapters
uv run uvicorn api.main:app --reload     # Home at http://127.0.0.1:8000/queue
                                         # the borrower form at http://127.0.0.1:8000/apply

# the first user; there is no self-signup (SPEC 11). Omit --password to be prompted.
uv run glenwood users create --name "Sam Reed" --email sam@glenwood.example
uv run glenwood users deactivate --email sam@glenwood.example
uv run glenwood users reset-password --email sam@glenwood.example   # a forgotten one; prompts
uv run glenwood users list

# the one-time cleanup (README): every deal and everything hanging off it; the users stay.
uv run glenwood deals purge --all --confirm

# run the engine on a fixture deal, no database:
uv run glenwood run fixtures/synthetic/deals/go_split_draw_denver.json --underwrite
uv run glenwood export fixtures/synthetic/deals/go_split_draw_denver.json denver.xlsx
```

## When unsure

Ask. In particular, do not guess at: leverage caps, fee treatment, the listing period, the rental takeout assumptions, or the take-back's lost-interest months and legal costs. Use the placeholder in config and flag it.
