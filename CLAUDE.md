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
- `ruff` for lint/format, `mypy --strict` on `engine/`, `schema/`, `config/`, `intake/`, `services/`, `api/` and `cli/`

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
  engine/
    sizing.py              # LTV / LTC / LTARV, product-specific commitment split
    screen.py              # score components + verdict + reasons
    calc/                  # v1 simple model: outstanding.py, lender.py, borrower.py, exit.py, downside.py
    grids.py               # sensitivity grids
    solve.py               # rate solve for target yield
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
    listing_url.py         # URL → address only
    contract_ocr.py
  intake/
    parsers/               # sms_llm.py, contract.py, team_form.py
    normalize.py           # → IntakeRecord, missing_fields
  db/
    models.py              # SQLAlchemy
    repository.py          # intake -> rows
    migrations/            # Alembic
  services/                # the seam: load a deal, run the pure engine, persist, return
    actions.py             # the team actions: advance, decline, mark dead, reopen, note, overrides
    assemble.py            # deals row -> ScreenInputs / UnderwriteInputs; adapter-over-team
    audit.py               # append audit_log rows; read one deal's trail
    enrichment.py          # what the adapters produced (Phase 3); the precedence rule
    intake.py              # store an IntakeRecord with an actor; re-apply an edited one
    lifecycle.py           # the two automatic status transitions (SPEC 4.6)
    passwords.py           # PBKDF2 hash / verify; pure
    persistence.py         # append screens / underwrites rows; rebuild results from them
    queue.py               # the review-queue list: grouping, ordering, the pin rule
    readiness.py           # the SPEC 8.1 checklist: every input, its value, where it came from
    requests.py            # UnderwriteRequest / TeamOverrides: what the team supplies by hand
    runner.py              # run_screen, run_underwrite
    users.py               # create / deactivate / authenticate a queue user
  api/
    main.py, routes/       # auth, queue, intake, deals
    security.py            # the signed session cookie and the who-is-signed-in dependencies
    forms.py, render.py    # HTML form parsing; the Jinja environment and its filters
    intake_form.py         # the team-entry form: its fields, which are required, a deal as one
    templates/             # the review queue's own pages (committed; not the docx templates)
  cli/
    main.py, fixtures.py   # `glenwood run` / `glenwood export` on a fixture, no database
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

**An adapter value always beats a team value.** Anything the team enters by hand (SPEC §6.1) is a stand-in for a source that does not exist yet. The team's entry is never overwritten, and every value the engine ran on records whether it came from an `ADAPTER` or the `TEAM`.

**No hardcoded thresholds.** Any number a lender might want to change (caps, floors, fees, lookbacks, defaults) lives in `config/glenwood.yaml` and is read through `Config`. If you find yourself typing `0.75` or `620` in `engine/`, stop and move it to config.

**Money and rates.** Use `Decimal` for money and rates inside `engine/`. Never `float` for dollar amounts. Round only at output boundaries.

**Adapters are thin and mockable.** Each adapter implements the `Adapter` Protocol in `adapters/base.py`, returns an `AdapterResult` (status, raw payload, parsed result, source, timestamp), and never raises on a remote failure — it returns `status=FAILED` with the error. All network calls go through `httpx` with timeouts. Every adapter has a fixture-backed test that does not hit the network.

**A deal says why a button is off.** Anything the review queue refuses to run, it says the
reason for *before* the person presses it. `services/readiness.py` derives the Underwrite
inputs checklist from the same rules `services/assemble.py` raises `DealNotReady` on, so a
page that calls a deal ready and a run that then refuses it cannot both exist. A new required
input goes in one place and both readers pick it up.

**A missing input is not a failing one.** Where an input has a defensible stand-in, use it and
record the source as `DEFAULT` (`takeout.opex_defaults`). Where it has none - the market rent
is the example - report the thing it feeds as not evaluated, with the figure `None` rather
than `False` or `0`. A DSCR takeout nobody could compute has not fallen short, and a zero
would manufacture a shortfall on every deal whose rent nobody looked up.

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
- **No self-signup and no password reset.** Users are created and deactivated from the command line. Do not add a registration page, an invite link, or a reset-by-email flow to v1.
- **A code the model stores is never a label a person reads.** `Tranche` and
  `ExperienceBucket` are grid coordinates and stored values; `schema/labels.py` turns them
  into the FICO range and the deal count somebody actually picked, and every rendered page and
  flag message goes through it. The credit labels are derived from the config cutoffs, so
  moving a cutoff moves the label.
- **No client-side JS in the queue beyond the copy button.** Server-rendered Jinja, plain form posts, POST-redirect-GET. If a page seems to need script, it needs a different page.
- **Every form post carries a CSRF token.** The guard is a router-level dependency (`api/security.py`), so a new route is covered by where it lives rather than by somebody remembering; every `<form method="post">` renders `{{ csrf.field(csrf_token) }}`. `tests/test_csrf.py` posts to every guarded route without one and asserts the refusal — do not add a route that needs an exemption without saying why there.

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
uv run mypy engine schema config intake services api cli
uv run uvicorn api.main:app --reload     # the review queue at http://127.0.0.1:8000/queue

# the first user; there is no self-signup (SPEC 11). Omit --password to be prompted.
uv run glenwood users create --name "Sam Reed" --email sam@glenwood.example
uv run glenwood users deactivate --email sam@glenwood.example
uv run glenwood users list


# run the engine on a fixture deal, no database:
uv run glenwood run fixtures/synthetic/deals/go_split_draw_denver.json --underwrite
uv run glenwood export fixtures/synthetic/deals/go_split_draw_denver.json denver.xlsx
```

## When unsure

Ask. In particular, do not guess at: leverage caps, fee treatment, draw-curve constants, DSCR takeout assumptions, or anything in a SPEC section marked **[v1 — revisit in mechanics walkthrough]**. Use the placeholder in config and flag it.
