# Platforms and Deployment Modes

Pantry Raider can run as a server stack, as a self-contained Raspberry Pi
appliance, or as a thin Pi control surface for a stack that lives elsewhere. This
page describes the three deployment modes, how to host the server stack, the AI
providers you can plug in, and the Home Assistant integration.

For the physical hardware and peripherals, see [Hardware](hardware.md).

## Deployment modes

The mode is chosen in the setup wizard (or carried in via `config.env` on a
flashed device) and stored in the app's `settings.json`. The three modes are
defined in `service/app/config.py`:

- **server.** Pantry Raider runs on a general server (NAS, mini PC, VM, and so
  on), with the Grocy it brings along or one you already run. This is the only
  non-Pi mode. There are two ways to install it: the Docker install
  (`scripts/install.sh`, or the compose file by hand), which updates through
  the bundled Watchtower, and the on-device installer (`install.sh`) on a
  Debian or Ubuntu machine set aside for the kitchen, which updates from signed
  releases like a Pi appliance. [Get started](install.md) helps you choose.
- **pi_hosted.** Everything runs on one Raspberry Pi: Pantry Raider plus
  Grocy. The installer offers this as two clear choices: **Pi Host Kiosk**
  (the full stack plus this device's own touchscreen/kiosk, and a Stream Deck
  if one is attached) and **Pi Host Standalone** (the full stack, headless: no
  kiosk or Stream Deck, which you can still add later from the web UI). Both are
  the same `pi_hosted` stack; the difference is only whether this Pi also drives
  a screen. The first-boot provisioner installs a host bridge and a port 80 to
  9284 redirect so the device is reachable at `http://<hostname>.local/`.
- **pi_remote.** A thin satellite. The device drives a kiosk and/or Stream Deck
  pointed at an existing Pantry Raider server on the LAN. There is no local
  Docker, Grocy, or Mealie; the satellite runs the Pantry Raider UI on port 80
  (via a Python venv, or optionally Docker) and pulls its backend config from the
  main server. This is what makes a low-spec board like a Pi Zero useful.

### Switching a Pi Hosted appliance to satellite duty

A `pi_hosted` appliance can later become a satellite of a bigger server without
reflashing. In Settings under Advanced, "Run as a satellite" takes the
main server's URL and API key, pauses the local Grocy and Mealie containers
(their data stays on the SD card, nothing is deleted), and flips the device to
`pi_remote`: the kiosk and Stream Deck keep working, now backed by the main
server's inventory and settings.

The switch is reversible. On a switched device the Advanced settings section
gains "Switch back to full stack", which starts the paused containers again
and restores the backend settings the device had before the switch, inventory
data intact. A device that was flashed as a plain Pi Remote has no local stack
and is never offered the switch back.

For exactly which settings are editable, inherited from the server, or
device-local in each mode, see the [Settings visibility matrix](settings-matrix.md).

## Updates across the fleet

The "Install updates automatically" setting (on by default) and the update
channel work a little differently in each mode:

- A `server` installed with Docker is kept current by the bundled Watchtower
  container (the maintained `ghcr.io/nicholas-fedor/watchtower`), which checks
  once a day for
  a new Pantry Raider image and recreates the app container when one is
  published. It only manages Pantry Raider's own app container and leaves any
  Watchtower you already run alone. On a server, the channel and the switch in
  Settings cover the Pi Remotes connected to it and the update notice; they do
  not steer Watchtower. To stay on a release line, set `PANTRYRAIDER_TAG` (for
  example `0.21`) in `.env`. To stop automatic installs, run
  `docker compose stop watchtower`.
- A `server` set up with `install.sh` has no Watchtower. It updates the way a
  Pi appliance does, below.
- A `pi_hosted` appliance applies updates through the host-bridge over-the-air
  helper when the switch is on, and the in-app update control on its Settings
  page can check and apply on demand.
- A `pi_remote` satellite inherits the switch and the channel from its main
  server (they are among the `SATELLITE_PULL_FIELDS`), so a server and its
  satellites converge on the same version rather than drifting apart.

Server installs need Docker 24 or newer with Compose v2. A server set up
before the updater changed keeps its old compose file, and with it an updater
that does not work with Docker 29, until you replace the file; the one-time step
is in the README under Updating.

### Going back to an earlier version

A Raspberry Pi, or a server set up with `install.sh`, never moves back to an
older release on its own. To return to one on purpose, pin the version you
want. You need a root shell on the device (a keyboard and screen, or ssh), and
the device has to be on the Releases only update channel in Settings, Backups &
Updates, because the pin is ignored on Every change.

1. Take a backup in Settings, Backups & Updates first.
2. Write the version you want into the pin file and run the updater:

   ```bash
   echo 0.21.0 | sudo tee /etc/foodassistant/pinned-version
   sudo foodassistant-update
   ```

   The updater checks that release against the Pantry Raider release key like
   any other update, then installs it. It stays on that version while the
   file is there. Releases from 0.21.0 on carry that signature; a pin to an
   older release is refused and the device stays on the version it runs, which
   the update log and Settings, Backups & Updates explain.

3. Restore the backup you took while that version was running, in Settings,
   Backups & Updates. Settings and database changes only move forward, so an
   older release can find data from a newer one that it does not understand.

To follow new releases again, remove the pin and update:

```bash
sudo rm -f /etc/foodassistant/pinned-version
sudo foodassistant-update
```

A Docker Compose or Unraid server goes back by setting `PANTRYRAIDER_TAG` to
the older version, with the same backup restore afterwards; see the README
under Updating.

## Hosting the server stack

The `server` and `pi_hosted` modes run the stack with Docker 24 or newer and
Docker Compose v2. Optional backends are gated behind compose profiles, so you
only run what you need. A Pi appliance or an `install.sh` server always runs
its Grocy; the profiles below are the Docker install's (`docker-compose.prod.yml`).

Profiles:

- No profile: Pantry Raider on its own (plus the Watchtower updater on the
  Docker install), for a Grocy you already run elsewhere.
- `with-grocy`: adds the bundled Grocy inventory. The one-command installer
  turns this on for you.
- `with-mealie`: adds Mealie, for people who already keep recipes there.
  Recipes, the meal plan and the shopping list are built in without it.
- `with-ollama`: adds Ollama for fully local AI.
- `with-printing`: adds the print service for labels and documents.
- `with-beszel`: adds Beszel monitoring for the host and its containers.

Example enabling everything:

```bash
docker compose --profile with-grocy --profile with-mealie --profile with-ollama up -d
```

### Pinned backend versions and ports

The bundled backends are pinned to specific image tags (not `:latest`) so an
unattended pull cannot move you onto a breaking release.

| Service | Image | Tag | Port |
|---------|-------|-----|------|
| Pantry Raider | `ghcr.io/syracuse3dprintingorg/pantryraider` | `${PANTRYRAIDER_TAG}`, or the older `${FOODASSISTANT_TAG}` (default `latest`) | 9284 |
| Grocy | `lscr.io/linuxserver/grocy` | `4.7.1` | 9383 |
| Mealie | `ghcr.io/mealie-recipes/mealie` | `v3.28.0` | 9285 |
| Ollama | `ollama/ollama` | `0.40.4` | 11434 (this device only) |
| Watchtower (server only) | `ghcr.io/nicholas-fedor/watchtower` | `1.23.1` | none |
| Beszel hub and agent | `henrygd/beszel`, `henrygd/beszel-agent` | `0.21.0` (same tag for both) | 8090 |
| CUPS (server compose) | `olbat/cupsd` | pinned by digest | none (a private network and a shared socket) |

The bundled Ollama answers only on the device itself. Pantry Raider runs on the
same device and reaches it from there, so nothing else needs the port. To let other
computers on your network use it, including Pi Remotes that share the server's AI
settings, change its port line in the compose file
from `127.0.0.1:11434:11434` to `11434:11434`; Ollama has no password of its
own, so only do that on a network you trust.

The print service publishes no port. The app reaches it through a socket in
the shared `cups-run` volume (`CUPS_SERVER=/run/cups/cups.sock`), and the two
share a private network that nothing else joins. You add and manage printers
in Settings, Printing. A compose file from before 0.21 published the print
service's admin page on port 6631 and still works; Settings, Printing shows
**Update your print service** with the steps to move to the new file (see
[Printing](printing.md#update-your-print-service)).

The app's own port is published on IPv4 only (`0.0.0.0:9284:8000`). Docker
hands an IPv6 visitor to the app through its own network gateway, where it
would look like a device on your home network. If your compose file is older
and lists the port as `9284:8000`, change that line and run
`docker compose up -d`.

### Logs

The app writes its own activity to the container log and to the debug
download in Settings. It does not write a line for every request: a kitchen
screen refreshes every few seconds, and on a Raspberry Pi each of those lines
would be a write to the memory card. If you want per-request lines back while
you chase something, set `PR_ACCESS_LOG=1` on the container and restart it.
The container log itself is capped at three files of 10 MB, so it can never
fill a card on its own. That cap covers every bundled service (Grocy, Mealie,
Ollama, CUPS, Watchtower, Beszel) on new installs, and on servers that download
the current compose file. A Pi appliance that is already set up keeps its
current compose file, where only the Pantry Raider log is capped, until it is
reflashed.

To move a backend to a newer version, back up first, then bump the tag in your
compose file and recreate just that service.

### Grocy updated and inventory stopped loading

**Symptom.** The Inventory and Expiring pages show a banner starting "Grocy
is running but reported a problem with its own setup", quoting an "Invalid
setting in config.php" message about `AUTH_CLASS`. Refresh does not help.
Grocy's own web page shows the same message. `/health` reports `grocy:
error` with the cause in `grocy_detail`.

**Cause.** Grocy 4.7.0 moved its login handler into a new namespace. A
`config.php` written by an older Grocy still says
`Setting('AUTH_CLASS', 'Grocy\Middleware\DefaultAuthMiddleware');`, and once
the Grocy image updates (an unpinned `:latest`, or a bump of the pin), Grocy
refuses every request until that line changes. The stacks Pantry Raider ships
pin Grocy, so this mostly hits a Grocy you run separately.

**Fix on a server or Unraid install.** In Grocy's data folder (`/config/data`
inside the container; on Unraid, `/mnt/user/appdata/<your grocy>/data`), back
up `config.php`, then change the line to:

```php
Setting('AUTH_CLASS', 'Grocy\Middleware\Auth\DefaultAuthMiddleware');
```

If Grocy signs you in through LDAP or a reverse proxy, the line names
`LdapAuthMiddleware` or `ReverseProxyAuthMiddleware` instead: keep that name
and add `Auth\` in the same place. The banner's fix names the one your Grocy
uses.

Grocy picks the change up within a few seconds; press Refresh in Pantry
Raider. One-liner for a Grocy container named `grocy` with the standard
sign-in:

```bash
docker exec grocy sh -c 'cp -p /config/data/config.php /config/data/config.php.bak && sed -i "s/Grocy\\\\Middleware\\\\DefaultAuthMiddleware/Grocy\\\\Middleware\\\\Auth\\\\DefaultAuthMiddleware/" /config/data/config.php'
```

**Appliances repair themselves.** A Pi Hosted appliance checks the running
Grocy on every update and fixes the line (backup first), and the banner
offers a **Repair inventory connection** button that does the same on demand. The
compose stacks also mount a start-up script into the Grocy container
(`docker/grocy-init`) so a future Grocy update fixes itself when the
container starts. Both handle all three sign-in methods, and both put the
line back if you return to Grocy 4.6.

**An expected warning in Grocy's log.** When the Grocy container starts, its
log may open with a linuxserver warning that files in `/custom-cont-init.d`
are not owned by root. That is the start-up script above, and the warning is
expected: the script still runs, and Grocy works normally.

### Reverse proxy and URL caveat

You can put Pantry Raider behind a reverse proxy (for example Pangolin) to get a
public URL. One important caveat: headless clients must use the LAN URL, not the
public proxy URL. A request without a browser session (for example a Home
Assistant REST sensor) hitting the public URL gets an HTML redirect rather than
the JSON it expects. So Home Assistant REST sensors point at the LAN address such
as `http://192.168.1.50:9284`, while human-facing Lovelace buttons can use the
public URL.

### HTTPS

Pantry Raider serves plain HTTP on port 9284 and does not terminate TLS itself,
so secure access is added in front of it. Pick whichever fits your setup:

- **Reverse proxy (recommended).** Put a proxy such as Caddy, nginx, Traefik, or
  the bundled Pangolin tunnel in front of the app and let it handle TLS and
  certificates. Caddy is the simplest: a one-line site block proxying to
  `localhost:9284` gets an automatic Let's Encrypt certificate for a public
  hostname. Terminate TLS at the proxy and forward HTTP to Pantry Raider on the
  LAN. A proxy in front of the app needs a password on the app, and signing
  in through it needs two-factor as well (see below).
- **Tunnel.** The built-in remote-access tunnel (Settings, Forager)
  publishes the app over HTTPS without opening a port, which is the easiest way
  to reach it securely from outside the LAN.
- **Self-signed, LAN only.** For a closed network you can place any of the above
  proxies in front with a self-signed certificate; expect a browser warning
  unless you trust the certificate on each device.

Keep the LAN-URL caveat above in mind: Home Assistant REST sensors and other
headless clients should still call the LAN HTTP address directly, not the
HTTPS front end.

### Signing in through a proxy

Everything that reaches Pantry Raider through a reverse proxy or a tunnel is
treated as a visitor from outside your home network:

- A kitchen with no password does not answer through a proxy. Set a password
  before you put one in front of it. An install upgraded to 0.21 keeps
  answering for 14 days and shows a banner with the date it stops.
- Signing in from outside needs two-factor as well as the password. Turn on
  two-factor in Settings, Security from the plain home address first (for
  example `http://pr.local:9284`), then sign in through the proxy. If you can
  only reach the kitchen through the proxy, the sign-in page offers the setup
  code from the container log, which signs you in once and takes you straight
  to turning two-factor on.

This includes a reverse proxy on the same machine as Pantry Raider, such as
Caddy or nginx in front of a Pi or a server. Before 0.21 its requests skipped
sign-in; now they get the sign-in page like any other visitor from outside.

A proxy on the same machine must pass on the visitor's address and the name
they opened, or some of its requests can look like Pantry Raider's own
programs on that machine. Caddy, Traefik, Apache and cloudflared do that by
default. nginx needs two lines in its `location` block:

```nginx
location / {
    proxy_pass http://127.0.0.1:9284;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Add both lines whenever nginx runs on the same machine. Left on its defaults
(no `X-Forwarded-For`, and the name rewritten to `127.0.0.1`), nginx hides
where a visitor came from, and since nginx 1.30 what it passes on is exactly
what a program on the same machine sends. The kitchen recognizes a browser
behind it once the browser has opened a page, treats it as outside and shows an
item with these two lines in the Review inbox. Before that, the first page a
visitor opens and anything that is not a browser look like a program on this
machine:

- On a server without a kitchen screen, Stream Deck or sensor reader, a program
  on the machine gets no special trust, so the visitor is asked to sign in.
- On a Raspberry Pi whose own programs have not all signed in with their own
  keys yet, the visitor can pass as one of them.
- On a kitchen with no password, the visitor is treated as a device at home.

So set a password and add the two lines before nginx goes in front of the app.

A kitchen screen on the same device should open the app at
`http://localhost:9284`, which is where it signs itself in. One that goes
through the proxy stops at the sign-in page; run
`sudo foodassistant-host-harden kiosk-url-local` on the device to point it back
at `localhost`.

## AI providers

AI features are optional. The vision provider is selected in the setup wizard.
The supported providers (see `vision_provider` in `service/app/config.py`):

- **gemini** (Google). Cloud, API key. The default provider.
- **openai**. Cloud, API key.
- **anthropic**. Cloud, API key.
- **ollama**. Fully local inference, no API key. Reads from an Ollama instance
  (bundled via the `with-ollama` profile on port 11434, which answers only on
  the device itself unless you publish the port). Local vision models are
  heavy, so this is best on x86-64 with plenty of RAM rather than a small SBC.

If you do not want local inference, configure any one of the cloud providers and
any supported board is fine.

## Home Assistant integration

There are two ways to use Pantry Raider with Home Assistant:

- **Add-on (HA OS / Supervised).** Pantry Raider installs as an add-on and lives
  in the HA sidebar with no separate login; HA authenticates the UI through
  Ingress.
- **Standalone.** A standalone Pantry Raider instance exposes REST endpoints that
  HA can consume. The `homeassistant/` directory ships REST sensors (expiring
  summary, inventory dashboard, pending scan count, and the Mealie shopping and
  meal-plan summaries), a `rest_command` for posting scans, automations, and a
  Lovelace dashboard.

A common HA pattern is the headless barcode scanner: a USB or Bluetooth HID
scanner is captured with the `keyboard_remote` integration, an automation buffers
the typed digits, and a `rest_command` posts the completed barcode to
`/pending/scan` on the Pantry Raider LAN URL. `keyboard_remote` only works on
Home Assistant OS / Supervised. See
[homeassistant/barcode-scanner.md](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/homeassistant/barcode-scanner.md) for the
full walkthrough, and remember the LAN-URL caveat above for all REST sensors and
commands.
