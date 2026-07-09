#!/usr/bin/env bash
# Pantry Raider backup: tars the bind-mounted data directories.
#
# Usage:
#   ./scripts/backup.sh [destination-dir]      # default: ./backups
#
# Restore:
#   ./scripts/restore.sh <backup-file.tar.gz>
#
# Cron example (2:30 AM daily, keep 14 days):
#   30 2 * * * /path/to/PantryRaider/scripts/backup.sh /mnt/nas/foodassistant-backups
#
# What's included:
#   service/data:   settings.json, SQLite defaults DB, staples.txt
#   grocy/config:   Grocy SQLite DB + config (inventory lives here)
#   mealie/data:    Mealie SQLite DB, recipe images
# Ollama models are NOT backed up, they're re-downloadable.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$REPO_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/foodassistant-$STAMP.tar.gz"

mkdir -p "$DEST"

# Grocy and Mealie use SQLite; a copy while a write is in flight can be
# inconsistent. sqlite3 .backup would be ideal, but a tar of a mostly-idle
# home instance at 2:30 AM is acceptable. Stop the stack first if you want
# guaranteed consistency: docker compose stop && backup && docker compose start
DIRS=()
for d in service/data grocy/config mealie/data; do
  [ -d "$REPO_DIR/$d" ] && DIRS+=("$d")
done

if [ ${#DIRS[@]} -eq 0 ]; then
  echo "ERROR: no data directories found under $REPO_DIR, nothing to back up." >&2
  exit 1
fi

tar -czf "$OUT" -C "$REPO_DIR" "${DIRS[@]}"
echo "Backed up ${DIRS[*]} -> $OUT ($(du -h "$OUT" | cut -f1))"

# Rotate: delete backups older than KEEP_DAYS
find "$DEST" -name 'foodassistant-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
echo "Rotation: backups older than $KEEP_DAYS days removed."
