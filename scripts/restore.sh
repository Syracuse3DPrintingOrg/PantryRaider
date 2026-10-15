#!/usr/bin/env bash
# Pantry Raider restore: unpacks a backup made by backup.sh.
#
# Usage:
#   ./scripts/restore.sh <backup-file.tar.gz>
#
# Stops the stack, moves current data dirs aside (suffixed .pre-restore),
# unpacks the archive, and restarts. Nothing is deleted: if the restore
# went wrong, the previous state is still in the .pre-restore directories.
#
# It restores into the layout your stack actually uses, found the same way
# backup.sh finds it: INSTALL_DIR when set, else this checkout, the current
# folder or /opt/foodassistant, whichever has a compose file that mounts
# data (docker-compose.prod.yml, install.sh servers, Pi appliances) or
# service/data (docker-compose.yml in a clone of this repository).

set -euo pipefail

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: $0 <backup-file.tar.gz>" >&2
  exit 1
fi

ARCHIVE="$(realpath "$1")"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

# The two layouts, the same names backup.sh and foodassistant-restore use.
layout_dir() {  # layout role
  case "$1:$2" in
    server:app) printf 'service/data' ;;
    server:grocy) printf 'grocy/config' ;;
    server:mealie) printf 'mealie/data' ;;
    appliance:app) printf 'data' ;;
    appliance:grocy) printf 'grocy' ;;
    appliance:mealie) printf 'mealie' ;;
    *) return 1 ;;
  esac
}

# "layout compose-file" for each layout a folder's compose files mount, one
# per line. Without a compose file, the app data folder that exists decides.
layouts_in() {  # dir
  local dir="$1" f found=""
  for f in docker-compose.yml docker-compose.prod.yml compose.yml compose.yaml; do
    [ -f "$dir/$f" ] || continue
    if grep -Eq '^[[:space:]]*-[[:space:]]*["'"'"']?\./service/data:/app/data' "$dir/$f" \
        && [ -d "$dir/service/data" ]; then
      found="$found server:$f"
    fi
    if grep -Eq '^[[:space:]]*-[[:space:]]*["'"'"']?\./data:/app/data' "$dir/$f" \
        && [ -d "$dir/data" ]; then
      found="$found appliance:$f"
    fi
  done
  if [ -z "$found" ]; then
    [ -d "$dir/service/data" ] && found="$found server:"
    [ -d "$dir/data" ] && [ -d "$dir/grocy" ] && found="$found appliance:"
  fi
  printf '%s\n' $found | sed '/^$/d'
}

if ! members="$(tar -tzf "$ARCHIVE" 2>/dev/null)"; then
  echo "ERROR: $ARCHIVE is not a readable .tar.gz backup." >&2
  exit 1
fi
SNAP=""
grep -Eq '^(\./)?service/data(/|$)' <<< "$members" && SNAP="server"
if grep -Eq '^(\./)?data(/|$)' <<< "$members"; then
  if [ -n "$SNAP" ]; then
    echo "ERROR: $ARCHIVE mixes service/data and data, so it is not a Pantry Raider backup." >&2
    exit 1
  fi
  SNAP="appliance"
fi
if [ -z "$SNAP" ]; then
  echo "ERROR: $ARCHIVE holds no Pantry Raider data (service/data or data)." >&2
  exit 1
fi

if [ -n "${INSTALL_DIR:-}" ]; then
  CANDIDATES=("$INSTALL_DIR")
else
  CANDIDATES=("$REPO_DIR" "$PWD" /opt/foodassistant)
fi
PROJECT_DIR=""
LAYOUT=""
COMPOSE_FILE=""
for dir in "${CANDIDATES[@]}"; do
  [ -d "$dir" ] || continue
  found="$(layouts_in "$dir")"
  [ -n "$found" ] || continue
  if [ "$(printf '%s\n' "$found" | cut -d: -f1 | sort -u | wc -l)" -gt 1 ]; then
    # A checkout that holds both: the backup's own layout is the one to use.
    found="$(printf '%s\n' "$found" | grep "^$SNAP:" || true)"
    [ -n "$found" ] || continue
  fi
  PROJECT_DIR="$(cd "$dir" && pwd)"
  LAYOUT="$(printf '%s\n' "$found" | sed -n 1p | cut -d: -f1)"
  COMPOSE_FILE="$(printf '%s\n' "$found" | sed -n 1p | cut -d: -f2-)"
  break
done

if [ -z "$PROJECT_DIR" ]; then
  echo "ERROR: found no Pantry Raider stack to restore into (looked in: ${CANDIDATES[*]})." >&2
  echo "Run this from the folder with your compose file, or set INSTALL_DIR to that folder." >&2
  exit 1
fi

# A backup from the other layout would leave the stack on empty folders, so
# stop before touching anything and point at the helper that maps either
# layout onto this one.
if [ "$SNAP" != "$LAYOUT" ]; then
  echo "ERROR: this backup keeps its data in $(layout_dir "$SNAP" app), but the stack in $PROJECT_DIR uses $(layout_dir "$LAYOUT" app)." >&2
  echo "Restore it with: sudo INSTALL_DIR=\"$PROJECT_DIR\" $REPO_DIR/scripts/image-build/foodassistant-restore \"$ARCHIVE\"" >&2
  exit 1
fi

cd "$PROJECT_DIR"
COMPOSE=(docker compose)
[ -n "$COMPOSE_FILE" ] && COMPOSE+=(-f "$COMPOSE_FILE")
COMPOSE+=(--profile with-grocy --profile with-mealie --profile with-ollama)
DIRS=()
for role in app grocy mealie; do
  DIRS+=("$(layout_dir "$LAYOUT" "$role")")
done
# Only the data folders the backup holds are unpacked, never anything beside
# them (the compose file, .env).
UNPACK=()
for d in "${DIRS[@]}"; do
  grep -Eq "^(\./)?$d(/|\$)" <<< "$members" && UNPACK+=("$d")
done

echo "Stopping containers…"
"${COMPOSE[@]}" stop

echo "Setting aside current data…"
for d in "${UNPACK[@]}"; do
  if [ -d "$d" ]; then
    mv "$d" "$d.pre-restore-$STAMP"
    echo "  $d -> $d.pre-restore-$STAMP"
  fi
done

echo "Unpacking $ARCHIVE…"
tar -xzf "$ARCHIVE" -C "$PROJECT_DIR" "${UNPACK[@]}"

# Files that belong to this one machine, never to your data (the host
# helper's token, the access state, the setup code and the like) stay as they
# were: whatever the archive carried is dropped and this machine's own copies
# come back. Keep this list equal to DEVICE_LOCAL_FILES in
# service/app/routers/admin.py (a test checks it).
DEVICE_LOCAL_FILES="bridge-token bridge-token-enforced bridge-token-grace-started
bridge-identity-verified auth-state.json kiosk-launch-nonces.json setup-code
device.key deck-push.json pairing.json restore-note-pending"
APP_DATA="$(layout_dir "$LAYOUT" app)"
PREV="$APP_DATA.pre-restore-$STAMP"
if [ -d "$APP_DATA" ]; then
  for f in $DEVICE_LOCAL_FILES; do
    find "$APP_DATA" -mindepth 1 -maxdepth 1 -iname "$f" -exec rm -rf {} +
    if [ "$f" != "restore-note-pending" ] && { [ -e "$PREV/$f" ] || [ -L "$PREV/$f" ]; }; then
      cp -a "$PREV/$f" "$APP_DATA/$f"
    fi
  done
  # The app records the restore in its access state on its next start.
  if [ -f "$APP_DATA/settings.json" ] && ! cmp -s "$APP_DATA/settings.json" "$PREV/settings.json"; then
    date +%s > "$APP_DATA/restore-note-pending"
  fi
fi

echo "Restarting containers…"
"${COMPOSE[@]}" start

echo "Done. Previous data kept in *.pre-restore-$STAMP directories, delete them once verified."
