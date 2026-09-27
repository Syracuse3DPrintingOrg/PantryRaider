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

echo "Restarting containers…"
docker compose --profile with-grocy --profile with-mealie --profile with-ollama start

echo "Done. Previous data kept in *.pre-restore-$STAMP directories, delete them once verified."
