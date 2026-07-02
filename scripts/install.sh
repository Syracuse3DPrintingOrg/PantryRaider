#!/usr/bin/env bash
# Pantry Raider installer — pulls the prebuilt image and starts the stack.
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

command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"
if ! docker compose version >/dev/null 2>&1; then
  die "Docker Compose v2 is not available. Update Docker or install the compose plugin."
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

if [ ! -f .env ]; then
  say "Fetching .env (edit later to pin settings; the /setup wizard also works)"
  fetch "$REPO_RAW/.env.example" .env || true
fi

PROFILE_ARGS=""
for p in $PROFILES; do PROFILE_ARGS="$PROFILE_ARGS --profile $p"; done

say "Starting containers${PROFILE_ARGS:+ (profiles:$PROFILES)}"
# shellcheck disable=SC2086
docker compose $PROFILE_ARGS up -d

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done. Open the setup wizard:"
printf '    http://%s:9284/setup\n' "${HOST_IP:-YOUR-HOST}"
echo "Set a password during setup (required by default), then add your Grocy + AI provider keys."
