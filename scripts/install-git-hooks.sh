#!/usr/bin/env bash
# Install this repo's git hooks: the auto-version-bump pre-commit hook, so every
# commit bumps at least the patch number, and the pre-push guard that refuses a
# plain push to the public repo. Idempotent: safe to re-run.
#
# It chains onto the ACTIVE hooks rather than replacing them, so it coexists
# with any other managed hook. Each block is delimited by markers and added
# only if missing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Honor core.hooksPath if set; fall back to .git/hooks.
HOOKS_DIR="$(git -C "$REPO_ROOT" config core.hooksPath 2>/dev/null || true)"
if [ -z "$HOOKS_DIR" ]; then
  HOOKS_DIR="$(git -C "$REPO_ROOT" rev-parse --git-path hooks)"
fi
# Resolve relative hooksPath against the repo root.
case "$HOOKS_DIR" in
  /*) : ;;
  *) HOOKS_DIR="$REPO_ROOT/$HOOKS_DIR" ;;
esac
mkdir -p "$HOOKS_DIR"

# --- pre-commit: version bump, appended after anything already there ---
HOOK="$HOOKS_DIR/pre-commit"
BEGIN="# --- BEGIN FOODASSISTANT VERSION BUMP ---"
END="# --- END FOODASSISTANT VERSION BUMP ---"

block() {
  cat <<EOF
$BEGIN
# Managed by scripts/install-git-hooks.sh. Auto-bumps the patch version.
"\$(git rev-parse --show-toplevel)/scripts/git-hooks/pre-commit-version" "\$@" || exit \$?
$END
EOF
}

if [ -f "$HOOK" ] && grep -qF "$BEGIN" "$HOOK"; then
  echo "version hook already installed in $HOOK"
else
  if [ ! -f "$HOOK" ]; then
    printf '%s\n' '#!/usr/bin/env sh' > "$HOOK"
  fi
  printf '\n' >> "$HOOK"
  block >> "$HOOK"
  echo "installed version bump hook into $HOOK"
fi
chmod +x "$HOOK"
chmod +x "$REPO_ROOT/scripts/git-hooks/pre-commit-version" "$REPO_ROOT/scripts/bump-version.sh"

# --- pre-push: public repo guard, placed first so a refused push stops
# before any other pre-push work runs ---
PUSH_HOOK="$HOOKS_DIR/pre-push"
PUSH_BEGIN="# --- BEGIN PANTRY RAIDER PUBLIC PUSH GUARD ---"
PUSH_END="# --- END PANTRY RAIDER PUBLIC PUSH GUARD ---"

push_block() {
  cat <<EOF
$PUSH_BEGIN
# Managed by scripts/install-git-hooks.sh. Refuses a plain push to the public repo.
_pr_guard="\$(git rev-parse --show-toplevel)/scripts/git-hooks/pre-push-public-guard"
if [ -x "\$_pr_guard" ]; then
  # The guard reads the pushed refs on stdin; keep a copy for the hooks below.
  _pr_refs="\$(mktemp)" || exit 1
  cat > "\$_pr_refs"
  "\$_pr_guard" "\$@" < "\$_pr_refs" || { _pr_rc=\$?; rm -f "\$_pr_refs"; exit \$_pr_rc; }
  exec < "\$_pr_refs"
  rm -f "\$_pr_refs"
fi
$PUSH_END
EOF
}

if [ -f "$PUSH_HOOK" ] && grep -qF "$PUSH_BEGIN" "$PUSH_HOOK"; then
  echo "public push guard already installed in $PUSH_HOOK"
else
  tmp="$(mktemp "$HOOKS_DIR/.pre-push.XXXXXX")"
  if [ -f "$PUSH_HOOK" ] && head -n 1 "$PUSH_HOOK" | grep -q '^#!'; then
    { head -n 1 "$PUSH_HOOK"; push_block; tail -n +2 "$PUSH_HOOK"; } > "$tmp"
  else
    { printf '%s\n' '#!/usr/bin/env sh'; push_block
      if [ -f "$PUSH_HOOK" ]; then cat "$PUSH_HOOK"; fi; } > "$tmp"
  fi
  # Write through the existing file so its mode and ownership stay as they are.
  cat "$tmp" > "$PUSH_HOOK"
  rm -f "$tmp"
  echo "installed public push guard into $PUSH_HOOK"
fi
chmod +x "$PUSH_HOOK"
chmod +x "$REPO_ROOT/scripts/git-hooks/pre-push-public-guard"
