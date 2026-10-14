#!/usr/bin/env bash
# Mixed-version gate for a release. Run it from the repository root on
# a workstation (not on a Pantry Raider appliance) before tagging:
#
#   bash scripts/compat/mixed-version-gate.sh
#
# Required before the tag: G1 to G5. The Home Assistant section is optional
# and runs only with PR_COMPAT_HA=1.
#
#   G1  this release, host-networked, behind a throwaway Caddy and a throwaway
#       nginx, once on its defaults and once with the two lines the docs give:
#       sign-in page through the proxy, the proxy second-factor copy and the
#       setup-code rescue, a kiosk session from a launch ticket, and keyless
#       loopback refused once trust is retired.
#   G2  this release with no password behind Caddy: the grace banner, the
#       refusal page with the setup-code form first once the grace is over, the
#       setup-code sign-in, and Ingress-shaped requests from 172.30.32.2 served
#       only in the add-on.
#   G3  a v0.20.0 satellite container against this release: the config pull,
#       the forwards and the Grocy proxy work, and the payload without the
#       relay block applies.
#   G4  printing: a queue added under the v0.20.0 compose file survives the
#       switch to this release's (the cups-config volume keeps its name), the
#       0.20.x image prints under the new file, a container on the default
#       network cannot reach the print service, and admin over the socket works.
#   G5  the kiosk launcher against a real Chromium (no Docker needed); a missing
#       or broken browser fails the gate instead of skipping.
#
# Every container is named pr-compat-<run>-<n>, every network and volume
# starts with pr-compat-<run>, host ports are 19700 to 19799 on 127.0.0.1 only,
# and the exit trap removes exactly what this run created. Nothing else on the
# machine is touched. Without Docker, G1 to G4 are reported as not run, G5
# still runs, and the gate exits 0 when G5 passes.
#
# Settings (environment):
#   PR_COMPAT_R2_IMAGE   an image of this release to test (default: build one
#                        from this tree and remove it afterwards)
#   PR_COMPAT_OLD_IMAGE  the v0.20.x image (default
#                        ghcr.io/syracuse3dprintingorg/pantryraider:0.20.0)
#   PR_COMPAT_OLD_REF    the tag whose compose file G4 starts from (v0.20.0)
#   PR_COMPAT_HA=1       also run the optional Home Assistant section
#   PR_COMPAT_KEEP=1     leave everything in place for inspection

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

RUN="$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
PREFIX="pr-compat-$RUN"
OLD_IMAGE="${PR_COMPAT_OLD_IMAGE:-ghcr.io/syracuse3dprintingorg/pantryraider:0.20.0}"
OLD_REF="${PR_COMPAT_OLD_REF:-v0.20.0}"
CADDY_IMAGE="caddy:2"
NGINX_IMAGE="nginx:stable"
CURL_IMAGE="curlimages/curl:latest"
HA_IMAGE="ghcr.io/home-assistant/home-assistant:stable"
PASSWORD="fake-gate-pass-aaaaaa1"
NEW_PASSWORD="fake-gate-pass-aaaaaa2"
LEGACY_KEY="fake-gate-key-aaaaaaaa"
AI_KEY="fake-gate-gemini-aaaaaa"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/pr-compat-gate.XXXXXX")"

N=0
CONTAINERS=()
NETWORKS=()
VOLUMES=()
PROJECTS=()
BUILT_IMAGE=""
declare -A RESULT=()
declare -A WHY=()

say() { printf '%s\n' "$*"; }
fail() { say "  FAIL: $*"; return 1; }

cleanup() {
  if [ "${PR_COMPAT_KEEP:-0}" = "1" ]; then
    say "PR_COMPAT_KEEP=1: leaving $PREFIX containers, networks and volumes in place"
    return
  fi
  local p c n v
  for p in "${PROJECTS[@]}"; do
    [ -f "$WORK/$p/compose.yml" ] && docker compose -p "$p" -f "$WORK/$p/compose.yml" \
      down -v --remove-orphans >/dev/null 2>&1
  done
  for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1; done
  for n in "${NETWORKS[@]}"; do docker network rm "$n" >/dev/null 2>&1; done
  for v in "${VOLUMES[@]}"; do docker volume rm -f "$v" >/dev/null 2>&1; done
  [ -n "$BUILT_IMAGE" ] && docker rmi -f "$BUILT_IMAGE" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# These three set NAME, NET and VOL (never call them in $(...): the trap
# must see what they record, and a subshell would lose it).

# A new container name, remembered for the trap before anything is created.
next_name() {
  N=$((N + 1))
  NAME="$PREFIX-$N"
  CONTAINERS+=("$NAME")
}

new_network() {  # tag [docker network create args...]
  NET="$PREFIX-net-$1"; shift
  NETWORKS+=("$NET")
  docker network create "$@" "$NET" >/dev/null
}

new_volume() {  # tag
  VOL="$PREFIX-vol-$1"
  VOLUMES+=("$VOL")
  docker volume create "$VOL" >/dev/null
}

# Where a response redirects to, read from its Location header.
location_of() {  # header-file
  tr -d '\r' < "$1" | sed -n 's/^[Ll]ocation: *//p' | tail -1
}

wait_http() {  # url [seconds]
  local url="$1" i
  for i in $(seq 1 "${2:-90}"); do
    curl -s -o /dev/null -m 3 "$url" && return 0
    sleep 1
  done
  return 1
}

port_free() {
  ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# settings.json for a configured server install (written before the first
# start, so the app starts as an upgraded install with legacy trust).
server_settings() {  # password-or-empty
  python3 - "$1" "$LEGACY_KEY" "$AI_KEY" <<'PY'
import json, sys
pw, key, ai = sys.argv[1:4]
doc = {"deployment_mode": "server", "grocy_base_url": "http://127.0.0.1:9",
       "grocy_api_key": "fake-grocy-gate-aaaaaa", "api_key": key,
       "vision_provider": "gemini", "gemini_api_key": ai,
       "check_for_updates": False, "auto_update": False,
       "auth_required": bool(pw), "auth_password": pw}
print(json.dumps(doc))
PY
}

# Create an app container from IMAGE with a fresh data volume seeded with
# SETTINGS (and the bridge token in SEED_TOKEN, when set), then start it.
# Remaining arguments go to docker create; after -- comes the command.
start_app() {  # name image settings-json data-volume-tag [docker create args...] -- [command...]
  local name="$1" image="$2" settings="$3" tag="$4"; shift 4
  local args=() cmd=()
  new_volume "$tag" || return 1
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do args+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  cmd=("$@")
  docker create --name "$name" -v "$VOL:/app/data" "${args[@]}" "$image" "${cmd[@]}" \
    >/dev/null || return 1
  printf '%s' "$settings" > "$WORK/settings-$tag.json"
  docker cp "$WORK/settings-$tag.json" "$name:/app/data/settings.json" >/dev/null || return 1
  if [ -n "${SEED_TOKEN:-}" ]; then
    (umask 077; printf '%s\n' "$SEED_TOKEN" > "$WORK/token-$tag")
    docker cp "$WORK/token-$tag" "$name:/app/data/bridge-token" >/dev/null || return 1
  fi
  docker start "$name" >/dev/null
}

setup_code_of() {  # container
  docker logs "$1" 2>&1 | grep -o 'Pantry Raider setup code: [^ .]*' | tail -1 \
    | sed 's/^Pantry Raider setup code: //'
}

kiosk_ticket() {  # token
  python3 - "$1" <<'PY'
import hashlib, hmac, secrets, sys, time
token = sys.argv[1].encode()
t, n = str(int(time.time())), secrets.token_hex(16)
mac = hmac.new(token, b"pr-kiosk-v1\n" + t.encode() + b"\n" + n.encode(), hashlib.sha256)
print(f"v1.{t}.{n}.{mac.hexdigest()}")
PY
}

items_of_kind() {  # url kind
  curl -s -m 10 "$1" | python3 -c '
import json, sys
kind = sys.argv[1]
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    items = []
print(sum(1 for i in items if i.get("kind") == kind))' "$2"
}

r2_image() {
  if [ -n "${PR_COMPAT_R2_IMAGE:-}" ]; then
    R2_IMAGE="$PR_COMPAT_R2_IMAGE"
    return 0
  fi
  R2_IMAGE="$PREFIX-r2:gate"
  BUILT_IMAGE="$R2_IMAGE"
  say "Building this release's image from the tree ($R2_IMAGE)"
  docker build -q -t "$R2_IMAGE" -f service/Dockerfile . >/dev/null
}

# --- G1: loopback behind Caddy and nginx --------------------------------------------

caddyfile() {  # listen-port upstream-port file
  cat > "$3" <<CADDY
{
	auto_https off
	admin off
}
http://:$1 {
	bind 127.0.0.1
	reverse_proxy 127.0.0.1:$2
}
CADDY
}

g1() {
  local app caddy nginx token ticket out code jar p
  for p in 19700 19701 19702 19703; do
    port_free "$p" || { fail "port $p is in use"; return 1; }
  done
  if ! port_free 9299; then
    fail "a host bridge answers on 127.0.0.1:9299; run the gate on a machine without one"
    return 1
  fi
  # A throwaway bridge token, so the kitchen screen can be signed in with a
  # launch ticket later.
  token="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  next_name; app="$NAME"
  SEED_TOKEN="$token" start_app "$app" "$R2_IMAGE" "$(server_settings "$PASSWORD")" g1 \
    --network host -- uvicorn app.main:app --host 127.0.0.1 --port 19700 --workers 1 --no-proxy-headers \
    || { fail "could not start this release"; return 1; }
  wait_http "http://127.0.0.1:19700/health" || { fail "this release did not start"; return 1; }

  caddyfile 19701 19700 "$WORK/Caddyfile"
  next_name; caddy="$NAME"
  docker run -d --name "$caddy" --network host \
    -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null \
    || { fail "could not start Caddy"; return 1; }
  wait_http "http://127.0.0.1:19701/health" || { fail "Caddy did not start"; return 1; }

  # The sign-in page through the proxy.
  out="$(curl -s -o /dev/null -D "$WORK/g1-ui.h" -m 10 -w '%{http_code}' http://127.0.0.1:19701/ui/)"
  case "$out $(location_of "$WORK/g1-ui.h")" in
    "303 "*ui/login*) ;;
    *) fail "Caddy GET /ui/ answered '$out', not the sign-in page"; return 1 ;;
  esac
  curl -s -m 10 http://127.0.0.1:19701/ui/login | grep -q 'name="password"' \
    || { fail "the sign-in page through Caddy has no password field"; return 1; }

  # A password alone through the proxy: the proxy second-factor copy and the
  # setup-code link.
  out="$(curl -s -m 10 -o "$WORK/g1-login.html" -w '%{http_code}' \
    --data-urlencode "password=$PASSWORD" http://127.0.0.1:19701/ui/login)"
  [ "$out" = "401" ] || { fail "a password-only sign-in through Caddy answered $out"; return 1; }
  python3 - "$WORK/g1-login.html" <<'PY' || { fail "the proxy second-factor copy or the setup-code link is missing"; return 1; }
import html, sys
text = html.unescape(open(sys.argv[1], encoding="utf-8").read())
ok = ("Signing in through a proxy or tunnel needs a second factor" in text
      and "ui/setup-code" in text)
sys.exit(0 if ok else 1)
PY

  # The setup-code rescue: the code from the log and that password sign in
  # once and land on turning two-factor on.
  curl -s -o /dev/null -m 10 http://127.0.0.1:19701/ui/setup-code
  sleep 1
  code="$(setup_code_of "$app")"
  [ -n "$code" ] || { fail "no setup code in the log"; return 1; }
  out="$(curl -s -o /dev/null -D "$WORK/g1-code.h" -m 10 -w '%{http_code}' \
    --data-urlencode "code=$code" --data-urlencode "password=$PASSWORD" \
    http://127.0.0.1:19701/ui/setup-code)"
  case "$out $(location_of "$WORK/g1-code.h")" in
    "303 "*"setup?twofactor=needed#pane-security") ;;
    *) fail "the setup-code sign-in answered '$out $(location_of "$WORK/g1-code.h")'"; return 1 ;;
  esac

  # nginx on its defaults (Host rewritten, no X-Forwarded-For): the sign-in
  # page, never Settings, and exactly one same_host_proxy inbox item. From
  # 1.30 nginx talks HTTP/1.1 to the app, so a bare first request through it
  # carries no sign of the proxy at all. A browser's next request does: its
  # Referer names the site in the address bar, which is what is sent here.
  # On 19703 the same nginx with the two lines docs/security.md gives is
  # outside from the first request.
  cat > "$WORK/nginx.conf" <<'NGINX'
events {}
http {
  server {
    listen 127.0.0.1:19702;
    location / { proxy_pass http://127.0.0.1:19700; }
  }
  server {
    listen 127.0.0.1:19703;
    location / {
      proxy_pass http://127.0.0.1:19700;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
  }
}
NGINX
  next_name; nginx="$NAME"
  docker run -d --name "$nginx" --network host \
    -v "$WORK/nginx.conf:/etc/nginx/nginx.conf:ro" "$NGINX_IMAGE" >/dev/null \
    || { fail "could not start nginx"; return 1; }
  wait_http "http://127.0.0.1:19702/health" || { fail "nginx did not start"; return 1; }
  out="$(curl -s -o /dev/null -D "$WORK/g1-ng.h" -m 10 -w '%{http_code}' \
    -H 'Accept: text/html' -H 'Referer: https://kitchen.example.com/ui/' \
    http://127.0.0.1:19702/ui/)"
  case "$out $(location_of "$WORK/g1-ng.h")" in
    "303 "*ui/login*) ;;
    *) fail "nginx GET /ui/ answered '$out $(location_of "$WORK/g1-ng.h")', not the sign-in page"; return 1 ;;
  esac
  # /setup is not a /ui page, so a visitor the proxy hides gets 401 there
  # (row d1); a redirect to the sign-in page would do as well. Never Settings.
  out="$(curl -s -o "$WORK/g1-setup.html" -D "$WORK/g1-setup.h" -m 10 -w '%{http_code}' \
    -H 'Accept: text/html' -H 'Referer: https://kitchen.example.com/ui/' \
    http://127.0.0.1:19702/setup)"
  case "$out $(location_of "$WORK/g1-setup.h")" in
    "401 "*|"30"[0-9]" "*ui/login*) ;;
    *) fail "nginx GET /setup answered '$out' (a Settings page must never show)"; return 1 ;;
  esac
  [ "$(items_of_kind http://127.0.0.1:19700/action-items same_host_proxy)" = "1" ] \
    || { fail "the inbox does not hold exactly one same_host_proxy item"; return 1; }
  out="$(curl -s -o /dev/null -D "$WORK/g1-ng2.h" -m 10 -w '%{http_code}' \
    -H 'Accept: text/html' http://127.0.0.1:19703/ui/)"
  case "$out $(location_of "$WORK/g1-ng2.h")" in
    "303 "*ui/login*) ;;
    *) fail "nginx with Host and X-Forwarded-For: GET /ui/ answered '$out', not the sign-in page"; return 1 ;;
  esac

  # Retire the old trust for this device's programs: a keyless program here
  # is refused, and the kitchen screen signs in with a launch ticket.
  out="$(curl -s -o /dev/null -m 10 -w '%{http_code}' -H 'Content-Type: application/json' \
    -d '{"action": "retire"}' http://127.0.0.1:19700/setup/security/local-trust)"
  [ "$out" = "200" ] || { fail "retiring local trust answered $out"; return 1; }
  out="$(curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:19700/pending/count)"
  [ "$out" = "401" ] || { fail "keyless loopback after retirement answered $out, not 401"; return 1; }
  ticket="$(kiosk_ticket "$token")"
  jar="$WORK/g1-cookies"
  out="$(curl -s -o /dev/null -m 10 -c "$jar" -w '%{http_code}' -G \
    --data-urlencode "ticket=$ticket" --data-urlencode "next=/ui/" \
    http://localhost:19700/ui/kiosk/launch)"
  [ "$out" = "303" ] || { fail "the launch ticket answered $out"; return 1; }
  out="$(curl -s -o /dev/null -m 10 -b "$jar" -w '%{http_code}' http://localhost:19700/pending/count)"
  [ "$out" = "200" ] || { fail "the kiosk session was not accepted ($out)"; return 1; }
  return 0
}

# --- G2: an install with no password behind Caddy, and Ingress ----------------------

g2() {
  local app caddy out code hassio variant ip want got p
  for p in 19710 19711; do
    port_free "$p" || { fail "port $p is in use"; return 1; }
  done
  next_name; app="$NAME"
  start_app "$app" "$R2_IMAGE" "$(server_settings "")" g2 --network host -- \
    uvicorn app.main:app --host 127.0.0.1 --port 19710 --workers 1 --no-proxy-headers \
    || { fail "could not start this release"; return 1; }
  wait_http "http://127.0.0.1:19710/health" || { fail "this release did not start"; return 1; }
  caddyfile 19711 19710 "$WORK/Caddyfile2"
  next_name; caddy="$NAME"
  docker run -d --name "$caddy" --network host \
    -v "$WORK/Caddyfile2:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null \
    || { fail "could not start Caddy"; return 1; }
  wait_http "http://127.0.0.1:19711/health" || { fail "Caddy did not start"; return 1; }

  # In the grace: served, with the banner that names two-factor and never
  # offers a password alone for access from outside.
  out="$(curl -s -o "$WORK/g2-grace.html" -m 15 -w '%{http_code}' -H 'Accept: text/html' \
    http://127.0.0.1:19711/ui/timers)"
  [ "$out" = "200" ] || { fail "in the grace, /ui/timers through Caddy answered $out"; return 1; }
  python3 - "$WORK/g2-grace.html" <<'PY' || { fail "the grace banner is missing or offers a password alone"; return 1; }
import html, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'id="pr-grace-banner".*?</div>\s*<button', text, re.S)
if not m:
    sys.exit(1)
banner = re.sub(r"<[^>]+>", " ", html.unescape(m.group(0)))
ok = "two-factor" in banner and "or set a password" not in banner
sys.exit(0 if ok else 1)
PY

  # Move the grace 15 days back.
  docker exec -i "$app" python3 - <<'PY' || { fail "could not move the grace back"; return 1; }
import json
from datetime import datetime, timedelta
p = "/app/data/auth-state.json"
doc = json.load(open(p))
def back(value):
    if not value:
        return value
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (when - timedelta(days=15)).isoformat()
doc["first_start"] = back(doc.get("first_start"))
om = doc.setdefault("open_mode", {})
om["grace_until"] = back(om.get("grace_until"))
json.dump(doc, open(p, "w"))
PY
  docker restart "$app" >/dev/null
  wait_http "http://127.0.0.1:19710/health" || { fail "this release did not restart"; return 1; }

  # After the grace: 403 with the rescue page, the setup-code form first.
  out="$(curl -s -o "$WORK/g2-refused.html" -m 15 -w '%{http_code}' -H 'Accept: text/html' \
    http://127.0.0.1:19711/ui/timers)"
  [ "$out" = "403" ] || { fail "after the grace, /ui/timers through Caddy answered $out"; return 1; }
  python3 - "$WORK/g2-refused.html" <<'PY' || { fail "the refusal page does not show the setup-code form first"; return 1; }
import sys
text = open(sys.argv[1], encoding="utf-8").read()
form = text.find('name="code"')
home = text.find("At home, open this kitchen at")
sys.exit(0 if form != -1 and (home == -1 or form < home) else 1)
PY
  sleep 1
  code="$(setup_code_of "$app")"
  [ -n "$code" ] || { fail "no setup code in the log"; return 1; }

  # Ingress-shaped requests from 172.30.32.2 (before the code is used): served
  # only by an add-on install. Two copies of this install's data, one started
  # as the add-on.
  new_network hassio --subnet 172.30.32.0/23 \
    || { fail "could not create a network on 172.30.32.0/23 (another one uses it)"; return 1; }
  hassio="$NET"
  docker exec "$app" tar -C /app/data -cf - . > "$WORK/g2-data.tar" \
    || { fail "could not copy this install's data"; return 1; }
  for variant in plain addon; do
    local env_args=()
    [ "$variant" = "addon" ] && env_args=(-e PANTRY_RAIDER_ADDON=1)
    ip="172.30.32.$([ "$variant" = addon ] && echo 11 || echo 10)"
    new_volume "g2-$variant" || return 1
    docker run --rm -i -v "$VOL:/d" --entrypoint tar "$R2_IMAGE" -C /d -xf - \
      < "$WORK/g2-data.tar" || { fail "could not copy the data"; return 1; }
    next_name
    docker run -d --name "$NAME" --network "$hassio" --ip "$ip" -v "$VOL:/app/data" \
      "${env_args[@]}" "$R2_IMAGE" >/dev/null || { fail "could not start the $variant copy"; return 1; }
  done
  for variant in plain addon; do
    ip="172.30.32.$([ "$variant" = addon ] && echo 11 || echo 10)"
    want="$([ "$variant" = addon ] && echo 200 || echo 403)"
    got=""
    for _ in $(seq 1 30); do
      next_name
      got="$(docker run --rm --name "$NAME" --network "$hassio" --ip 172.30.32.2 "$CURL_IMAGE" \
        -s -o /dev/null -m 15 -w '%{http_code}' \
        -H 'X-Ingress-Path: /api/hassio_ingress/gatetoken' -H 'X-Hass-Source: core.ingress' \
        -H 'X-Forwarded-For: 192.168.1.20, 172.30.32.1' \
        -H 'X-Forwarded-Host: homeassistant.local:8123' -H 'X-Forwarded-Proto: http' \
        -H 'Accept: text/html' "http://$ip:8000/ui/timers" 2>/dev/null)"
      [ "$got" != "000" ] && [ -n "$got" ] && break
      sleep 2
    done
    [ "$got" = "$want" ] \
      || { fail "an Ingress-shaped request to the $variant copy answered $got, not $want"; return 1; }
  done

  # The setup code and a new password sign in and land on turning two-factor on.
  out="$(curl -s -o /dev/null -D "$WORK/g2-code.h" -m 10 -w '%{http_code}' \
    --data-urlencode "code=$code" --data-urlencode "password=$NEW_PASSWORD" \
    --data-urlencode "confirm=$NEW_PASSWORD" http://127.0.0.1:19711/ui/setup-code)"
  case "$out $(location_of "$WORK/g2-code.h")" in
    "303 "*"setup?twofactor=needed#pane-security") ;;
    *) fail "the setup-code sign-in answered '$out $(location_of "$WORK/g2-code.h")'"; return 1 ;;
  esac
  return 0
}

# --- G3: a v0.20.0 satellite against this release ------------------------------------

g3() {
  local net grocy server sat out i applied=no p settings
  for p in 19720 19721; do
    port_free "$p" || { fail "port $p is in use"; return 1; }
  done
  new_network fleet || { fail "could not create the fleet network"; return 1; }
  net="$NET"
  # A stand-in Grocy that answers the product list, so a proxied request that
  # gets through comes back 200 with [].
  new_volume grocy || return 1
  docker run --rm -v "$VOL:/stub" --entrypoint sh "$R2_IMAGE" -c \
    'mkdir -p /stub/api/objects && printf "[]" > /stub/api/objects/products' \
    || { fail "could not write the Grocy stand-in"; return 1; }
  next_name; grocy="$NAME"
  docker run -d --name "$grocy" --network "$net" -v "$VOL:/stub:ro" --entrypoint python3 \
    "$R2_IMAGE" -m http.server 8000 --directory /stub >/dev/null \
    || { fail "could not start the Grocy stand-in"; return 1; }

  next_name; server="$NAME"
  settings="$(server_settings "$PASSWORD" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
doc["grocy_base_url"] = "http://" + sys.argv[1] + ":8000"
print(json.dumps(doc))' "$grocy")"
  start_app "$server" "$R2_IMAGE" "$settings" g3-server --network "$net" \
    -p 127.0.0.1:19721:8000 -- || { fail "could not start this release as the server"; return 1; }
  wait_http "http://127.0.0.1:19721/health" || { fail "the server did not start"; return 1; }

  next_name; sat="$NAME"
  settings="$(python3 - "$server" "$LEGACY_KEY" <<'PY'
import json, sys
server, key = sys.argv[1:3]
print(json.dumps({"deployment_mode": "pi_remote", "remote_server_url": f"http://{server}:8000",
                  "upstream_api_key": key, "auth_required": False, "auth_password": "",
                  "check_for_updates": False, "auto_update": False,
                  "device_id": "compat-gate-sat-1"}))
PY
)"
  start_app "$sat" "$OLD_IMAGE" "$settings" g3-sat --network "$net" -p 127.0.0.1:19720:8000 -- \
    || { fail "could not start the v0.20.0 satellite"; return 1; }
  wait_http "http://127.0.0.1:19720/health" 120 || { fail "the satellite did not start"; return 1; }

  # The pull at start: the payload without a relay block applies (the AI key
  # arrives on the satellite and no fleet_relay is written).
  for i in $(seq 1 45); do
    if docker exec -i "$sat" python3 - "$AI_KEY" <<'PY'
import json, sys
doc = json.load(open("/app/data/settings.json"))
ok = doc.get("gemini_api_key") == sys.argv[1] and not doc.get("fleet_relay")
sys.exit(0 if ok else 1)
PY
    then applied=yes; break; fi
    [ "$i" = 20 ] && docker restart "$sat" >/dev/null   # pull again at start
    sleep 2
  done
  [ "$applied" = yes ] || { fail "the satellite did not apply the server's config"; return 1; }

  # A forward, sent from the satellite's own screen: a v0.20.0 satellite with
  # no password keeps its forwarding routes to its own loopback address.
  out="$(docker exec "$sat" python3 -c '
import urllib.error, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:8000/pending/count", timeout=15)
    print(r.read().decode(), r.status)
except urllib.error.HTTPError as e:
    print(e.read().decode(), e.code)' 2>&1)"
  case "$out" in
    *'"count"'*' 200') ;;
    *) fail "the forwarded /pending/count answered '$out'"; return 1 ;;
  esac

  # The Grocy proxy, through the satellite's own client.
  docker exec "$sat" python3 -c '
import asyncio
from app.services.grocy import GrocyClient
print(asyncio.run(GrocyClient().get_products()))' > "$WORK/g3-proxy.out" 2>&1 \
    || { fail "the proxied Grocy request failed: $(tail -1 "$WORK/g3-proxy.out")"; return 1; }
  [ "$(tail -1 "$WORK/g3-proxy.out")" = "[]" ] \
    || { fail "the proxied Grocy request returned $(tail -1 "$WORK/g3-proxy.out")"; return 1; }
  return 0
}

# --- G4: printing across the compose switch ------------------------------------------

compose_for() {  # source-file project -> $WORK/<project>/compose.yml
  local src="$1" project="$2"
  mkdir -p "$WORK/$project"
  python3 - "$src" "$project" "$OLD_IMAGE" "$WORK/$project/compose.yml" <<'PY'
import sys

import yaml

src, project, image, out = sys.argv[1:5]
doc = yaml.safe_load(open(src, encoding="utf-8"))
services = doc.get("services", {})
keep = {}
for name in ("service", "cups"):
    svc = dict(services[name])
    # Our own names, no published ports, no updater, nothing from the host.
    svc["container_name"] = f"{project}-{name}"
    for key in ("ports", "labels", "depends_on", "env_file", "logging"):
        svc.pop(key, None)
    svc["restart"] = "no"
    if name == "service":
        svc["image"] = image
        svc["volumes"] = [v for v in svc.get("volumes", []) if not str(v).startswith("./")]
        svc["networks"] = [n for n in svc.get("networks", ["default"]) if n != "updater"]
    keep[name] = svc
doc["services"] = keep
nets = {k: v for k, v in (doc.get("networks") or {}).items() if k != "updater"}
if nets:
    doc["networks"] = nets
else:
    doc.pop("networks", None)
doc["volumes"] = {k: v for k, v in (doc.get("volumes") or {}).items() if k.startswith("cups")}
for key in [k for k in doc if k.startswith("x-")]:
    doc.pop(key)
yaml.safe_dump(doc, open(out, "w"), sort_keys=False)
PY
}

add_queue() {  # container queue [lpadmin args...]
  local ctr="$1" queue="$2"; shift 2
  docker exec "$ctr" lpadmin "$@" -p "$queue" -v lpd://127.0.0.1:9/queue -E -m raw \
    >/dev/null 2>&1 \
    || docker exec "$ctr" lpadmin "$@" -p "$queue" -v lpd://127.0.0.1:9/queue -E \
      -m drv:///sample.drv/generic.ppd >/dev/null 2>&1
}

g4() {
  local project="$PREFIX-g4" old_src="$WORK/old-compose.yml" queue="PRCompatQueue" i out
  git show "$OLD_REF:docker-compose.prod.yml" > "$old_src" 2>/dev/null \
    || { fail "could not read the $OLD_REF compose file"; return 1; }
  PROJECTS+=("$project")
  CONTAINERS+=("$project-service" "$project-cups")
  compose_for "$old_src" "$project" || { fail "could not prepare the $OLD_REF compose file"; return 1; }
  docker compose -p "$project" -f "$WORK/$project/compose.yml" --profile with-printing \
    up -d cups >/dev/null 2>&1 || { fail "the $OLD_REF print service did not start"; return 1; }
  # lpstat -r exits 0 either way, so read what it says: the print service
  # fetches its cupsd.conf before the scheduler starts.
  for i in $(seq 1 30); do
    docker exec "$project-cups" lpstat -r 2>/dev/null | grep -q 'scheduler is running' && break
    sleep 2
  done
  add_queue "$project-cups" "$queue" \
    || { fail "could not add a queue under the $OLD_REF compose file"; return 1; }
  docker compose -p "$project" -f "$WORK/$project/compose.yml" --profile with-printing \
    down >/dev/null 2>&1

  compose_for "docker-compose.prod.yml" "$project" \
    || { fail "could not prepare this release's compose file"; return 1; }
  docker compose -p "$project" -f "$WORK/$project/compose.yml" --profile with-printing \
    up -d cups service >/dev/null 2>&1 || { fail "this release's compose file did not start"; return 1; }
  for i in $(seq 1 30); do
    docker exec "$project-cups" test -S /run/cups/cups.sock 2>/dev/null && break
    sleep 2
  done
  docker volume inspect "${project}_cups-config" >/dev/null 2>&1 \
    || { fail "the cups-config volume changed its name"; return 1; }
  docker exec "$project-cups" lpstat -v 2>/dev/null | grep -q "$queue" \
    || { fail "the queue added before the switch is gone"; return 1; }

  # The 0.20.x image prints through the socket the new file shares.
  out="$(docker exec "$project-service" lp -d "$queue" /etc/hostname 2>&1)" \
    || { fail "the 0.20.x image could not print: $out"; return 1; }
  case "$out" in *"request id"*) ;; *) fail "lp answered '$out'"; return 1 ;; esac

  # Nothing on the default network reaches the print service.
  next_name
  if docker run --rm --name "$NAME" --network "${project}_default" "$CURL_IMAGE" \
      -s -o /dev/null -m 5 http://cups:631/ >/dev/null 2>&1; then
    fail "a container on the default network reached cups:631"; return 1
  fi

  # Admin over the socket.
  add_queue "$project-service" PRCompatSocket -h /run/cups/cups.sock \
    || { fail "admin over the socket was refused"; return 1; }
  docker exec "$project-cups" lpstat -v 2>/dev/null | grep -q PRCompatSocket \
    || { fail "the queue added over the socket is missing"; return 1; }
  return 0
}

# --- G5: the kiosk launcher against a real Chromium ----------------------------------

g5() {
  local rc
  PR_CHROMIUM_REQUIRED=1 python3 -m pytest tests/test_kiosk_launcher_chromium.py -q \
    > "$WORK/g5.log" 2>&1
  rc=$?
  tail -3 "$WORK/g5.log" | sed 's/^/  /'
  [ $rc -eq 0 ] || { fail "the launcher test failed (PR_CHROMIUM_REQUIRED=1)"; return 1; }
  return 0
}

# --- Optional: Home Assistant with both integrations --------------------------------

ha() {
  local ver dir out name
  for ver in "$OLD_REF" current; do
    dir="$WORK/ha-$ver"
    mkdir -p "$dir/custom_components"
    if [ "$ver" = current ]; then
      cp -r homeassistant/custom_components/pantry_raider "$dir/custom_components/"
    else
      git archive "$ver" homeassistant/custom_components/pantry_raider \
        | tar -x -C "$dir" --strip-components=1 \
        || { fail "could not export the $ver integration"; return 1; }
    fi
    new_volume "ha-${ver//./-}" || return 1
    next_name; name="$NAME"
    docker create --name "$name" -v "$VOL:/config" "$HA_IMAGE" >/dev/null \
      || { fail "could not create Home Assistant"; return 1; }
    docker cp "$dir/custom_components" "$name:/config/" >/dev/null \
      || { fail "could not copy the $ver integration"; return 1; }
    docker start "$name" >/dev/null || { fail "could not start Home Assistant"; return 1; }
    for _ in $(seq 1 90); do
      docker logs "$name" 2>&1 | grep -q 'custom integration pantry_raider' && break
      sleep 2
    done
    docker logs "$name" 2>&1 | grep -q 'custom integration pantry_raider' \
      || { fail "Home Assistant did not find the $ver integration"; return 1; }
    sleep 5
    out="$(docker logs "$name" 2>&1 | grep -i 'pantry_raider' | grep -iE 'error|exception|failed' || true)"
    [ -z "$out" ] || { fail "the $ver integration logged errors: $(printf '%s' "$out" | head -3)"; return 1; }
    docker rm -f "$name" >/dev/null 2>&1
  done
  return 0
}

# --- Run ------------------------------------------------------------------------------

run_section() {  # id title function
  say ""
  say "$1: $2"
  if "$3"; then RESULT[$1]=PASS; say "  PASS"; else RESULT[$1]=FAIL; fi
}

have_docker=no
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  have_docker=yes
fi

say "Pantry Raider mixed-version gate (run $RUN)"
if [ "$have_docker" = yes ]; then
  if r2_image; then
    run_section G1 "loopback behind Caddy and nginx" g1
    run_section G2 "no password behind Caddy, and Ingress" g2
    run_section G3 "a v0.20.0 satellite against this release" g3
    run_section G4 "printing across the compose switch" g4
  else
    for s in G1 G2 G3 G4; do RESULT[$s]=FAIL; WHY[$s]="this release's image did not build"; done
    say "Could not build this release's image."
  fi
else
  say "Docker is not available: G1 to G4 are skipped (not run)."
  for s in G1 G2 G3 G4; do RESULT[$s]=SKIP; done
fi
run_section G5 "the kiosk launcher against a real Chromium" g5
if [ "${PR_COMPAT_HA:-0}" = "1" ] && [ "$have_docker" = yes ]; then
  run_section HA "Home Assistant with both integrations (optional)" ha
else
  RESULT[HA]=SKIP
  say ""
  say "HA: Home Assistant with both integrations (optional): skipped (set PR_COMPAT_HA=1)"
fi

say ""
say "Gate result:"
status=0
for s in G1 G2 G3 G4 G5 HA; do
  line="  $s ${RESULT[$s]}"
  [ -n "${WHY[$s]:-}" ] && line="$line (${WHY[$s]})"
  say "$line"
  case "$s:${RESULT[$s]}" in
    HA:*) ;;
    *:FAIL) status=1 ;;
  esac
done
if [ "$have_docker" != yes ]; then
  say "  G1 to G4 were not run; run the gate on a machine with Docker before tagging."
fi
exit $status
