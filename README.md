# glenwood-uw

Intake → enrichment → screen → underwrite → outputs for a hard money lender in Oklahoma and
Colorado. This file is operations: how to run it locally, how to deploy it, and the two or
three things you will need to do to a running instance.

**[SPEC.md](SPEC.md) is the source of truth** for scope, math and the data model.
[CLAUDE.md](CLAUDE.md) is the working agreement — layout, the rules the code holds itself to,
and the commands.

## Running it locally

```bash
uv sync
cp .env.example .env            # then fill in DATABASE_URL and SESSION_SECRET
uv run alembic upgrade head
uv run glenwood users create --name "Your Name" --email you@example.com
uv run uvicorn api.main:app --reload
```

Then <http://127.0.0.1:8000/queue> — Home — which will send you to `/login`. The borrower's own form
is at <http://127.0.0.1:8000/apply> and needs no sign-in; `/apply?lang=es` is the Spanish
page and `/apply?src=<slug>` records where the link was posted (a flyer, a partner, a
campaign) on the deal as its source.

FastAPI's own API docs are off; `GLENWOOD_API_DOCS=1 uv run uvicorn api.main:app --reload`
serves them for that run at <http://127.0.0.1:8000/docs> (it is read from the shell when the
app starts, not from `.env`).

`SESSION_SECRET` has no default and the app refuses to start a session without one — a
default would be a signing key every copy of this repository knows. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

There is no self-signup and no reset-by-email. Accounts are created and deactivated from the
command line, which is the point: the list of people who can read credit and court findings
is one somebody maintains deliberately. A signed-in person changes their own password from
the Change password link in the top bar; when somebody has forgotten theirs, set a new one
from the command line.

```bash
uv run glenwood users create --name "Sam Reed" --email sam@example.com   # prompts for a password
uv run glenwood users list
uv run glenwood users deactivate --email sam@example.com
uv run glenwood users reset-password --email sam@example.com             # prompts for the new one
```

Omit `--password` and it prompts twice, which keeps the password out of your shell history.
A changed or reset password signs that person out of every session at once.

### The one-time cleanup

```bash
uv run glenwood deals purge --all --confirm
```

Deletes **every** deal and everything hanging off it — borrowers, entities, properties,
submissions, screens, underwrites, versions, enrichment runs, documents, `ma_sync` rows and the audit log
— and leaves the `users` table alone. It is for wiping the test deals out of a database before
the real ones arrive, and for nothing else: there is no undo and it takes no backup. It
refuses to run without both flags on the line, and it opens the database only once they are.
What it leaves behind, apart from the users, is one `audit_log` row (`DEALS_PURGED`) carrying
the count of rows it deleted from each table.

### The checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy engine schema config intake services api cli adapters
```

The database-backed tests need a throwaway Postgres. They find one on their own — an embedded
server from the `pgserver` dev dependency — unless `TEST_DATABASE_URL` points at one, in which
case **its tables are dropped and recreated around every test**. Never point it at anything
you care about.

## Deploying

[`render.yaml`](render.yaml) is a Render Blueprint. Connect the repository once as a Blueprint
and every push to `main` deploys: `uv sync --frozen --no-dev`, then `alembic upgrade head`
against the live database before the new code serves anything, then uvicorn.

**The database is Neon, not Render Postgres.** Render runs the service; the database is the
Neon project `glenwood`, created at Neon directly, not through a Render or Vercel
integration. Today the project is in Logan's own Neon organization; it moves to Defiance's in
Workstream 2 of the go-live plan. The Neon project should be in the same region as the Render
service: `render.yaml` says `oregon`, so AWS US West (Oregon). `DATABASE_URL` is the only
credential the service holds for it, and it lives in one place: the service's environment
variables on Render. It is not in `render.yaml`, not in git, and not in anybody's `.env`.

**Idle connections.** The database pool pings a connection before handing it to a request
(`db/session.py`, `pool_pre_ping`). Without it, a connection the managed Postgres or the
network dropped during a quiet spell was served to the next request and failed it with
`psycopg.OperationalError: server closed the connection unexpectedly` - a 500 on the first
page after the gap, typically right after signing in, that a refresh cleared. If one ever
comes back, that line in the service log is what to look for, right after the
`glenwood.auth` lines that bracket the sign-in.

**First time:**

1. In the Neon console, open the `glenwood` project and its production branch, press
   **Connect**, and copy the connection string for the app's database and role. Take the
   **direct** one (the host without `-pooler`): the service keeps its own connection pool,
   and `alembic upgrade head` wants a session of its own.
2. New → Blueprint, point it at this repository. Render reads `render.yaml`.
3. Set `DATABASE_URL` on the service (Environment) to that string. Neon hands it out as
   `postgresql://…?sslmode=require`; paste it as-is, `sslmode` and all. `db/session.py` names
   the psycopg driver itself, so the bare prefix that SQLAlchemy would otherwise resolve to an
   uninstalled driver is not a thing you have to remember. Put it nowhere else.
4. Leave `SESSION_SECRET` alone. `generateValue: true` means Render minted one at create time
   and nobody has ever seen it, which is what a signing key wants.
5. Deploy. The first one runs every migration from empty.

The blueprint does not own the database and cannot: it is Neon's, outside Render, so the
`DATABASE_URL` entry stays `sync: false` and is set by hand, and no `render.yaml` change can
delete it.

`preDeployCommand` is a paid-instance feature. On a free instance, move `alembic upgrade head`
to the front of `startCommand`; it is idempotent, but it then runs on every boot and two
instances can race it, so treat that as a stopgap.

**Migrations `0010`, `0011` and `0016` delete rows.** The Phase 5 underwrite (SPEC §8, engine
`1.0.0`) shares no result shape with the one before it, and no ledger could be reconstructed
from a row that never had one, so `0010` deletes every `screens` and `underwrites` row written
before it. `0011` does the same for engine `1.1.0`, which dropped `ltv_basis`,
`as_is_value_source`, `MetricCheck.basis` and the `LTV_AS_IS` / `LTARV` metrics from the
result (SPEC §7.4): a stored row carrying any of them cannot be rebuilt into a model that
forbids extras, and one such row would take down the deal page. `0016` deletes every
`underwrites` row for engine `1.4.0`, which anchors the ledger to month ends (SPEC §8.3) and
replaces the result's `exit` with `analyses`: a stored ledger no longer says what the engine
would, and its shape no longer loads. It also drops `deals.asset_type` and
`deals.stated_exit` and adds `PAUSED` to the status enum. The deals themselves keep their
intake, their team entry, their screens, their status and their whole audit trail;
re-pricing one is a button, and every team edit re-prices on its own. Take a dump first if the
deployed database holds runs anybody wants to read again.

**`0011` also rewrites every phone number.** They are stored as digits now
(`5551234567`, shown and typed `555-123-4567`), where they were E.164 (`+15551234567`), and
the phone is the borrower match key (SPEC §5). The rewrite skips any row whose digits-only
form is already taken - the column is unique, and a collision is two rows that were always the
same borrower, which is a merge rather than a migration. Check for one afterwards if the
deployed database has borrowers in it.

### Creating the first user against the deployed database

The deployed app has no users and no way to make one through the browser. Two routes, and the
second always works:

**From the Render Shell** (Starter and above), on the service:

```bash
uv run glenwood users create --name "Your Name" --email you@example.com
```

It prompts for the password, so it does not land in the shell history — and the shell already
has `DATABASE_URL` in its environment.

**From your machine, against Neon.** Neon has no internal and external URL: the connection
string the service uses (Neon console → `glenwood` → **Connect**) reaches the database from
anywhere over TLS. Pass it for the one command rather than putting it in `.env`, so a later
`uv run pytest` or `alembic upgrade` cannot pick up production by accident, and so the one
credential still lives only on Render:

```bash
DATABASE_URL='postgresql://<role>:<password>@<endpoint>.neon.tech/<database>?sslmode=require' \
  uv run glenwood users create --name "Your Name" --email you@example.com
```

Check it landed, then sign in at `https://<service>.onrender.com/login`:

```bash
DATABASE_URL='…' uv run glenwood users list
```

`glenwood users` and `glenwood deals` (`run`, `export` and `purge`) are the only commands
that touch a database; `glenwood run` and `glenwood export` read a fixture off disk and never
open one. `glenwood deals run <deal-id>` prints a stored deal's Working version and
`glenwood deals export <deal-id> out.xlsx` writes its workbook; `--version` takes another
version's number or exact name.

### Rotating SESSION_SECRET

The secret signs session cookies and CSRF tokens. Rotating it invalidates every one of both,
so **everybody is signed out and every open form has to be re-submitted**. No passwords
change and no data moves.

Rotate when someone who had access to the environment leaves, if the value has been pasted
anywhere it should not have been, or on whatever schedule you decide.

1. Render dashboard → the service → Environment → `SESSION_SECRET` → Regenerate (or paste a
   value from `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Save. Render restarts the service with the new value.
3. Everyone signs in again. There is nothing else to do — no session table to clear, because
   there is no session table.

Two things worth knowing before you do it:

- **There is no overlap window.** The app verifies against one secret, so a rotation is a
  clean break rather than a drain. That is deliberate: the reason to rotate is usually that
  the old value should stop working *now*.
- **It is not how you remove one person's access.** For that, deactivate the user:
  `glenwood users deactivate --email them@example.com`. The `active` flag is read on every
  request, so their live session stops on their next click and everybody else keeps working.
  Rotating the secret to lock one person out is a blunt instrument that also signs out the
  people you did not mean to.

### Rotating the Neon password

`DATABASE_URL` carries the database role's password, and it is the service's only database
credential. Rotating it changes nothing anybody sees: sessions are signed with
`SESSION_SECRET`, not the database password, so nobody is signed out.

Rotate when someone who could read Render's environment leaves, if the connection string has
been pasted anywhere it should not have been (a chat, a ticket, a shell history), or on
whatever schedule you decide.

1. Neon console → the `glenwood` project → **Branches** → the production branch →
   **Roles & Databases** → the role in `DATABASE_URL` → **Reset password**. Copy the new
   connection string (**Connect**, as in "First time").
2. Render dashboard → the service → Environment → `DATABASE_URL` → paste the new string →
   **Save and deploy**. The deploy's `alembic upgrade head` is the first thing to use it, so a
   bad paste shows up as a failed deploy, in the deploy log, before any page does.
3. Anyone who copied the old string for a one-off command (the first-user step above) now
   holds a dead one, which is the point.

The old password stops working the moment it is reset. Connections the service already has
open stay up, but any new one fails until step 2 lands, so do the two steps back to back and
at a quiet time.

### Backups

**What exists today: Neon's point-in-time restore.** The Launch plan keeps 7 days of history,
so the database can be put back to any moment in the last week. Nothing has to be switched on
for it.

To restore:

1. Find the moment to go back to - the `audit_log` rows on either side of whatever went
   wrong say when it happened.
2. To look before restoring: Neon console → **Branches** → **New branch** from the production
   branch at that time, point a one-off `DATABASE_URL` at the new branch's connection string,
   and read it (`uv run glenwood users list`, `uv run glenwood deals run <deal-id>`). Delete
   the branch afterwards.
3. Neon console → **Branches** → the production branch → **Restore** → that date and time →
   restore. Neon keeps the state it replaced as a backup branch, so a restore can itself be
   undone.

A restore puts back **everything** to that moment - deals, runs, users, and the audit log
too - so whatever happened after it is gone from the database unless it is redone by hand.
The connection string does not change; the service reconnects on its own (`pool_pre_ping`).

**What is planned and not built: a nightly `pg_dump`** to an S3 bucket Defiance owns, so
there is a copy older than 7 days and one that does not depend on the Neon account. Until it
lands, take a dump by hand before anything destructive - a migration that deletes rows, a
`glenwood deals purge` - with a `pg_dump` at least as new as the project's Postgres:

```bash
pg_dump --format=custom --file "glenwood-$(date +%F).dump" '<the connection string>'
```

### Turnstile

Cloudflare Turnstile stands in front of `/apply` and Adjust my numbers once both keys are set
on the service (SPEC §4.2); until then the form has the honeypot and the rate limit and no
widget. To turn it on:

1. Cloudflare dashboard → **Turnstile** → **Add widget**. Name it for the form, widget mode
   **Managed**, and add both hostnames: `glenwood-uw.onrender.com` and
   `app.defiancecapitalco.com`. Create it.
2. Copy the **Site Key** and the **Secret Key** it shows.
3. Render dashboard → the service → Environment → set `TURNSTILE_SITE_KEY` and
   `TURNSTILE_SECRET_KEY` → **Save and deploy**.
4. Open `/apply` on each hostname: the widget sits above the submit button. Send a test deal
   and delete it from Home.

It **fails open**: when Cloudflare cannot be reached, times out or answers anything but a
200, the post goes through and the service log says `turnstile: siteverify …; letting the
post through` on `glenwood.turnstile`. A refused secret (a mistyped key) goes the same way,
with its own warning - `turnstile: the secret key was refused …; letting the post through` -
so until the key is fixed Turnstile checks nothing; check the log after step 3. A hostname
not on the widget's list shows the widget's own error instead of a challenge, so the form
carries no token and **every post from that hostname is refused**; add the hostname in
Cloudflare, or unset the keys until it is. Cloudflare publishes test keys that always pass
or always fail (the Turnstile docs, "Testing") for trying it on `localhost`.

## Operational notes

- **Nothing the team reads is public.** Every route but the borrower form is behind a session
  cookie. Pages redirect to `/login`; the JSON routes under `/deals` answer 401.
- **`/apply` is the one public page.** It writes a new deal and reads back the borrower's
  own indicative terms on the thank-you link - every rate cushioned by
  `pricing.borrower_rate_cushion` and rounded up to `pricing.borrower_rate_rounding` - with
  an Advanced view of their own cash flows and an Adjust my numbers form that re-runs the
  deal as "Borrower edit n" (SPEC §4.2). Every response under `/apply` says
  `X-Robots-Tag: noindex`. A hidden honeypot box drops a bot's post without
  a word, and `web_intake.submissions_per_hour_per_ip` in `config/glenwood.yaml` (placeholder
  5) turns the next post from one address in an hour away with a polite line. The count is
  per process and starts over on a restart, which is fine for what it protects. Behind a
  proxy the address is the last entry in `X-Forwarded-For`, which is the one the proxy added.
  With its two keys set, Cloudflare Turnstile is checked after both (see "Turnstile" above).
- **Failed sign-ins are recorded and lock the sign-in.** Each one is an `audit_log` row
  (`SIGN_IN_FAILED`, actor `anonymous`, the email and the address, never the password). Ten
  for one email, or from one address, in 15 minutes (`sign_in.lockout_failures` and
  `sign_in.lockout_minutes`) turn the next attempt away with "Too many sign-in attempts. Try
  again in N minutes." There is nothing to reset: the lock lifts 15 minutes after the last
  failure, because attempts made while locked are not counted. The service log line
  `sign-in refused while locked` names the email and the address.
- **Every response carries the security headers** (`nosniff`, `strict-origin-when-cross-origin`,
  `frame-ancestors 'none'`, `X-Frame-Options: DENY`), HSTS over HTTPS, and the session cookie
  is `Secure` behind Render's proxy because the app reads `X-Forwarded-Proto` itself. Do not
  add `--forwarded-allow-ips='*'` to the start command: it would make the client's own first
  `X-Forwarded-For` entry the address the app believes.
- **The Spanish on `/apply` is pending native-speaker review.** Every string is in
  `api/i18n.py`; the `es` column is marked there and should be read by a native speaker before
  the link goes on a flyer.
- **Every form post carries a CSRF token** bound to the signed-in user and expiring with their
  session. A post without one is refused with a 403 and writes nothing. JSON bodies are exempt
  and only JSON bodies — a cross-site HTML form cannot produce one.
- **Every write is recorded.** `audit_log` names who did what, including sign-in and sign-out.
  A deal's own trail is at the bottom of its page.
- **Deactivating a user is not deleting them.** `audit_log.actor` names people who have left,
  and a row whose actor resolves to nobody would be worse than useless.
- **Nothing is ever sent to a borrower automatically.** The screen drafts a reply; a person
  copies it, edits it and sends it by hand (SPEC §11).
- **Market data is RentCast, and it is optional.** With `RENTCAST_API_KEY` set (the dashboard
  on Render, `.env` locally) every deal is enriched on arrival - the property record, the
  automated valuation and the long-term rent estimate, three requests - and the deal page's
  Refresh market data button pulls again. Without it nothing is pulled and the team's own
  as-is value and rent stand. The free tier is 50 requests a month: `rentcast.monthly_request_budget`
  in `config/glenwood.yaml` (placeholder 50) is where the pull stops and says so, and
  `rentcast.cache_days` (placeholder 30) is how long one address's pull serves the next deal at it.
  Every call is an `enrichment_runs` row with the raw body, and every pull an audit row (SPEC §6.2).
