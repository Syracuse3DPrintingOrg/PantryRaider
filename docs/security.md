# Security model

Pantry Raider runs inside your home and, optionally, talks to one small cloud
service you can ignore entirely. This page explains where the trust boundaries
are, what each moving part is allowed to do, and which trade-offs you are
accepting when you turn a feature on. It is written in the same spirit as
[What needs the internet](what-needs-internet.md): you should never have to
guess.

## The app itself

- Sign-in is required whenever a password is set; browsers get a session
  cookie, and headless clients (Home Assistant, scripts) use the API key from
  Settings, Security. Optional two-factor (TOTP) and a read-only viewer role
  are available for shared households.
- Requests from the device's own loopback address are trusted so the attached
  kiosk display and local jobs work; nothing off the device gets that pass.
- Fetches of addresses you type in (like importing a recipe from a webpage)
  refuse loopback and link-local targets outright, so a pasted URL can never
  be used to poke at services on the device itself.

## The host bridge (Pi appliances)

A Pi appliance runs a small root helper on `127.0.0.1:9299` that performs the
few jobs a container cannot: OTA updates, restores, reboots, display power.
Its boundaries:

- It listens only on loopback; it is never reachable from the network.
- Every request must carry a shared token that the bridge writes to a file
  only the app's service account can read. A process that cannot read that
  file gets `403` on every call, including other local users.
- Its surface is a short allow-list of specific actions, not a shell.

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
