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
# It backs up whichever layout your stack actually uses:
#   docker-compose.prod.yml, install.sh servers and Pi appliances:
#     data (settings.json, the app database, staples.txt), grocy (Grocy's
#     database and config, where your inventory lives), mealie (recipes)
#   docker-compose.yml in a clone of this repository:
#     service/data, grocy/config, mealie/data, the same three
# It looks for the folder with your compose file in INSTALL_DIR when that is
# set, then in this checkout, the current folder and /opt/foodassistant, and
# stops with an error rather than write an archive without your data.
# Ollama models are NOT backed up, they're re-downloadable.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$REPO_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/foodassistant-$STAMP.tar.gz"

mkdir -p "$DEST"

# Grocy, Mealie and Pantry Raider keep SQLite databases, and a plain copy
# taken while one is being written can come back unreadable. Each database is
# copied through SQLite's own online backup first (see below), so the archive
# holds a consistent copy while the stack keeps running.

# The two layouts, the same names foodassistant-restore uses: appliance (the
# compose stack's ./data, ./grocy and ./mealie) and server (a checkout's
# service/data, grocy/config and mealie/data).
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

has_files() { [ -d "$1" ] && [ -n "$(ls -A "$1" 2>/dev/null)" ]; }

# The layouts a folder's compose files mount whose app data folder holds
# something, one per line. Without a compose file, the app data folders that
# hold something decide.
layouts_in() {  # dir
  local dir="$1" f found=""
  for f in docker-compose.yml docker-compose.prod.yml compose.yml compose.yaml; do
    [ -f "$dir/$f" ] || continue
    if grep -Eq '^[[:space:]]*-[[:space:]]*["'"'"']?\./service/data:/app/data' "$dir/$f" \
        && has_files "$dir/service/data"; then
      found="$found server"
    fi
    if grep -Eq '^[[:space:]]*-[[:space:]]*["'"'"']?\./data:/app/data' "$dir/$f" \
        && has_files "$dir/data"; then
      found="$found appliance"
    fi
  done
  if [ -z "$found" ]; then
    has_files "$dir/service/data" && found="$found server"
    has_files "$dir/data" && has_files "$dir/grocy" && found="$found appliance"
  fi
  printf '%s\n' $found | sort -u | sed '/^$/d'
}

if [ -n "${INSTALL_DIR:-}" ]; then
  CANDIDATES=("$INSTALL_DIR")
else
  CANDIDATES=("$REPO_DIR" "$PWD" /opt/foodassistant)
fi
PROJECT_DIR=""
LAYOUT=""
for dir in "${CANDIDATES[@]}"; do
  [ -d "$dir" ] || continue
  found="$(layouts_in "$dir")"
  [ -n "$found" ] || continue
  if [ "$(printf '%s\n' "$found" | wc -l)" -gt 1 ]; then
    echo "ERROR: $dir holds data in both service/data and data, so it is not clear which one your stack uses." >&2
    echo "Move the one you no longer use aside, then run this again." >&2
    exit 1
  fi
  PROJECT_DIR="$(cd "$dir" && pwd)"
  LAYOUT="$found"
  break
done

if [ -z "$PROJECT_DIR" ]; then
  echo "ERROR: found no Pantry Raider data to back up (looked in: ${CANDIDATES[*]})." >&2
  echo "Run this from the folder with your compose file, or set INSTALL_DIR to that folder." >&2
  echo "If the data folders belong to root, run it with sudo." >&2
  exit 1
fi

DIRS=()
for role in app grocy mealie; do
  d="$(layout_dir "$LAYOUT" "$role")"
  has_files "$PROJECT_DIR/$d" && DIRS+=("$d")
done
APP_DIR="$(layout_dir "$LAYOUT" app)"

# Files that belong to this one device, never to your data: the host helper's
# token and markers, the access state, kiosk tickets, the setup code, the
# device key and the like. A restore onto another device must not bring them
# along. Keep this list equal to DEVICE_LOCAL_FILES in
# service/app/routers/admin.py (a test checks it).
DEVICE_LOCAL_FILES="bridge-token bridge-token-enforced bridge-token-grace-started
bridge-identity-verified auth-state.json kiosk-launch-nonces.json setup-code
device.key deck-push.json pairing.json restore-note-pending"
EXCLUDES=(--anchored --ignore-case)
for f in $DEVICE_LOCAL_FILES; do
  EXCLUDES+=("--exclude=$APP_DIR/$f")
done
# The app's own spare and damaged copies (a .last-good copy kept beside a
# file, a damaged file set aside as .corrupt-<time>), its database snapshots
# and the copy it takes before a restore describe this device's history, not
# your data, and the app's own backup leaves them out the same way. A last
# good copy of the key store holds the keys, and one carried to another
# device would be put back there the first time a file is damaged.
EXCLUDES+=("--exclude=$APP_DIR/db-snapshots" "--exclude=$APP_DIR/.pre-restore")
EXCLUDES+=(--no-anchored "--exclude=*.last-good" "--exclude=*.corrupt-*")

# A consistent copy of every SQLite database, made with SQLite's online
# backup (Python's sqlite3, so no extra package is needed). The live file and
# its -wal, -shm and -journal companions are left out of the archive and the
# copy goes in under the same name, owner and mode. A database that cannot be
# copied this way is archived as it is, with a warning, as before.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pantryraider-backup.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SNAPSHOTS=()
if command -v python3 >/dev/null 2>&1; then
  while IFS= read -r -d '' rel; do
    SNAPSHOTS+=("$rel")
  done < <(cd "$PROJECT_DIR" && python3 -I -c '
import os, sqlite3, sys
stage, roots = sys.argv[1], sys.argv[2:]
skip_dirs = {"db-snapshots", ".pre-restore"}
for root in roots:
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name.endswith(".last-good") or ".corrupt-" in name:
                continue
            rel = os.path.join(here, name)
            try:
                with open(rel, "rb") as fh:
                    if fh.read(16) != b"SQLite format 3\0":
                        continue
            except OSError:
                continue
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                src = sqlite3.connect("file:" + os.path.abspath(rel) + "?mode=ro", uri=True, timeout=30)
                out = sqlite3.connect(dst)
                with out:
                    src.backup(out)
                out.close()
                src.close()
                st = os.stat(rel)
                os.chmod(dst, st.st_mode & 0o7777)
                try:
                    os.chown(dst, st.st_uid, st.st_gid)
                except OSError:
                    pass
            except Exception as exc:
                sys.stderr.write("WARNING: could not copy %s safely (%s); archiving it as it is.\n" % (rel, exc))
                try:
                    os.unlink(dst)
                except OSError:
                    pass
                continue
            sys.stdout.write(rel + "\0")
' "$STAGE" "${DIRS[@]}")
else
  echo "WARNING: python3 is not installed, so databases are archived as they are; stop the stack first for a guaranteed clean copy." >&2
fi
SNAP_EXCLUDES=(--anchored)
for rel in "${SNAPSHOTS[@]}"; do
  for suffix in "" -wal -shm -journal; do
    SNAP_EXCLUDES+=("--exclude=$rel$suffix")
  done
done

tar "${EXCLUDES[@]}" "${SNAP_EXCLUDES[@]}" -cf "$OUT.tar" -C "$PROJECT_DIR" "${DIRS[@]}"
if [ "${#SNAPSHOTS[@]}" -gt 0 ]; then
  tar -rf "$OUT.tar" -C "$STAGE" "${SNAPSHOTS[@]}"
fi
gzip -c "$OUT.tar" > "$OUT.partial"
rm -f "$OUT.tar"
# Never leave an archive behind that has no files in it. (The listing is
# read whole first: grep -q stopping early would fail tar under pipefail.)
listing="$(tar -tzf "$OUT.partial" 2>/dev/null || true)"
if ! grep -q '[^/]$' <<< "$listing"; then
  rm -f "$OUT.partial"
  echo "ERROR: the backup of ${DIRS[*]} in $PROJECT_DIR came out empty; nothing was saved." >&2
  exit 1
fi
mv -f "$OUT.partial" "$OUT"
echo "Backed up ${DIRS[*]} from $PROJECT_DIR -> $OUT ($(du -h "$OUT" | cut -f1))"

# Rotate: delete backups older than KEEP_DAYS, but always keep the newest
# MIN_KEEP, so a stretch with no backups never deletes the last good ones.
MIN_KEEP="${MIN_KEEP:-3}"
mapfile -t ALL_BACKUPS < <(find "$DEST" -maxdepth 1 -name 'foodassistant-*.tar.gz' | sort -r)
for f in "${ALL_BACKUPS[@]:$MIN_KEEP}"; do
  if [ -n "$(find "$f" -mtime "+$KEEP_DAYS")" ]; then rm -f "$f"; fi
done
echo "Rotation: backups older than $KEEP_DAYS days removed (the newest $MIN_KEEP are always kept)."
