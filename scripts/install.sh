#!/usr/bin/env bash
# Pantry Raider installer: pulls the prebuilt image and starts the stack.
# No git clone or local build required.
#
#   curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/scripts/install.sh | bash
#
# Environment overrides:
#   INSTALL_DIR=/opt/foodassistant   where to put compose + data (default ./foodassistant)
#   PROFILES="with-grocy with-mealie"  optional bundled backends to start
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main"
INSTALL_DIR="${INSTALL_DIR:-foodassistant}"
PROFILES="${PROFILES:-with-grocy}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$1" >&2; }

command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"
if ! docker compose version >/dev/null 2>&1; then
  die "Docker Compose v2 is not available. Pantry Raider needs Docker 24 or newer with the compose plugin."
fi

say "Installing into ./${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "Need curl or wget to download files."; fi
}

say "Fetching docker-compose.yml"
fetch "$REPO_RAW/docker-compose.prod.yml" docker-compose.yml

# Grocy runs every executable script in this folder at start-up; the repair
# script keeps Grocy loading across its 4.7 config change. Fetched on every
# run so re-running the installer picks up fixes. Downloads land as 644 and
# the init step skips files that are not executable, hence the chmod. On an
# older install Docker already created the folder empty and owned by root, so
# a re-run without sudo cannot write there; warn and carry on rather than stop
# before the containers start.
say "Fetching the Grocy start-up repair script"
GROCY_INIT=docker/grocy-init/10-repair-auth-class.sh
mkdir -p docker/grocy-init
if [ -w docker/grocy-init ] && { [ ! -e "$GROCY_INIT" ] || [ -w "$GROCY_INIT" ]; }; then
  fetch "$REPO_RAW/docker/grocy-init/10-repair-auth-class.sh" docker/grocy-init/10-repair-auth-class.sh
  chmod 755 docker/grocy-init/10-repair-auth-class.sh
else
  warn "Could not add the Grocy start-up repair script because the docker/grocy-init folder belongs to another user. Run: sudo chown -R \"$(id -u):$(id -g)\" \"$PWD/docker/grocy-init\" and then run this installer again."
fi

if [ ! -f .env ]; then
  say "Fetching .env (edit later to pin settings; the /setup wizard also works)"
  fetch "$REPO_RAW/.env.example" .env || true
fi

PROFILE_ARGS=""
for p in $PROFILES; do PROFILE_ARGS="$PROFILE_ARGS --profile $p"; done

say "Starting containers${PROFILE_ARGS:+ (profiles:$PROFILES)}"
# shellcheck disable=SC2086
docker compose $PROFILE_ARGS up -d

# The automatic updater restarts in a loop on Docker versions it cannot talk
# to, and compose still reports success, so check it a few seconds in. A
# looping container is briefly running between restarts, so look more than
# once before calling it healthy.
updater_restarting() {
  for _ in 1 2 3; do
    [ "$(docker inspect -f '{{.State.Restarting}}' foodassistant-watchtower 2>/dev/null || true)" = "true" ] && return 0
    sleep 1
  done
  return 1
}
sleep 5
if updater_restarting; then
  warn "The automatic updater is not starting, so Pantry Raider will not update itself. Server installs need Docker 24 or newer. Update Docker if yours is older, then run this installer again to get the current updater."
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done. Open the setup wizard:"
printf '    http://%s:9284/setup\n' "${HOST_IP:-YOUR-HOST}"
echo "Set a password during setup (required by default). Grocy sets itself up, so the only key you may want is an AI provider key for photo and receipt scanning."
