# Forager

Forager is Pantry Raider's hosted companion service at
`forager.pantryraider.app`. It powers the optional subscription features,
starting with managed AI (photo analysis, receipt parsing, and barcode
enrichment without your own API key). The self-hosted app in `service/`
works fully without it; Forager only adds to a linked install.

Design and rationale: [docs/design/cloud-platform.md](../docs/design/cloud-platform.md).
Internal module and table names keep the original generic cloud naming; the
Forager brand appears in user-facing copy, the domain, and deploy config.

## What it does

- Accounts with password login (optional Google sign-in) and a web portal:
  landing page, signup, and an account page with plan, usage, linked
  kitchens, and billing.
- One-step linking: an install signs in with the account's email and
  password and gets its own bearer token in a single call, then appears in
  the account's kitchen list. Pairing codes remain as the advanced path,
  and removal (portal) or unlink (app) revokes the credential.
- An AI proxy backed by Gemini 3.8 Flash. Every new account starts with a
  30-day free trial of 2,000,000 AI tokens a month, the full Premium
  allowance. After that, Cloud Basic includes 100,000 tokens a month and
  Premium 2,000,000. The proxy records real token counts from each response
  in a usage ledger and answers over-quota requests with a 402 the app
  shows as a friendly message.
- A Stripe webhook (signature-verified, idempotent) that turns Checkout and
  subscription events into the entitlement each request checks. Plan prices
  live in Stripe, not in code.
- Recipe share links: a member sends one recipe to one person by URL. The
  page at `/r/{token}` is public (no account needed to read, print, or
  download the recipe as an importable file); shares addressed to a member's
  email land in their "Shared with you" list, anyone else gets the link by
  email, and the owner can turn a link off from the account page at any time.
- An operator admin panel at `/admin`: fleet totals, per-account detail,
  disable/enable, comped plans, kitchen revocation, and an audit trail.

## Admin panel

`/admin` is gated by `CLOUD_ADMIN_EMAILS`, a comma-separated list of account
emails. Sign up for a normal account with a listed email, confirm the address
from the verification email, and turn on two-factor sign-in on the account
page; the panel then opens from that session. Until all three are true, and
for everyone else, signed in or not, `/admin` answers 404, so the panel's
existence is never advertised. An empty list means nobody gets in.

The overview shows totals (accounts, kitchens, active paid subscriptions,
month-to-date tokens and estimated Gemini spend at the blended rate in
`CLOUD_GEMINI_COST_PER_MILLION_TOKENS`) plus a searchable account table. Each
account's detail page lists its kitchens (with revoke), entitlement and
Stripe subscription state, six months of usage, and actions: disable or
enable the account (a disabled account is refused at login, provisioning,
and the AI proxy with a clear message), comp a starter plan until a chosen
date, and expire a comp early. Every admin mutation is written to the
`admin_actions` audit table; the detail page shows the account's trail and
the overview shows the latest twenty actions.

Unlike the subscriber portal, the admin pages are for the operator and speak
plainly about tokens, instances, and Stripe ids.

## Run the tests

The suite is self-contained (SQLite in memory, a stubbed or mocked AI
upstream; production uses Postgres and Gemini):

```bash
cd cloud
pip install -r requirements.txt pytest
python -m pytest
```

## Dependencies

`requirements.txt` is the hand-edited list, every line an exact `==` pin.
`requirements.lock` is its full transitive resolution with a SHA-256 for
every wheel, and it is what the Docker image installs
(`pip install --require-hashes -r requirements.lock`). Change a pin and
regenerate the lock in the same commit with [uv](https://docs.astral.sh/uv/),
from the repository root:

```bash
uv pip compile cloud/requirements.txt --universal --python-version 3.12 --generate-hashes -o cloud/requirements.lock
```

`tests/test_requirements_lock.py` fails when a pin is missing from the lock
or the Dockerfile stops installing from it.

## Deploy on a VPS

Requirements: a small Debian/Ubuntu VPS with Docker and Docker Compose, and
a DNS record for `forager.pantryraider.app` pointing at it.

```bash
git clone https://github.com/Syracuse3DPrintingOrg/PantryRaider.git
cd PantryRaider/cloud
cp -f .env.example .env
nano .env        # Postgres password, Gemini API key, Stripe secrets
docker compose up -d --build
curl https://forager.pantryraider.app/health
```

To update, pull and rebuild from the same directory:

```bash
git fetch origin && git reset --hard origin/main
FORAGER_GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

The `version` field on `/health` (and the status page) is the Pantry Raider
release the build came from, read from `service/app/config.py` through the
compose file's second build context, so the build needs the whole checkout,
not just `cloud/`, and Docker Compose 2.17 or newer. `FORAGER_GIT_SHA` is
optional: with it the version reads like `0.21.88+2dfb7373`, without it just
`0.21.88`. A plain `docker build` of `cloud/` alone still works and reports
`unknown`.

The `.env` file needs, beyond the domain and Postgres password:

- `CLOUD_GEMINI_API_KEY`: the Google AI Studio key behind the AI proxy
  (`CLOUD_AI_FORWARDER=gemini` selects the Gemini upstream).
- `CLOUD_STRIPE_PRICE_STARTER`: the Stripe price id of the starter plan, so
  purchases map to the right quota.
- `CLOUD_STRIPE_SECRET_KEY`: the Stripe API secret key (`sk_...`), used to
  open Customer Portal sessions (the account page's manage/cancel button)
  and to cancel subscriptions directly (the in-app cancel fallback and
  account deletion). Without it those flows explain themselves and point at
  support instead.
- `CLOUD_REPORT_IP_PEPPER`: a random secret mixed into the hash that stands
  in for an anonymous recipe-report visitor's address. Set it once (any long
  random string) before running migrations and leave it alone.
- `CLOUD_DAILY_TOKEN_CEILING` (optional, default 50,000,000): the most AI
  tokens the whole service spends in one UTC day. Once it is reached, AI
  calls answer 503 until midnight UTC and the log shows an error line saying
  so. `0` turns the ceiling off.
- `CLOUD_GOOGLE_APP_ALLOW_NO_CHALLENGE` (optional, default on): whether a
  kitchen may link through "Continue with Google" without a PKCE challenge.
  Current kitchens always send one, and their linking code only redeems with
  the matching verifier. Kitchens on older versions send none and still link
  while this is on; set it to `0` once they have updated to refuse them.

Caddy terminates TLS with automatic certificates; the app container never
binds a public port. Postgres data lives in the `pgdata` volume; back it up
with `docker compose exec db pg_dump -U pantry pantrycloud`.

Point the Stripe webhook endpoint at
`https://forager.pantryraider.app/v1/stripe/webhook` and put its signing
secret in `.env`.

## Go-live checklist

1. DNS: an A record for `forager.pantryraider.app` pointing at the VPS.
2. Bring the stack up and confirm Caddy obtained the certificate:
   `curl -v https://forager.pantryraider.app/health` shows a valid cert and
   `{"status": "ok"}`.
3. `CLOUD_GEMINI_API_KEY` set in `.env` and the app restarted; a paired
   test install can run a photo analysis end to end.
4. Stripe live mode: the starter product's price id in
   `CLOUD_STRIPE_PRICE_STARTER`, the webhook endpoint added in the Stripe
   dashboard, and its signing secret in `CLOUD_STRIPE_WEBHOOK_SECRET`.
5. First account smoke test: sign up, pair an install with a code, run one
   analyze call (free tier), complete a test Checkout, and confirm
   `GET /v1/instance/me` reports the starter quota.

## Layout

| Path | What lives there |
|---|---|
| `app/config.py` | Env-driven settings (`CLOUD_` prefix) and the plan quota table |
| `app/models.py` | Accounts, sessions, instances, pairing codes, subscriptions, entitlements, usage ledger, Stripe events |
| `app/security.py` | scrypt password hashing, token issue/hash, Stripe signature verification |
| `app/usage.py` | Per-account monthly token accounting and the quota gate |
| `app/forwarder.py` | The `AIForwarder` interface: `GeminiForwarder` (production) and `StubForwarder` (tests) |
| `app/routers/` | `accounts`, `instances` (provisioning and pairing), `portal` (the web pages), `oauth_google` (Google sign-in), `ai` (the proxy), `stripe_webhook`, `admin` (the operator panel) |
| `app/templates/` | The portal's server-rendered pages |
| `tests/` | Standalone pytest suite (SQLite, no Docker or network) |

The schema is managed with Alembic (`alembic.ini` and `migrations/`). At
startup an empty database gets every table and is stamped at the current
revision, a database already under Alembic runs `alembic upgrade head`, and an
older database from before Alembic gets only the additive table creation it
always had until someone runs `alembic stamp head` on it once. See
`app/database.py`.
