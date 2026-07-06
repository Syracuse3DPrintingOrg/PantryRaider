# Forager tunnel: VPS setup

The remote-access tunnel lets a kitchen reach itself from anywhere without
opening a port on the home router. Each kitchen dials out to this VPS as a
WireGuard peer, and Caddy reverse-proxies its own subdomain to the kitchen
over that tunnel. This directory holds the VPS side: the tunnel agent, its
systemd unit, and these setup notes.

## What runs on the VPS

- **WireGuard** (`wg0`, UDP 51820): the tunnel server. Each kitchen is a peer
  with a stable /32 from `10.99.0.0/16`; the server itself is `10.99.0.1`.
- **Caddy**: terminates TLS. It serves the apex (portal + API) and, per
  kitchen, a subdomain issued on demand.
- **forager-tunnel-agent**: a small root helper on `127.0.0.1:9300`. The
  cloud app calls it to add or remove a WireGuard peer and rewrite Caddy's
  per-kitchen routes. The app never touches `wg` or Caddy directly.

## 1. Install WireGuard and generate the server keys

```bash
sudo apt-get update && sudo apt-get install -y wireguard

umask 077
wg genkey | sudo tee /etc/wireguard/server_private.key | wg pubkey | sudo tee /etc/wireguard/server_public.key
```

Create `/etc/wireguard/wg0.conf`:

```ini
[Interface]
Address = 10.99.0.1/16
ListenPort = 51820
PrivateKey = <contents of /etc/wireguard/server_private.key>
# No [Peer] blocks here: the agent adds and removes peers at runtime with
# `wg set` and persists them with `wg-quick save`.
```

Enable and start it:

```bash
sudo systemctl enable --now wg-quick@wg0
```

Open UDP 51820 in the VPS firewall. The kitchen dials out, so no other
inbound ports are needed for the tunnel itself (443 stays open for Caddy).

Put the **server public key** (`/etc/wireguard/server_public.key`) into the
cloud app's `.env` as `CLOUD_TUNNEL_SERVER_PUBLIC_KEY`; the app pins each
kitchen's peer to it.

## 2. The shared agent token

The agent and the cloud app authenticate to each other with one shared
secret. Generate it once and place it where both can read it:

```bash
sudo mkdir -p /etc/forager
openssl rand -hex 32 | sudo tee /etc/forager/tunnel-token
sudo chmod 600 /etc/forager/tunnel-token
```

Set the same value in the cloud app's `.env` as `CLOUD_TUNNEL_AGENT_TOKEN`.
The agent refuses to start if the token file is missing or empty.

## 3. Install the agent

```bash
sudo install -m 0755 forager-tunnel-agent /usr/local/bin/forager-tunnel-agent
sudo install -m 0644 forager-tunnel-agent.service /etc/systemd/system/forager-tunnel-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now forager-tunnel-agent
curl -s http://127.0.0.1:9300/health   # {"status": "ok", ...}
```

The agent is stdlib-only Python 3, so no pip install is needed.

If the cloud app runs in Docker Compose and the agent runs on the host, set
`CLOUD_TUNNEL_AGENT_URL=http://host.docker.internal:9300` (or the host's
bridge IP) so the container can reach it. When both run on the host,
`http://127.0.0.1:9300` is right.

## 4. Caddy (on the host, not in Docker)

Caddy runs on the host, because only a host-network Caddy can reach both the
app (published on `127.0.0.1:8000` by the compose stack) and the kitchen
tunnel peers (`10.99.0.0/16` on `wg0`, in the host netns). The compose stack
is app + Postgres only.

```bash
sudo apt-get install -y caddy
sudo install -m 0644 /opt/pantryraider/cloud/Caddyfile /etc/caddy/Caddyfile
sudo touch /etc/caddy/forager-kitchens.caddy   # so the import resolves
sudo CLOUD_DOMAIN=forager.pantryraider.app systemctl reload caddy
```

The shipped `cloud/Caddyfile` proxies the apex to `127.0.0.1:8000`, adds the
on-demand TLS block (`ask http://127.0.0.1:8000/v1/tunnel/tls-check` plus an
issuance rate guard), and imports `/etc/caddy/forager-kitchens.caddy`. Set
`CLOUD_DOMAIN` for the caddy service (an `Environment=CLOUD_DOMAIN=...` drop-in
on `caddy.service`, or the systemd default). The agent reloads Caddy itself
with `systemctl reload caddy` whenever a kitchen route changes.

## 5. DNS

Point a wildcard at this VPS so every kitchen subdomain resolves here, in
addition to the apex:

```
forager.pantryraider.app.    A    <VPS public IP>
*.forager.pantryraider.app.  A    <VPS public IP>
```

On-demand TLS means there is no wildcard certificate and no Cloudflare (or
other) DNS API token: Caddy asks the app before issuing each per-kitchen
certificate, and the app says yes only for a subdomain with a live tunnel.

## 6. Cloud app `.env` values

```
CLOUD_TUNNEL_ENDPOINT=forager.pantryraider.app:51820
CLOUD_TUNNEL_SERVER_PUBLIC_KEY=<server_public.key from step 1>
CLOUD_TUNNEL_AGENT_URL=http://172.28.0.1:9300
CLOUD_TUNNEL_AGENT_TOKEN=<the shared token from step 2>
CLOUD_TUNNEL_CIDR=10.99.0.0/16
```

The app container (on the pinned `172.28.0.0/16` compose network) reaches the
host-run agent at the network gateway `172.28.0.1`. Start the agent bound to
that internal-only address so containers can reach it but the public internet
cannot:

```bash
sudo FORAGER_TUNNEL_AGENT_HOST=172.28.0.1 systemctl restart forager-tunnel-agent
```

(Set `Environment=FORAGER_TUNNEL_AGENT_HOST=172.28.0.1` in the systemd unit so
it persists. The gateway interface exists once `docker compose up` has created
the network, so start the compose stack before the agent, or let the agent's
restart pick it up.)

That completes the VPS side. From then on a kitchen enabling remote access in
the app calls `/v1/tunnel/enable`, the app allocates an IP and subdomain and
asks the agent to wire it up, and the kitchen's WireGuard client brings up the
tunnel with the values the endpoint returned.
