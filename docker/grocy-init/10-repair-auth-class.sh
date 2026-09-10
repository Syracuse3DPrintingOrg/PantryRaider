#!/usr/bin/env bash
# Pantry Raider: repair Grocy's config.php after the Grocy 4.7 namespace move.
# ==========================================================================
# Grocy 4.7.0 moved its auth middlewares into a Grocy\Middleware\Auth
# sub-namespace. A config.php written by an earlier Grocy still says
#   Setting('AUTH_CLASS', 'Grocy\Middleware\DefaultAuthMiddleware');
# and once the image updates, Grocy answers EVERY request (API included) with
# an "Invalid setting in config.php" page, so the inventory stops loading.
#
# This script runs INSIDE the LinuxServer Grocy container at every start: the
# stack mounts the folder it lives in at /custom-cont-init.d, and the image runs
# every executable file there as root before Grocy's own services come up. It
# rewrites that one line, and only when both of these hold:
#   - the active AUTH_CLASS line names the old class (comments are ignored), and
#   - this Grocy actually ships the new class file, so the edit is the right one.
# A timestamped backup (config.php.bak-<stamp>) is written first. The rewrite
# goes through PHP's str_replace (literal, backslash-safe) with a literal sed
# fallback, and the file keeps its inode, owner, and mode.
#
# It never fails the container start: every path exits 0 and says why.
# Idempotent: a repaired config is recognised and left alone.
#
# Env overrides (for tests, which run this on the host against temp files):
#   GROCY_CONFIG          path to config.php (default /config/data/config.php)
#   GROCY_NEW_CLASS_FILE  the 4.7 class file whose presence gates the edit
#   GROCY_REPAIR_TOOL     "sed" forces the sed path even when php is present
#   DRY_RUN=1             decide and log, but write nothing
set -u

CONFIG="${GROCY_CONFIG:-/config/data/config.php}"
NEW_CLASS_FILE="${GROCY_NEW_CLASS_FILE:-/app/www/middleware/Auth/DefaultAuthMiddleware.php}"
DRY_RUN="${DRY_RUN:-0}"
# Single quotes: the backslashes are literal.
OLD_CLASS='Grocy\Middleware\DefaultAuthMiddleware'
NEW_CLASS='Grocy\Middleware\Auth\DefaultAuthMiddleware'

log() { echo "[grocy-repair] $*"; }

# The active AUTH_CLASS setting line (comment lines ignored), or nothing.
active_auth_line() {
  grep -F 'AUTH_CLASS' "$1" 2>/dev/null \
    | grep -v -E '^[[:space:]]*(//|#|\*|/\*)' \
    | grep -F 'Setting(' | head -n1
}

# Rewrite the old class name to the new one, in place.
rewrite() {
  local f="$1" php_bin="" p rc tmp
  if [ "${GROCY_REPAIR_TOOL:-}" != "sed" ]; then
    for p in php php83 php82 php81; do
      if command -v "$p" >/dev/null 2>&1; then php_bin="$p"; break; fi
    done
  fi
  if [ -n "$php_bin" ]; then
    # The values travel as environment variables, so no shell or PHP quoting
    # layer ever sees the backslashes. file_put_contents truncates and writes
    # the existing file, so the inode (owner, mode) is kept.
    GROCY_REPAIR_FILE="$f" GROCY_REPAIR_OLD="$OLD_CLASS" GROCY_REPAIR_NEW="$NEW_CLASS" \
      "$php_bin" -r '
        $f = getenv("GROCY_REPAIR_FILE");
        $s = file_get_contents($f);
        if ($s === false) { exit(2); }
        $n = str_replace(getenv("GROCY_REPAIR_OLD"), getenv("GROCY_REPAIR_NEW"), $s);
        if ($n === $s) { exit(3); }
        exit(file_put_contents($f, $n) === false ? 4 : 0);'
    return $?
  fi
  # sed fallback. The expression is a single-quoted literal: \\ is one
  # backslash on both sides, and no variable is ever interpolated into it.
  # Written to a temp file and copied back so the file keeps its inode.
  tmp="$f.repair-tmp"
  sed 's/Grocy\\Middleware\\DefaultAuthMiddleware/Grocy\\Middleware\\Auth\\DefaultAuthMiddleware/g' "$f" > "$tmp" \
    && cat "$tmp" > "$f"
  rc=$?
  rm -f "$tmp"
  return $rc
}

if [ ! -f "$CONFIG" ]; then
  log "no $CONFIG yet (fresh install); nothing to repair"
  exit 0
fi
line="$(active_auth_line "$CONFIG")"
if [ -z "$line" ]; then
  log "config.php sets no AUTH_CLASS, so Grocy uses its own default; nothing to repair"
  exit 0
fi
case "$line" in
  *"$NEW_CLASS"*)
    log "AUTH_CLASS already names $NEW_CLASS; nothing to repair"
    exit 0 ;;
  *"$OLD_CLASS"*)
    : ;;
  *)
    log "AUTH_CLASS is set to a custom class; leaving it alone"
    exit 0 ;;
esac
if [ ! -f "$NEW_CLASS_FILE" ]; then
  log "this Grocy has no $NEW_CLASS_FILE (older than 4.7), so the current AUTH_CLASS is still right"
  exit 0
fi

stamp="$(date +%Y%m%d-%H%M%S)"
backup="$CONFIG.bak-$stamp"
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would back up $CONFIG to $backup and change AUTH_CLASS to $NEW_CLASS"
  exit 0
fi
if ! cp -p "$CONFIG" "$backup"; then
  log "could not write the backup $backup; leaving config.php untouched"
  exit 0
fi
if ! rewrite "$CONFIG"; then
  log "the rewrite failed; restoring config.php from $backup"
  cat "$backup" > "$CONFIG"
  exit 0
fi
after="$(active_auth_line "$CONFIG")"
case "$after" in
  *"$NEW_CLASS"*)
    log "repaired: AUTH_CLASS now names $NEW_CLASS (backup at $backup)" ;;
  *)
    log "the rewrite did not take; restoring config.php from $backup"
    cat "$backup" > "$CONFIG" ;;
esac
exit 0
