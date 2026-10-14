#!/usr/bin/env bash
# Pantry Raider restore: unpacks a backup made by backup.sh.
#
# Usage:
#   ./scripts/restore.sh <backup-file.tar.gz>
#
# Stops the stack, moves current data dirs aside (suffixed .pre-restore),
# unpacks the archive, and restarts. Nothing is deleted: if the restore
# went wrong, the previous state is still in the .pre-restore directories.

set -euo pipefail

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: $0 <backup-file.tar.gz>" >&2
  exit 1
fi

ARCHIVE="$(realpath "$1")"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

cd "$REPO_DIR"

# A Pantry Raider device's USB backup keeps its data in data, grocy and
# mealie, which this checkout's stack does not mount. Unpacking one here would
# leave the stack on empty folders, so stop before touching anything and point
# at the helper that maps either layout onto this one.
if ! members="$(tar -tzf "$ARCHIVE" 2>/dev/null)"; then
  echo "ERROR: $ARCHIVE is not a readable .tar.gz backup." >&2
  exit 1
fi
if grep -Eq '^(\./)?data(/|$)' <<< "$members"; then
  echo "ERROR: this backup came from a Pantry Raider device (it holds data, grocy and mealie)." >&2
  echo "Restore it with: sudo INSTALL_DIR=\"$REPO_DIR\" $REPO_DIR/scripts/image-build/foodassistant-restore \"$ARCHIVE\"" >&2
  exit 1
fi

echo "Stopping containers…"
docker compose --profile with-grocy --profile with-mealie --profile with-ollama stop

echo "Setting aside current data…"
for d in service/data grocy/config mealie/data; do
  if [ -d "$d" ]; then
    mv "$d" "$d.pre-restore-$STAMP"
    echo "  $d -> $d.pre-restore-$STAMP"
  fi
done

echo "Unpacking $ARCHIVE…"
tar -xzf "$ARCHIVE" -C "$REPO_DIR"

# Files that belong to this one machine, never to your data (the host
# helper's token, the access state, the setup code and the like) stay as they
# were: whatever the archive carried is dropped and this machine's own copies
# come back. Keep this list equal to DEVICE_LOCAL_FILES in
# service/app/routers/admin.py (a test checks it).
DEVICE_LOCAL_FILES="bridge-token bridge-token-enforced bridge-token-grace-started
bridge-identity-verified auth-state.json kiosk-launch-nonces.json setup-code
device.key deck-push.json pairing.json restore-note-pending"
APP_DATA="service/data"
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
docker compose --profile with-grocy --profile with-mealie --profile with-ollama start

echo "Done. Previous data kept in *.pre-restore-$STAMP directories, delete them once verified."
