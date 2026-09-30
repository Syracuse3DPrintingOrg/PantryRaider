# Remote access

Out of the box, Pantry Raider is reachable only on your own network. That is
the right default for a kitchen: nothing is exposed to the internet, and there
is nothing to secure beyond your own front door.

Remote access is for the times you want the pantry with you. Standing in the
cereal aisle wondering whether you already have two boxes, adding to the
shopping list on the drive home, checking a timer from the yard. Turning it on
gives your kitchen a web address you can open from anywhere, without opening a
port on your router.

Remote access is still **experimental**. It works, and it is in daily use, but
expect the occasional rough edge and do not make it the only way into your
kitchen. See [Feature maturity](maturity.md) for how the rest of the app is
rated.

## The two ways in

Open **Settings, Forager** and find the **Remote access** panel. The **Mode**
row offers three choices.

**Off** is the default. Nothing leaves your network.

**Forager** is the built-in option and the one this page is mostly about. Your
kitchen gets an address like `https://your-kitchen.forager.pantryraider.app`,
served over HTTPS, with no router configuration at all.

**Cloudflare Tunnel** is for people who already run their own Cloudflare
Tunnel and would rather keep everything under their own account. Paste your
tunnel token in the field below the radio. If you go this route, running
`cloudflared` yourself alongside the stack and pointing it at port 9284 is the
most dependable setup, and it works the same way any other self-hosted service
does behind Cloudflare.

A reverse proxy you run yourself (Caddy, nginx, Traefik, Pangolin) is a fourth
path that does not involve this panel at all. See the HTTPS section of
[Platforms and deployment](platforms.md) for that.

## What a Forager plan includes

Forager is the optional hosted companion to Pantry Raider. You never need it:
the app is complete without an account, and community recipes are free. What a
paid plan adds is remote access and a pool of AI tokens so you do not have to
bring your own provider key.

- **Forager Free.** An account, community recipes (browse, download, and share
  what you make), and a 30 day trial of everything below. One trial per
  install.
- **Cloud Basic.** Remote access plus a small monthly AI allowance. This is the
  one to pick if you already have your own Gemini, OpenAI, Anthropic, or Ollama
  setup and only want to get at your kitchen from outside.
- **Premium.** Remote access plus the full monthly AI allowance, which covers
  photo import, receipt scanning, barcode enrichment, and AI recipe help
  without ever adding a provider key. Premium also unlocks backups pushed to
  Forager's own storage.

Current prices are on the
[Forager pricing page](https://forager.pantryraider.app/pricing).

## Subscribing

1. In the app, open **Settings, Forager**. If you have no account yet, use
   **Create an account**; it opens the Forager signup page.
2. Sign up. Every new account starts with a 30 day trial, so you can try remote
   access before paying for anything.
3. Sign in from the same Forager pane to link this kitchen. The link is
   per-device: a satellite or a second install links separately.
4. When you are ready to keep it, open **Plan and billing** on your Forager
   account page and pick a plan. The same page has **Manage or cancel
   subscription** whenever you want to change your mind.

## Turning remote access on

Before the button will work, the app asks for two things, and both are worth
having anyway:

- **A password on this device.** Remote access without a password would put
  your pantry on the open web. Set one under Settings, Security.
- **A second factor.** Either two-factor authentication on this device or on
  your Forager account. Signing in from outside your house always asks for a
  second factor, so this is not optional.

Then:

1. Set **Mode** to **Forager**.
2. Type the name you want in the **Web address** field. The app checks it as
   you type and suggests a free one if the name is taken.
3. Press **Turn on remote access**.

The panel then shows your address with a copy button. Give it a minute on the
first run: the certificate is issued on demand, and the panel says whether the
device has connected yet.

To change the address later, turn remote access off and back on with the new
name. To stop entirely, press **Turn off remote access** or set Mode back to
Off.

## What actually happens

Your device makes an outbound WireGuard connection to Forager's server and
keeps it open. Requests to your address arrive at that server and travel back
down the tunnel to your kitchen. Nothing listens on your router, and nothing
about your network needs to change.

Two details worth knowing:

- **The connection is outbound only** and carries traffic for your kitchen
  alone. It is not a full VPN: nothing else on your network is routed through
  it, and Forager cannot reach anything but the app.
- **HTTPS terminates at Forager's server**, which then relays to your kitchen
  over the encrypted tunnel. That is how the certificate can be issued for you.
  [Privacy](privacy.md) covers what that means in full.

A Raspberry Pi appliance runs the tunnel on the device itself, outside the app.
A server runs it inside the Pantry Raider container, which is why a server
needs a little extra setup, below.

## Servers: one extra step

On a plain Docker server, the tunnel runs inside the Pantry Raider container,
and Linux only lets a container do that when Docker grants it three extra
things. A default install is not given them, because most kitchens never leave
the LAN and there is no reason to hand every install privileges it will not
use.

When you want remote access, add the override file that grants them:

```bash
docker compose -f docker-compose.yml -f docker-compose.forager.yml up -d
```

Keep your usual profiles on the same command, for example:

```bash
docker compose -f docker-compose.yml -f docker-compose.forager.yml \
  --profile with-grocy up -d
```

That is the whole change. Without the override, the Remote access panel says
plainly that it is unavailable on this host and everything else works exactly
as before. Your host also needs the kernel's WireGuard support, which every
current Linux has, Unraid included.

A Raspberry Pi appliance needs none of this.

## Going back a version

Worth saying here because it comes up when an update misbehaves: **updates only
move forward.** Settings and database changes are applied on the way up and are
not undone by installing an older build, so pinning an older tag on its own can
leave the app looking at data it does not understand.

A real rollback is two things together: pin the older version, and restore the
backup you took while you were on it. Take a backup before every update and
that stays a two-minute job.

## When something is not working

- **"Connect this device to Forager first."** Sign in on the Forager pane
  before turning remote access on.
- **"Remote access is part of a Forager plan."** Your trial has ended or the
  account has no plan. Add one from your Forager account page.
- **The panel says remote access is unavailable on this host.** On a server,
  add the override file above. On anything else, the host is missing WireGuard
  support.
- **The address resolves but the page never loads.** The panel reports whether
  the device has connected. If it is still waiting, check that the machine can
  reach the internet outbound.
- **Your address is taken.** Pick the suggestion the field offers, or type
  another name.

Home Assistant sensors and other headless clients should keep calling your
kitchen at its LAN address, not the public one. See
[Platforms and deployment](platforms.md).
