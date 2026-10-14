# Security model

Pantry Raider runs inside your home and, optionally, talks to one small cloud
service you can ignore entirely. This page explains where the trust boundaries
are, what each moving part is allowed to do, and which trade-offs you are
accepting when you turn a feature on. It is written in the same spirit as
[What needs the internet](what-needs-internet.md): you should never have to
guess. For what happens to your data specifically, see the
[privacy policy](privacy.md).

## The app itself

- Sign-in is required whenever a password is set; browsers get a session
  cookie, and devices and scripts use a key from Settings, Security (see
  [Keys and what they can reach](#keys-and-what-they-can-reach)). Optional
  two-factor (TOTP) and a read-only viewer role are available for shared
  households.
- Programs on the device itself sign in like everything else. The kitchen
  screen gets a one-time sign-in ticket each time it starts, and the Stream
  Deck and the sensor reader each have a key of their own. Until 0.21 anything
  running on the device was let in without a key; that trust ends by itself
  once the screen, the Stream Deck and the sensor reader have all signed in on
  their own and a week passes without anything else needing it. Settings,
  Security, This device's own programs shows where it stands, can end it now,
  or can let programs on the device in without a key for another 7 days. A
  program that reaches the app by the device's own name (for example
  `http://pr:9284`) counts as the device itself, like `localhost`.
- A reverse proxy on the same machine does not count as the device. Caddy,
  nginx and the like sign in like any visitor from outside: they get the
  sign-in page, and a password sign-in through them needs two-factor. Have the
  proxy pass the visitor's address and the name they opened (Host and
  X-Forwarded-For). Caddy, Traefik, Apache and cloudflared do that by default;
  nginx needs `proxy_set_header Host $host;` and
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. Without
  them nginx hides where a visitor came from: the kitchen recognizes a browser
  behind it once the browser has opened a page, treats it as outside and puts
  those two lines in an inbox item, but the first page a visitor opens, and
  anything that is not a browser, can look like a program on this machine. A kitchen screen whose address
  goes through a proxy signs in like a visitor too; run
  `sudo foodassistant-host-harden kiosk-url-local` to point it back at
  `localhost`.
- Signing in from outside your home network always needs a second factor, not
  just when you use the built-in Forager tunnel. If you put your own reverse
  proxy or tunnel (Cloudflare, Pangolin, and the like) in front of the app,
  a request that arrives through it is treated as coming from the internet, so
  a password alone is never enough from outside. A device on your home network
  signs in with the password alone, and so does one on a range listed under
  Home networks or one that reaches the kitchen over Tailscale (whose
  `100.64.0.0/10` addresses are not the open internet). When a kitchen has a
  password but two-factor is not on yet, the setup code from its log signs you
  in once from outside and takes you straight to turning two-factor on. One
  setup code allows 10 wrong passwords in all; after that, opening the setup
  code page again prints a new code to the log. The sign-in,
  PIN, setup code and pairing screens are also rate limited: a burst of wrong
  guesses is locked out for a few minutes. That throttle is per app process
  and resets on restart, so it raises the cost of a brute force but is not a
  replacement for your proxy's own rate limiting; keep the second factor on
  for anything reachable from outside.
- Two exceptions answer the local network without a password, because the
  thing asking cannot carry one: a new device asking to pair (you still
  approve it on the screen), and the Bandit Cub update check. The firmware
  files themselves need a sign-in on a kitchen with a password; they serve
  only the browser flasher on the Bandit Cubs page: a board name, a version,
  and the same image the project's public GitHub release serves; nothing
  about your kitchen. A Cub flashed today does not ask your server for
  anything; it listens to your kitchen's Bluetooth broadcast. A Cub still
  running a build with the older network transport needs its own key for
  everything it shows. "Local network" here means a request that reached the
  app directly on your LAN: a request that came in through a reverse proxy or
  a tunnel does not qualify, so neither pairing nor the update check is
  exposed to the internet when the app is published through one.
- Fetches of an address you supply are screened by one guard that resolves the
  name and inspects the address the connection will actually use, on the first
  request and on every redirect. Importing a recipe from a webpage refuses any
  address that is not a public one on the internet, so a pasted link can never
  reach the device itself or anything on your LAN. Where a feature genuinely
  needs the local network (previewing a camera, connecting Home Assistant),
  private LAN addresses are allowed but the device's own loopback address, the
  cloud metadata address, and reserved ranges are still refused. Encoded forms
  of an address (decimal, hex, an IPv6-wrapped IPv4) and names that resolve to
  a blocked address are all caught, because the check runs on the resolved
  address the socket connects to, not on the text of the URL.

## A kitchen with no password

A password is required by default. A kitchen without one (the Home Assistant
add-on, or an owner who turned it off) answers only your home network:

- Devices on your home network and the Pantry Raider add-on in Home
  Assistant's sidebar keep working. A request through a reverse proxy or a
  tunnel, or one that uses a name the kitchen does not know, is refused with a
  page that explains the ways back in. A request that another site's page
  tries to send is refused too.
- "Home network" means the private address ranges the device is connected
  to. If yours uses other addresses (a VPN such as Tailscale, which uses
  `100.64.0.0/10`), add them under Home networks in Settings, Security, or set
  `LAN_CIDRS` in the container settings as a comma separated list. A name the
  kitchen should answer at home besides its own (for example
  `pantry.home.example`) goes under Other names for this kitchen, or in
  `HOME_HOST_NAMES`, also comma separated.
- An install upgraded to 0.21, or a fresh device restored from a backup, keeps
  answering through a proxy or an unknown name for 14 days. During that time
  every page shows a banner with the date it stops, an inbox item names the
  address it was reached at, and the Home Assistant integration shows a
  Repairs notice. A new install refuses those requests from the start.
- To keep access from outside, turn on two-factor in Settings, Security, then
  set a password. Signing in from outside needs both. If you are already
  turned away, the ways back in are, in order: the setup code (from outside,
  the refusal page asks for it; it is in the container log on the line that
  begins `Pantry Raider setup code`, works once and lasts 24 hours, and leads
  to a password and then to two-factor); opening the kitchen at its home
  address; or setting `AUTH_PASSWORD` in the container settings, restarting,
  and signing in once from outside with the setup code and that password.
- Devices at home that use the kitchen without a key, such as Home Assistant
  set up without one, older Bandit Cubs and ESP buttons, stop working once a
  password is set until each has a key. Settings, Security names the ones it
  has seen, and an inbox item lists them when the password is set; approve
  their pairing or create a key for each. Require a key, in Settings,
  Security, makes a kind of device need its own key even before a password is
  set; a new install starts with all four kinds turned on.
- Every page of a kitchen with no password shows a slim notice that anyone on
  your home network can use and change it.
- When Docker publishes the port on IPv6, a visitor over IPv6 reaches the app
  through Docker's own network gateway and cannot be told apart from a
  device at home. Settings, Security says so when it sees that. The compose
  files publish the port as `0.0.0.0:9284:8000` (IPv4 only) for this reason;
  change an older compose file's port line to match.

## Keys and what they can reach

Every device and script that reaches the app without a person signed in uses
a key. Settings, Security lists them all with what each can do, when it was
last used and where from, and lets you create, limit or revoke them. A new
key is shown once.

| What it can do (as Settings shows it) | Reaches |
|---|---|
| A second screen (satellite) | Every kitchen page and action, including Grocy and Mealie through the main server, but not Settings or admin pages |
| The Home Assistant integration | The integration's own data and controls, and notifications from Home Assistant |
| Home Assistant sensors, buttons and ESPHome panels | Reading kitchen data, timers, the scan command, notifications, ESP buttons and screens |
| A Bandit Cub or other small display | The Cub summary, update check and firmware, timers, ESP buttons and screens |
| Everything, including Settings | Settings, admin pages, backups and updates as well (an admin key) |
| Everything except Settings (an older key) | Keys from before 0.21 |

- An admin key can only be created on a kitchen that has a password, needs
  that password, and expires after 90 days. No other key can change settings.
- On a kitchen with no password, any device at home can create a key without
  a password.
- Devices that pair (Settings, Security, Device pairing) get a key limited to
  their own kind, after you approve the matching code on the kitchen screen or
  in Settings, Bandit Remotes.
- A paired device that has still not used its key a day after you approved
  it gets an item in the Review inbox with Remove key. The key is never
  removed without you, so a device that takes a while to finish setting up
  keeps it, and the item goes away once the device connects.
- An older full-access key is retired 90 days after its last use, once every
  device that used it has a key of its own. A key used by a Bandit or a
  Bandit Cub is never removed without asking you, and neither is `API_KEY`
  from the container settings. The switch is Retire old keys once nothing
  uses them.
- While Keep new keys working if this kitchen goes back to an older version
  is on (the default), each new key is also copied where older versions look
  for keys, so going back a version keeps every device connected.

## The host bridge (Pi appliances and install.sh servers)

A Pi appliance, and a server set up with `install.sh`, runs a small root helper
on `127.0.0.1:9299` that performs the few jobs a container cannot: OTA updates,
restores, reboots, display power. Its boundaries:

- It listens only on loopback; it is never reachable from the network.
- Every request must carry a shared token that the bridge writes to a file
  only the app's service account can read. A process that cannot read that
  file gets `403` on every call, including other local users.
- Its surface is a short allow-list of specific actions, not a shell.
- One route answers without the token: `GET /identity`, which returns a
  keyed hash of a one-time number the caller picked. The app uses it to check
  that the helper answering on that port really holds the shared token before
  it hands the helper anything, such as an update request on a server.

### Bridge token and restore hardening

- The token file is created root-only and then opened just far enough for the
  app's own service account to read it; no other local account can. If the
  bridge cannot work out which account that is, the file stays root-only.
- Devices from before the token handshake get a short compatibility window so
  their first update still goes through, but that window is now bounded: it
  closes after 24 hours or as soon as the device proves it can send the
  token, whichever comes first, and it never reopens. A brand-new device
  never has the window at all. If the bridge cannot set up its own token, it
  refuses tokenless requests instead of allowing them.
- The token is shared with the app on purpose, so the app can ask for
  updates, restores, and reboots. That means the app's service account and
  the bridge are one trust domain: anything that fully compromises the app
  on the device can reach those same actions. The Stream Deck controller and
  the Bluetooth thermometer reader run as that same account, so they sit
  inside the same trust domain too. The bridge limits the damage
  by offering only its fixed list of actions, and restores are confined as
  described next.
- A restore snapshot is checked before anything is touched: every file in it
  must live inside the app's own data folders. Entries that try to escape
  (absolute paths, `..`, links pointing elsewhere, device files) are
  rejected, and the archive is unpacked in a staging area from which only
  the expected data folders are moved into place. A snapshot can never
  replace the software's own configuration, no matter where it came from.

## The recovery hotspot (Pi appliances)

When a Pi appliance cannot reach any network, it raises its own Wi-Fi
hotspot (network name `FoodAssistant` unless you rename it) so you can
connect and fix the Wi-Fi settings. The device watches its connection the
whole time it is running, not just at startup: if your network disappears
for a few minutes (a dead router, a changed Wi-Fi password, a move to a new
house), the hotspot comes on the air on its own, and it turns itself back
off once the device is connected again. That hotspot is protected the same
way your home network is:

- The hotspot password is unique to your device, generated when the device
  first sets itself up. There is no shared factory password, so nobody can
  look it up and join your appliance while it is waiting to be configured.
- The hotspot uses WPA2 with AES only; the older TKIP cipher is not offered.
- You can rename the hotspot and set your own password any time in Settings,
  Network, under Recovery hotspot. The card shows the current name and
  password (behind a click-to-reveal) and has a one-click generator for a
  fresh random password. Names are 1 to 32 characters, passwords 8 to 63.
- Your device's hotspot details live in two places you can always reach:
  the file `pantry-raider-hotspot.txt` on the SD card's boot partition (pop
  the card into any computer, no special tools; it lists both the name and
  the password, and is kept current when you change them), and on the device
  itself, root-only, at `/etc/foodassistant/ap-passphrase` (with the chosen
  name at `/etc/foodassistant/ap-ssid`). The first-boot log notes where to
  find them. The boot-partition copy makes recovery possible without a
  screen, and the file says plainly that anyone who reads the card can join
  the hotspot. To stop keeping it, turn off Keep a copy on the SD card in
  Settings, Network (the copy is removed), or set `AP_BOOT_COPY=false` in
  `foodassistant.config.env` before the first boot. Changing the switch never
  restarts the hotspot.
- Prefer to pick your own before the first boot? Set `AP_PASSPHRASE` (8 to
  63 characters) and, if you like, `AP_SSID` in `foodassistant.config.env`
  on the boot partition. If a value is not usable the device falls back to a
  generated password and the default name rather than leaving you with no
  recovery hotspot. Once the device has saved the password, it removes it
  from `foodassistant.config.env`.
- Devices set up before per-device passwords existed keep their old hotspot
  password until they are re-provisioned or reflashed, but can set a new one
  from Settings at any time.

## Automatic updates

- A **server** install uses Watchtower (the maintained
  `ghcr.io/nicholas-fedor/watchtower`, which works with Docker 24 or newer),
  and it needs the Docker socket to swap containers. Docker-socket access is
  root-equivalent on that host: that is the standard trade-off of every
  auto-update container. If you would rather not grant it, remove the
  Watchtower service from the compose file and update with
  `docker compose pull && docker compose up -d` yourself; nothing else
  depends on it.
- It is scoped to Pantry Raider: it only updates the Pantry Raider app
  container, and it leaves any Watchtower you already run on the same host,
  and the containers that one looks after, alone.
- Watchtower runs on a private network shared only with Pantry Raider, so its
  update API (and the Docker socket behind it) is not reachable from Grocy,
  Mealie, Ollama, CUPS, or Beszel. If you set a value for
  `WATCHTOWER_HTTP_API_TOKEN` in your `.env`, both containers pick it up.
- `latest` moves only when a release is published and signed with the
  Pantry Raider release key, so a server following it installs only signed
  releases.
- A **Pi appliance** and a server set up with `install.sh` update through the
  host bridge instead (no Docker socket exposure inside the app container),
  and a **satellite** follows the main server's update setting. Their update
  helper installs only a release tag (or, on the Every change channel, a
  commit) signed with the release key, checks the download before running any
  of it, and never moves to a release older than one the device already ran:
  it holds and says why in Settings, Backups & Updates, which also shows the
  installed release and the key it was checked against. The only way past
  the check is `sudo foodassistant-update --allow-unsigned-once`, from a root
  shell on the device. See [Verifying a release](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/SECURITY.md#verifying-a-release)
  to check a release yourself.
- The app checks that the helper it talks to on the device is really its own
  before handing it anything.
- Updates only move forward. Settings and database changes are applied on the
  way up and are not undone by installing an older build, so going back a
  version means restoring the backup you took on that version as well as
  pinning the tag. Take a backup before each update and you always have one.

## Satellites and the fleet

A satellite (a Bandit: a thin kiosk or Stream Deck device) pulls its settings
from your main server over your local network: the inventory address, the
theme, the Stream Deck layout, and so on.

- Once a Bandit and its main server both run 0.21, the Bandit keeps no copy of
  your AI, recipe or Home Assistant keys or camera logins. The main server
  runs AI and recipe lookups on its behalf (and counts the AI use against its
  own budget), passes Home Assistant calls along for only the entities and
  actions your keys, cameras and sensors use, and shows cameras that need a
  login through itself. A Bandit with its own Home Assistant thermometers or
  Bluetooth probe targets keeps its Home Assistant token. Grocy and Mealie
  were already reached through the main server, which adds its own keys and
  passes on only the requests Pantry Raider itself makes.
- Bandits older than 0.21 still need those keys on the device. Share service
  keys with older satellites, in Settings, Security, decides: Auto (the
  default) keeps sharing until every Bandit seen in the last 30 days is up to
  date, Always keeps sharing, and Never stops at once. Once no Bandit holds
  them any more, one inbox item suggests replacing the AI and recipe keys,
  since older Bandit backups can still hold copies.
- Once every device that uses a Bandit's key runs 0.21 or later, that key
  stops receiving those keys, even if the Bandit later goes back to an older
  version. If you put a Bandit back on an older version on purpose, its key
  shows Service keys held back in Settings, Security, and Let this key
  receive service keys again shares them with it until it is updated.
- The server proves it is the real server before a satellite will accept any
  setting that matters from it. Each pull carries a one-time number the
  satellite picked, and the server signs its answer with the shared key the
  two already agreed on. A reply without a valid signature can change only
  look-and-feel settings (the theme, the clock, the weather location), and a
  Bandit's Stream Deck profiles, alarms and commands count only when they
  match what the server signed. A device on your network that answers in the
  server's place cannot push a poisoned inventory address or new commands.
- Honest limit: the satellite still presents its key to whatever answers at
  the server's address, so an attacker who is actively positioned to read that
  request on your LAN can learn the key. The signing stops a stray or
  impersonating device from feeding a satellite bad config; it does not by
  itself defend the key against someone already able to intercept your local
  traffic. For a fleet that spans networks, run that channel over a trusted
  link (a VPN or a private network) rather than an open shared LAN. A future
  release will move the fleet channel to TLS.

## Home Assistant

- The Home Assistant integration pairs: you approve its code on the kitchen
  screen and it gets a key limited to what the integration needs.
- It then creates a Home Assistant user just for Pantry Raider and hands
  Pantry Raider that user's token, which it replaces once a year. The user is
  read-only unless one of your keys controls a device, and it is never an
  administrator. The integration removes only
  an owner token it created itself, and only once every Pantry Raider install
  in that Home Assistant uses a limited account.
- Keys that unlock, open or disarm something (locks, alarms, garage doors,
  covers) or call an action Pantry Raider does not know run only after you
  allow them on that key in Settings, Start Page. Bluetooth shelf buttons
  never run them, because anyone within radio range can fake a press.

## Printing on the LAN

On a Docker server, the built-in print service publishes no port. The app
reaches it through a shared socket, and it shares printers only inside a
private network that it and the app alone join; its admin pages answer only
inside that network, and the print image's built-in account is locked. Compose
files from before 0.21 published an admin port; Settings, Printing shows
**Update your print service** with the fix when that applies (see
[Printing](printing.md#update-your-print-service)).

On a Raspberry Pi or an `install.sh` server, printer administration is open
only to the device itself. A device that already shared its printers on the
local network (port 631, restricted to the local subnet) keeps sharing them so
your other devices can still print to them. A device set up from 0.21 prints
for itself only. Nothing is exposed to the internet.

## Forager and recipe share links

Forager is optional; the app works fully without it. When you do link it:

- Your kitchen authenticates with a per-install token; tokens are stored
  hashed on the server.
- A share link contains a long random token that cannot be guessed or
  enumerated. Only the recipe you shared travels to the cloud; nothing else
  from your database goes with it.
- You can revoke any link at any time from your Forager account, and pages
  that get reported by multiple people are taken down automatically.
- Creating shares and sending share emails are rate-limited per account and
  per address, and the share pages carry standard browser protections
  (content security policy, no framing, strict referrer policy). Recipe
  content is always HTML-escaped, and image links must point at a plain
  public web host: internal addresses and IP literals are refused.
- Sending a recipe to a person never reveals whether they have a Forager
  account.
- Remote access on a server runs a WireGuard tunnel inside the Pantry Raider
  container, which needs extra privileges from Docker. A default install is not
  given them, so an install that stays on the LAN never carries privileges it
  has no use for. Turn them on only when you want remote access, by adding
  `docker-compose.forager.yml` to your compose command; see
  [Remote access](remote-access.md). A Pi appliance never needs this, because
  it runs the tunnel on the host instead.

## Reporting a problem

If you find a security issue, open a GitHub security advisory on the
repository (Security tab, "Report a vulnerability") rather than a public
issue.
