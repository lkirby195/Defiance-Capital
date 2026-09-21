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

Then <http://127.0.0.1:8000/queue>, which will send you to `/login`.

`SESSION_SECRET` has no default and the app refuses to start a session without one — a
default would be a signing key every copy of this repository knows. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

There is no self-signup and no password reset. Accounts are created and deactivated from the
command line, which is the point: the list of people who can read credit and court findings
is one somebody maintains deliberately.

```bash
uv run glenwood users create --name "Sam Reed" --email sam@example.com   # prompts for a password
uv run glenwood users list
uv run glenwood users deactivate --email sam@example.com
```

Omit `--password` and it prompts twice, which keeps the password out of your shell history.

### The checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy engine schema config intake services api cli
```

The database-backed tests need a throwaway Postgres. They find one on their own — an embedded
server from the `pgserver` dev dependency — unless `TEST_DATABASE_URL` points at one, in which
case **its tables are dropped and recreated around every test**. Never point it at anything
you care about.

## Deploying

[`render.yaml`](render.yaml) is a Render Blueprint. Connect the repository once as a Blueprint
and every push to `main` deploys: `uv sync --frozen --no-dev`, then `alembic upgrade head`
against the live database before the new code serves anything, then uvicorn.

**First time:**

1. Create a Postgres instance on Render, in the same region as the service.
2. New → Blueprint, point it at this repository. Render reads `render.yaml`.
3. Set `DATABASE_URL` on the service to the database's **Internal Database URL**. Render hands
   it out as `postgres://…`; paste it as-is. `db/session.py` names the psycopg driver itself,
   so the bare prefix that SQLAlchemy would otherwise resolve to an uninstalled driver is not
   a thing you have to remember.
4. Leave `SESSION_SECRET` alone. `generateValue: true` means Render minted one at create time
   and nobody has ever seen it, which is what a signing key wants.
5. Deploy. The first one runs every migration from empty.

To wire the database in the blueprint instead of by hand, replace the `DATABASE_URL` entry
with a `fromDatabase` reference and add a `databases:` block — worth doing once the database
name is settled, and not before, because a blueprint that owns the database can also destroy
it.

`preDeployCommand` is a paid-instance feature. On a free instance, move `alembic upgrade head`
to the front of `startCommand`; it is idempotent, but it then runs on every boot and two
instances can race it, so treat that as a stopgap.

### Creating the first user against the deployed database

The deployed app has no users and no way to make one through the browser. Two routes, and the
second always works:

**From the Render Shell** (Starter and above), on the service:

```bash
uv run glenwood users create --name "Your Name" --email you@example.com
```

It prompts for the password, so it does not land in the shell history — and the shell already
has `DATABASE_URL` in its environment.

**From your machine, against the External Database URL.** Render gives each database an
external URL for exactly this. Pass it for the one command rather than putting it in `.env`,
so a later `uv run pytest` or `alembic upgrade` cannot pick up production by accident:

```bash
DATABASE_URL='postgres://user:password@host.oregon-postgres.render.com/glenwood' \
  uv run glenwood users create --name "Your Name" --email you@example.com
```

Check it landed, then sign in at `https://<service>.onrender.com/login`:

```bash
DATABASE_URL='…' uv run glenwood users list
```

`glenwood users` is the only command that touches a database; `glenwood run` and
`glenwood export` read a fixture off disk and never open one.

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

## Operational notes

- **Nothing the app serves is public.** Every route is behind a session cookie. Pages redirect
  to `/login`; the JSON routes under `/deals` answer 401.
- **Every form post carries a CSRF token** bound to the signed-in user and expiring with their
  session. A post without one is refused with a 403 and writes nothing. JSON bodies are exempt
  and only JSON bodies — a cross-site HTML form cannot produce one.
- **Every write is recorded.** `audit_log` names who did what, including sign-in and sign-out.
  A deal's own trail is at the bottom of its page.
- **Deactivating a user is not deleting them.** `audit_log.actor` names people who have left,
  and a row whose actor resolves to nobody would be worse than useless.
- **Nothing is ever sent to a borrower automatically.** The screen drafts a reply; a person
  copies it, edits it and sends it by hand (SPEC §11).
