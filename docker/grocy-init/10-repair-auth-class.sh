#!/usr/bin/env bash
# Pantry Raider: repair Grocy's config.php after the Grocy 4.7 namespace move.
# ==========================================================================
# Grocy 4.7.0 moved its auth middlewares into a Grocy\Middleware\Auth
# sub-namespace. A config.php written by an earlier Grocy still says
#   Setting('AUTH_CLASS', 'Grocy\Middleware\DefaultAuthMiddleware');
# and once the image updates, Grocy answers EVERY request (API included) with
# an "Invalid setting in config.php" page, so the inventory stops loading. The
# same move covers ReverseProxyAuthMiddleware and LdapAuthMiddleware. Going
# back is just as fatal: once 4.7 has written the new name, rolling the image
# back to 4.6 fails every request, because 4.6 has no Auth sub-namespace.
#
# This script runs INSIDE the LinuxServer Grocy container at every start: the
# stack mounts the folder it lives in at /custom-cont-init.d, and the image runs
# every executable file there as root before Grocy's own services come up. For
# the Default, ReverseProxy, and Ldap middlewares it rewrites that one line to
# match the Grocy that is actually installed:
#   - forward: the active AUTH_CLASS line (comments are ignored) names the old
#     class and this Grocy ships middleware/Auth/<Name>AuthMiddleware.php;
#   - back: the line names the Auth\ class, this Grocy has no middleware/Auth
#     folder, and it ships middleware/<Name>AuthMiddleware.php.
# Any other class is left alone. A timestamped backup
# (config.php.bak-<stamp>) is written first. The rewrite touches only the
# Setting('AUTH_CLASS', ...) line and goes through PHP's str_replace (literal,
# backslash-safe) with a literal sed fallback that also works with busybox
# sed, and the file keeps its inode, owner, and mode.
#
# It never fails the container start: every path exits 0 and says why.
# Idempotent: a config that already matches this Grocy is left alone.
#
# Env overrides (for tests, which run this on the host against temp files):
#   GROCY_CONFIG          path to config.php (default /config/data/config.php)
#   GROCY_MIDDLEWARE_DIR  Grocy's middleware folder, whose layout decides the
#                         direction (default /app/www/middleware)
#   GROCY_REPAIR_TOOL     "sed" forces the sed path even when php is present
#   DRY_RUN=1             decide and log, but write nothing
set -u

CONFIG="${GROCY_CONFIG:-/config/data/config.php}"
MIDDLEWARE_DIR="${GROCY_MIDDLEWARE_DIR:-/app/www/middleware}"
DRY_RUN="${DRY_RUN:-0}"
# The built-in middlewares Grocy 4.7 moved. Plain words only: each one is
# spliced into a class name and a sed expression below.
NAMES="Default ReverseProxy Ldap"

log() { echo "[grocy-repair] $*"; }

# Single quotes: the backslashes are literal.
old_class() { printf '%s' 'Grocy\Middleware\'"$1"'AuthMiddleware'; }
new_class() { printf '%s' 'Grocy\Middleware\Auth\'"$1"'AuthMiddleware'; }

# True when the line names the class exactly (the closing quote rules out a
# longer custom class that merely starts with the same name).
names_class() {
  case "$1" in
    *"$2'"*|*"$2\""*) return 0 ;;
  esac
  return 1
}

# The active AUTH_CLASS setting line (comment lines ignored), or nothing.
active_auth_line() {
  grep -F 'AUTH_CLASS' "$1" 2>/dev/null \
    | grep -v -E '^[[:space:]]*(//|#|\*|/\*)' \
    | grep -F 'Setting(' | head -n1
}

# rewrite <file> <name> <forward|back>: swap the old class name for the new
# one (forward) or the new for the old (back), in place.
rewrite() {
  local f="$1" name="$2" dir="$3" php_bin="" p rc tmp from to expr
  # Only Setting('AUTH_CLASS', ...) lines change; config.php also mentions the
  # class names in its comments, and those stay as Grocy wrote them.
  expr='/^[[:space:]]*Setting([[:space:]]*.AUTH_CLASS/'
  if [ "$dir" = "forward" ]; then
    from="$(old_class "$name")"; to="$(new_class "$name")"
    expr="$expr"'s/Grocy\\Middleware\\'"$name"'AuthMiddleware/Grocy\\Middleware\\Auth\\'"$name"'AuthMiddleware/g'
  else
    from="$(new_class "$name")"; to="$(old_class "$name")"
    expr="$expr"'s/Grocy\\Middleware\\Auth\\'"$name"'AuthMiddleware/Grocy\\Middleware\\'"$name"'AuthMiddleware/g'
  fi
  if [ "${GROCY_REPAIR_TOOL:-}" != "sed" ]; then
    for p in php php84 php83 php82 php81; do
      if command -v "$p" >/dev/null 2>&1; then php_bin="$p"; break; fi
    done
  fi
  if [ -n "$php_bin" ]; then
    # The values travel as environment variables, so no shell or PHP quoting
    # layer ever sees the backslashes. file_put_contents truncates and writes
    # the existing file, so the inode (owner, mode) is kept.
    GROCY_REPAIR_FILE="$f" GROCY_REPAIR_OLD="$from" GROCY_REPAIR_NEW="$to" \
      "$php_bin" -r '
        $f = getenv("GROCY_REPAIR_FILE");
        $s = file_get_contents($f);
        if ($s === false) { exit(2); }
        $o = getenv("GROCY_REPAIR_OLD"); $w = getenv("GROCY_REPAIR_NEW");
        $n = preg_replace_callback("/^[ \t]*Setting\([ \t]*.AUTH_CLASS.*\$/m",
          function ($m) use ($o, $w) { return str_replace($o, $w, $m[0]); }, $s);
        if ($n === null) { exit(5); }
        if ($n === $s) { exit(3); }
        exit(file_put_contents($f, $n) === false ? 4 : 0);'
    return $?
  fi
  # sed fallback, plain BRE that GNU and busybox sed read the same way: \\ is
  # one literal backslash on both sides, and the only thing spliced in is a
  # name from NAMES. Written to a temp file and copied back so the file keeps
  # its inode.
  tmp="$f.repair-tmp"
  sed "$expr" "$f" > "$tmp" && cat "$tmp" > "$f"
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

name=""
dir=""
for n in $NAMES; do
  if names_class "$line" "$(new_class "$n")"; then name="$n"; dir="back"; break; fi
  if names_class "$line" "$(old_class "$n")"; then name="$n"; dir="forward"; break; fi
done
if [ -z "$name" ]; then
  log "AUTH_CLASS is set to a custom class; leaving it alone"
  exit 0
fi

if [ "$dir" = "forward" ]; then
  from="$(old_class "$name")"; to="$(new_class "$name")"
  if [ ! -f "$MIDDLEWARE_DIR/Auth/${name}AuthMiddleware.php" ]; then
    log "this Grocy has no $MIDDLEWARE_DIR/Auth/${name}AuthMiddleware.php (older than 4.7), so AUTH_CLASS $from is still right"
    exit 0
  fi
else
  from="$(new_class "$name")"; to="$(old_class "$name")"
  if [ -d "$MIDDLEWARE_DIR/Auth" ] || [ ! -f "$MIDDLEWARE_DIR/${name}AuthMiddleware.php" ]; then
    log "AUTH_CLASS already names $from; nothing to repair"
    exit 0
  fi
fi

stamp="$(date +%Y%m%d-%H%M%S)"
backup="$CONFIG.bak-$stamp"
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would back up $CONFIG to $backup and change AUTH_CLASS from $from to $to"
  exit 0
fi
if ! cp -p "$CONFIG" "$backup"; then
  log "could not write the backup $backup; leaving config.php untouched"
  exit 0
fi
if ! rewrite "$CONFIG" "$name" "$dir"; then
  log "the rewrite failed; restoring config.php from $backup"
  cat "$backup" > "$CONFIG"
  exit 0
fi
after="$(active_auth_line "$CONFIG")"
if names_class "$after" "$to"; then
  if [ "$dir" = "forward" ]; then
    log "repaired: AUTH_CLASS now names $to (was $from; backup at $backup)"
  else
    log "repaired: AUTH_CLASS now names $to again, because this Grocy is older than 4.7 (was $from; backup at $backup)"
  fi
else
  log "the rewrite did not take; restoring config.php from $backup"
  cat "$backup" > "$CONFIG"
fi
exit 0
