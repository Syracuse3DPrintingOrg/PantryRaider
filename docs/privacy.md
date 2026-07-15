# Privacy policy

Pantry Raider is self-hosted: your inventory, recipes, photos, and settings
live on your own hardware, and the app sends no telemetry, no analytics, and
no usage reports to anyone. The few features that do reach the internet are
listed below, each with exactly what is sent, where it goes, and how to turn
it off.

This page is written the same way as
[What needs the internet](what-needs-internet.md): every claim here matches
what the code actually does, including the imperfect parts. Where something
is not yet as good as it should be, this page says so instead of papering
over it.

## What stays on your hardware

Everything, unless a section below says otherwise. In particular:

- Your inventory, expiry dates, and shopping list (in Grocy, on your box).
- Your recipes, meal plan, and cooking history.
- Photos you scan, receipts, and camera feeds.
- Your settings, API keys, and passwords.
- Timers, nutrition logs, audit sessions, and everything on the kiosk.
- The Home Assistant integration, Stream Deck, thermometers, fridge sensors,
  shelf buttons, and Bandit satellites: all of this talks over your own
  network and never leaves it.
- The Bluetooth status broadcast for battery displays, when you turn it on,
  is one-way and carries only counts and numbers (expired, expiring, pending,
  timers, a probe reading). It never contains item names, tokens, or anything
  personal, by design.
- Diagnostics and support bundles. The debug log is a rotating file on your
  device, secrets are redacted from the downloadable copy, and nothing is
  ever uploaded automatically. Sharing a bundle is always a manual act by
  you.

There are no accounts, sign-ups, or registrations required to run the app.

## What leaves your install, and when

### AI photo and receipt scanning

Only when you configure an AI provider. If you choose Google Gemini, OpenAI,
or Anthropic, the photo being analyzed (a food photo or a receipt) and the
related text prompts are sent to that provider under your own API key, the
same as any app using those APIs. Text tasks can also send item names: barcode
enrichment sends the product's public catalog fields, and "suggest from
inventory" sends the names of items in stock (never the whole database, and
never anything about you).

If you choose Ollama, everything runs on your own hardware and no image or
text leaves your network. If you configure nothing, no AI requests are made
at all, and AI-only buttons do not appear.

Forager's managed AI is a fourth option, covered in the Forager section
below.

**Off switch:** set the AI provider to Ollama, or leave AI unconfigured.

### Barcode lookup

Scanning a barcode sends the barcode number, and nothing else, to the
community [Open Food Facts](https://world.openfoodfacts.org) database to turn
it into a product name. The request identifies the app by name in its user
agent; it carries no account, install id, or inventory data. There is no
toggle for this because it only happens when you scan, and a scanned barcode
is useless without a lookup; if you prefer, type item names in by hand and no
lookup occurs.

The optional AI enrichment step then sends the returned public product fields
to your configured AI provider (see above); with Ollama or no provider, that
step stays local or is skipped.

### Recipe search from public catalogs

When external recipe suggestions are enabled (TheMealDB by default, or
Spoonacular if you add a key), searches send your query text, and
suggestions-from-stock send simplified ingredient names derived from your
inventory (for example "chicken thighs"), capped to a handful per request.
No quantities, dates, or anything else goes along.

**Off switch:** set the recipe source to your own library only, in Settings.

### Importing a recipe from a link

Pasting a recipe URL makes your install fetch that page, so the site you
pasted sees a normal page request from your network. Nothing is sent anywhere
else.

### Weather

The kiosk weather panel sends your configured location text to Open-Meteo
(geocoding plus forecast). If Open-Meteo is unavailable it falls back to
wttr.in; with no location configured, wttr.in estimates one from your
network's public address. Requests happen only while the weather panel or a
weather tile is in use, cached for ten minutes.

**Off switch:** do not use the weather panel or tile; you can also point the
app at a self-hosted Open-Meteo instance in Settings.

### Community shelf life

Two separate switches, documented in full on
[Community shelf life and privacy](community-shelf-life.md):

- **Downloading** the aggregated shelf-life table (on by default) fetches a
  small file from Forager once a day. The request is anonymous and sends
  nothing about you or your pantry.
- **Sharing** your own date corrections is opt-in and off by default. Each
  shared point is anonymous by construction: no account, no install id, no
  address, day-level dates only, and the server publishes a product only
  after at least five separate kitchens agree.

### Update checks

The app checks GitHub for a newer version so it can show the update notice:
a plain unauthenticated request to GitHub for the latest release or version
number, cached for a few hours. GitHub sees what any website you visit sees,
your network's public address and a generic client signature; the request
carries no install id or account. Applying an update downloads code and
container images from GitHub and its container registry. Cub firmware is
fetched from GitHub releases by your own server and cached locally, so the
flasher page works without your browser touching GitHub.

There is currently no switch that disables the version check itself; the
auto-update switch only controls whether updates are applied automatically.
If your install has no internet route, the check simply fails quietly and
everything else works.

### Remote access

Off by default. If you enable it, traffic to your kitchen flows through the
tunnel you chose: your own Cloudflare Tunnel, or Forager's built-in remote
access (covered below). On your LAN, nothing is exposed to the internet.

### Backups you push to cloud storage

Pushing a backup to your own rclone remote (S3, Drive, and so on) is a
manual action that sends the backup archive to the storage account you
configured. Secrets are left out unless you explicitly include them. Nothing
is ever backed up off your hardware automatically.

### Amazon links

Some hardware pages link to Amazon with the project's affiliate tag, so a
purchase after clicking supports the project at no cost to you. These are
plain links your browser follows if you click them; the app itself sends
nothing to Amazon, and no purchase or browsing data ever comes back to it or
to us.

## Forager accounts and shares

Forager is the optional cloud companion. The app is fully usable without it;
nothing in this section applies until you create an account and link a
kitchen.

**What an account stores:** your email address, a password hash (never the
password), and, if you enable them, two-factor and passkey credentials (a
passkey stores only its public half; the secret stays on your device). Every
sign-in, pairing, and instance token is stored only as a hash. Forager does
not ask for or store your name, address, or phone number.

**What a linked kitchen registers:** a name you choose, the app version, and
the deployment mode, so your account page can show your kitchens. No LAN
addresses or user IPs are stored for it.

**Payments:** handled by Stripe. Forager stores only Stripe's customer and
subscription identifiers and the subscription status; card numbers and
billing details never touch Forager's servers.

**Managed AI:** photos and text you scan through Forager's AI pass through
its server in memory to the upstream model (Google Gemini) and are never
written to disk, database, or logs. What is kept is a usage ledger of token
counts per month, which is how your quota is metered.

**Recipe shares:** a share link is a public web page holding exactly the
recipe you shared, nothing else from your library. Links are unguessable,
you can revoke any of them at any time from your account, and pages reported
by several people are taken down automatically. Share links do not expire on
their own; they stay up until you revoke them. Sending a recipe by email
sends the recipient's address to the mail provider to deliver that one
message; the address is not added to any list. One honest wrinkle: when a
visitor who is not signed in reports a share page, the report record includes
their network address so repeat reports from the same person count once.
That is the only place Forager keeps a client address.

**Community recipes:** submitting a recipe to the community publishes its
title, ingredients, steps, and your attribution text for other kitchens to
browse. Your email is never shown; ratings and reports are tied to accounts
internally to prevent abuse.

**Cloud backups (Premium):** a backup you push to Forager is stored on
Forager's server encrypted in transit but not encrypted at rest; treat it
accordingly if your backup includes secrets. Only your account can list or
download it, and only the newest three are kept.

**Remote access through Forager:** your kitchen gets a public
`yourname.forager.pantryraider.app` address. Be aware of what that means:
the secure connection from your browser ends at Forager's server, which
relays traffic to your kitchen over an encrypted WireGuard link. Forager
does not log or inspect that traffic, and no access logging is configured,
but the traffic does pass through the server, so it is not end-to-end
encrypted between your browser and your kitchen. If that trade-off is not
right for you, use your own Cloudflare Tunnel or a VPN instead.

**Bot checks:** the signup page uses Cloudflare Turnstile, which means
Cloudflare sees the visit the way it does on any Turnstile-protected site.

**Email:** Forager sends email only for verification, password resets, and
recipe shares you initiate, delivered through a transactional mail provider.
There are no newsletters or marketing emails.

**Deleting your account:** you can delete your Forager account yourself,
from the bottom of your account page. Deletion asks you to confirm who you
are, cancels any paid subscription immediately, and removes your sign-in
details, sessions, two-factor material, passkeys, paired kitchens, cloud
backups (including the stored files), usage history, and your share links.
Recipes you published to the community remain, with your name replaced by
"a former member". If you prefer, or cannot sign in,
[support@pantryraider.app](mailto:support@pantryraider.app) can do it for
you.

**Cancelling a subscription:** cancel online any time from your account
page, in as few steps as subscribing took. Your plan stays active until the
end of the period you already paid for and simply does not renew.

**Recipe reports:** if someone reports a shared recipe without being signed
in, the report is identified only by a short one-way hash, never a stored
IP address, and report records are deleted after 90 days.

## Community shelf life data

Covered in full, in plain language, on
[Community shelf life and privacy](community-shelf-life.md): what a shared
point contains, what is never shared, and how the aggregated table is built
so no single kitchen's data is ever visible.

## What we never collect

- No analytics, telemetry, or usage tracking of any kind, in the app or in
  its documentation pages.
- No advertising, no tracking pixels, no third-party trackers.
- No sale or sharing of data with anyone, ever. There is nothing to sell:
  for a self-hosted install without Forager, we hold no data about you at
  all.
- No location data, beyond the town name you type in for the weather panel,
  which goes only to the weather service.

## Data retention on Forager

- AI images and prompts: never stored; they exist only for the duration of
  the request.
- Token usage: kept as monthly totals for quota metering and billing
  history.
- Recipe shares and community recipes: kept until you revoke or delete them.
- Cloud backups: the newest three per account; older ones are deleted when a
  new one arrives.
- Anonymous shelf-life observations: kept to build the aggregated table;
  they contain nothing that can identify you.
- Account records: kept while the account exists, removed when you delete
  the account (see deleting your account above).
- Housekeeping runs daily: expired sign-in tokens, verification and reset
  links, pairing codes, and stale sessions are purged automatically, and
  recipe-report records are deleted after 90 days. Tokens are stored hashed
  and single-use even before they are swept.

## Changes to this policy

Changes are recorded here, newest first.

- **2026-07-15:** self-serve account deletion, online cancellation, hashed
  report identifiers, and daily retention housekeeping shipped; the policy
  now describes them.
- **2026-07-15:** first version of this policy.
