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
  cookie, and headless clients (Home Assistant, scripts) use the API key from
  Settings, Security. Optional two-factor (TOTP) and a read-only viewer role
  are available for shared households.
- Requests from the device's own loopback address are trusted so the attached
  kiosk display and local jobs work; nothing off the device gets that pass.
- Signing in from outside your home network always needs a second factor, not
  just when you use the built-in Forager tunnel. If you put your own reverse
  proxy or tunnel (Cloudflare, Pangolin, and the like) in front of the app,
  a request that arrives through it is treated as coming from the internet, so
  a password alone is never enough from outside. The sign-in, PIN, and pairing
  screens are also rate limited: a burst of wrong guesses is locked out for a
  few minutes. That throttle is per app process and resets on restart, so it
  raises the cost of a brute force but is not a replacement for your proxy's
  own rate limiting; keep the second factor on for anything reachable from
  outside.
- Two exceptions answer the local network without a password, because the
  devices asking cannot carry one: a new Bandit or Cub asking to pair (you
  still approve it on the screen), and a Bandit Cub fetching its own
  firmware. The firmware endpoints hand out a board name, a version, and the
  same image the project's public GitHub release serves; nothing about your
  kitchen. Everything a Cub actually shows still needs its own key. "Local
  network" here means a request that reached the app directly on your LAN: a
  request that came in through a reverse proxy or a tunnel does not qualify,
  so neither pairing nor the firmware endpoints are exposed to the internet
  when the app is published through one. (This closed a gap where a proxied
  request looked local because the proxy's own address is a private one.)
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

## The host bridge (Pi appliances)

A Pi appliance runs a small root helper on `127.0.0.1:9299` that performs the
few jobs a container cannot: OTA updates, restores, reboots, display power.
Its boundaries:

- It listens only on loopback; it is never reachable from the network.
- Every request must carry a shared token that the bridge writes to a file
  only the app's service account can read. A process that cannot read that
  file gets `403` on every call, including other local users.
- Its surface is a short allow-list of specific actions, not a shell.

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
  on the device can reach those same actions. The bridge limits the damage
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
hotspot (network name `FoodAssistant`) so you can connect and fix the Wi-Fi
settings. That hotspot is protected the same way your home network is:

- The hotspot password is unique to your device, generated when the device
  first sets itself up. There is no shared factory password, so nobody can
  look it up and join your appliance while it is waiting to be configured.
- The hotspot uses WPA2 with AES only; the older TKIP cipher is not offered.
- Your device's hotspot password lives in two places you can always reach:
  the file `pantry-raider-hotspot.txt` on the SD card's boot partition (pop
  the card into any computer, no special tools), and on the device itself,
  root-only, at `/etc/foodassistant/ap-passphrase`. The first-boot log notes
  where to find it. The boot-partition copy is a deliberate trade-off for
  headless devices: anyone holding the SD card already controls the device,
  so the copy gives up nothing while making recovery possible without a
  screen.
- Prefer to pick your own? Set `AP_PASSPHRASE` in `foodassistant.config.env`
  on the boot partition before first boot (8 to 63 characters). If the value
  is not a valid length the device falls back to a generated password rather
  than leaving you with no recovery hotspot.
- Devices set up before this change keep their old hotspot password until
  they are re-provisioned or reflashed.

## Automatic updates

- A **server** install uses Watchtower, which needs the Docker socket to swap
  containers. Docker-socket access is root-equivalent on that host: that is
  the standard trade-off of every auto-update container. If you would rather
  not grant it, remove the Watchtower service from the compose file and update
  with `docker compose pull && docker compose up -d` yourself; nothing else
  depends on it.
- A **Pi appliance** updates through the host bridge instead (no Docker
  socket exposure inside the app container), and a **satellite** follows the
  main server's update setting.

## Satellites and the fleet

A satellite (a thin kiosk or Stream Deck device) holds no backend settings of
its own. It pulls them from your main server: the inventory address, the
recipe and AI keys, the Home Assistant token, and so on. That pull runs over
your local network.

- The server proves it is the real server before a satellite will accept any
  credential or backend address from it. Each pull carries a one-time number
  the satellite picked, and the server signs its answer with the shared key
  the two already agreed on. A device on your network that answers in the
  server's place but does not hold that key cannot push a poisoned inventory
  address or a swapped-out token: the satellite refuses the credential-bearing
  fields and keeps the settings it already had. Harmless preferences (the
  theme, the weather location) can still refresh, and a tampered signature is
  refused outright.
- Honest limit: the satellite still presents its key to whatever answers at
  the server's address, so an attacker who is actively positioned to read that
  request on your LAN can learn the key. The signing stops a stray or
  impersonating device from feeding a satellite bad config; it does not by
  itself defend the key against someone already able to intercept your local
  traffic. For a fleet that spans networks, run that channel over a trusted
  link (a VPN or a private network) rather than an open shared LAN. A future
  release will move the fleet channel to TLS.

## Printing on the LAN

When you set up printing on a device, its CUPS service is reachable on the
local network (port 631) so your other Pantry Raider devices and computers can
print to the same label printer. Access is restricted to the local subnet
(`@LOCAL`); nothing is exposed to the internet. If a device should not share
its printer, leave printing off on that device.

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

## Reporting a problem

If you find a security issue, open a GitHub security advisory on the
repository (Security tab, "Report a vulnerability") rather than a public
issue.
