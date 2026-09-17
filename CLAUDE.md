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
- `ruff` for lint/format, `mypy --strict` on `engine/`, `schema/`, `config/`, `intake/` and `services/`

## Layout

```
glenwood-uw/
  CLAUDE.md
  SPEC.md
  pyproject.toml
  config/
    glenwood.yaml          # all tunables; placeholder values until the client fills them
    config.py              # Pydantic model that loads and validates the yaml
  schema/
    intake.json            # canonical IntakeRecord JSON schema (generated from Pydantic, committed)
    models.py              # IntakeRecord, UnderwriteResult, enums (Product, Tranche, ExperienceTier, Verdict, ...)
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
    assemble.py            # deals row -> ScreenInputs / UnderwriteInputs; adapter-over-team
    enrichment.py          # what the adapters produced (Phase 3); the precedence rule
    lifecycle.py           # the two automatic status transitions (SPEC 4.6)
    persistence.py         # append screens / underwrites rows; rebuild results from them
    requests.py            # UnderwriteRequest: the SPEC 8.1 inputs supplied at underwrite time
    runner.py              # run_screen, run_underwrite
  api/
    main.py, routes/       # webhooks, queue, actions (advance, decline, generate LOI, handoff)
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

**Record everything.** Every enrichment call writes an `enrichment_runs` row with the raw response. Every screen and underwrite records `ENGINE_VERSION` and the config hash. Nothing is overwritten; re-runs create new rows.

**Config is validated on load.** `Config` must reject a yaml with missing keys or out-of-range values at startup, not at the first deal.

**Bump `ENGINE_VERSION`** on any change to math in `engine/`, and add or update a fixture test that demonstrates the change.

## Hard constraints (do not work around these)

- **No listing-site scraping.** `listing_url.py` extracts an address from the URL string only. Never fetch Zillow, Redfin, Realtor.com, Hubzu, Auction.com, Xome, or similar pages.
- **No automated outbound SMS.** The system drafts `suggested_reply`; a human sends it. Do not add any code path that sends a message to a borrower without a team action.
- **No credit pull without authorization.** `credco.py` must check `deal.credit_authorization_signed` and refuse otherwise. Do not add an override.
- **Mortgage Automator is write-only at handoff.** Do not write to MA before the "LOI accepted" action. Reads (borrower match) are fine anytime.
- **No dependency on any other repo.** See top of file.

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
uv run mypy engine schema config intake services
uv run uvicorn api.main:app --reload

# run the engine on a fixture deal, no database:
uv run glenwood run fixtures/synthetic/deals/go_split_draw_denver.json --underwrite
uv run glenwood export fixtures/synthetic/deals/go_split_draw_denver.json denver.xlsx
```

## When unsure

Ask. In particular, do not guess at: leverage caps, fee treatment, draw-curve constants, DSCR takeout assumptions, or anything in a SPEC section marked **[v1 — revisit in mechanics walkthrough]**. Use the placeholder in config and flag it.
