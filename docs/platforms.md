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
  on). It connects to a separately running Grocy. This is the only non-Pi
  mode.
- **pi_hosted.** Everything runs on one Raspberry Pi: Pantry Raider plus
  Grocy, with or without an attached display. The kiosk
  auto-enables when a display is present, and a display can be added later. The
  first-boot provisioner also installs a host bridge and a port 80 to 9284
  redirect so the device is reachable at `http://<hostname>.local/`.
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

A single "Install updates automatically" setting (on by default) drives updates
for the whole deployment:

- A `server` install applies updates through the bundled Watchtower container,
  which checks for a new Pantry Raider image and recreates the service container
  when one is published.
- A `pi_hosted` appliance applies updates through the host-bridge over-the-air
  helper, and the in-app update control on its Settings page can check and apply
  on demand.
- A `pi_remote` satellite inherits the flag from its main server (it is one of
  the `SATELLITE_PULL_FIELDS`), so a server and its satellites converge on the
  same version rather than drifting apart.

## Hosting the server stack

The `server` and `pi_hosted` modes run the stack with Docker and Docker Compose
v2. Optional backends are gated behind compose profiles, so you only run what you
need.

Profiles:

- (default, no profile or `with-grocy`): the Pantry Raider service plus Grocy.
- `with-mealie`: adds Mealie for recipes, meal plan, and shopping list.
- `with-ollama`: adds Ollama for fully local AI.

Example enabling everything:

```bash
docker compose --profile with-grocy --profile with-mealie --profile with-ollama up -d
```

### Pinned backend versions and ports

The bundled backends are pinned to specific image tags (not `:latest`) so an
unattended pull cannot move you onto a breaking release.

| Service | Image | Tag | Port |
|---------|-------|-----|------|
| Pantry Raider | `ghcr.io/syracuse3dprintingorg/pantryraider` | `${FOODASSISTANT_TAG}` (default `latest`) | 9284 |
| Grocy | `lscr.io/linuxserver/grocy` | `4.6.0` | 9383 |
| Mealie | `ghcr.io/mealie-recipes/mealie` | `v3.19.2` | 9285 |
| Ollama | `ollama/ollama` | `0.30.8` | 11434 |

To move a backend to a newer version, back up first, then bump the tag in your
compose file and recreate just that service.

### Reverse proxy and URL caveat

You can put Pantry Raider behind a reverse proxy (for example Pangolin) to get a
public URL. One important caveat: headless clients must use the LAN URL, not the
public proxy URL. A request without a browser session (for example a Home
Assistant REST sensor) hitting the public URL gets an HTML redirect rather than
the JSON it expects. So Home Assistant REST sensors point at the LAN address such
as `http://192.168.1.170:9284`, while human-facing Lovelace buttons can use the
public URL.

### HTTPS

Pantry Raider serves plain HTTP on port 9284 and does not terminate TLS itself,
so secure access is added in front of it. Pick whichever fits your setup:

- **Reverse proxy (recommended).** Put a proxy such as Caddy, nginx, Traefik, or
  the bundled Pangolin tunnel in front of the app and let it handle TLS and
  certificates. Caddy is the simplest: a one-line site block proxying to
  `localhost:9284` gets an automatic Let's Encrypt certificate for a public
  hostname. Terminate TLS at the proxy and forward HTTP to Pantry Raider on the
  LAN.
- **Tunnel.** The built-in remote-access tunnel (Settings, Connections)
  publishes the app over HTTPS without opening a port, which is the easiest way
  to reach it securely from outside the LAN.
- **Self-signed, LAN only.** For a closed network you can place any of the above
  proxies in front with a self-signed certificate; expect a browser warning
  unless you trust the certificate on each device.

Keep the LAN-URL caveat above in mind: Home Assistant REST sensors and other
headless clients should still call the LAN HTTP address directly, not the
HTTPS front end.

## AI providers

AI features are optional. The vision provider is selected in the setup wizard.
The supported providers (see `vision_provider` in `service/app/config.py`):

- **gemini** (Google). Cloud, API key. The default provider.
- **openai**. Cloud, API key.
- **anthropic**. Cloud, API key.
- **ollama**. Fully local inference, no API key. Reads from an Ollama instance
  (bundled via the `with-ollama` profile on port 11434). Local vision models are
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
