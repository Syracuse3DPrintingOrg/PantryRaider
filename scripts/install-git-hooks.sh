#!/usr/bin/env bash
# Install the auto-version-bump pre-commit hook so every commit bumps at least
# the patch number. Idempotent: safe to re-run (for example after a beads hook
# update rewrites the managed hook file).
#
# It chains onto the ACTIVE pre-commit hook rather than replacing it, so it
# coexists with the beads integration (which sets core.hooksPath to .beads/hooks).
# Our block is delimited by markers and re-appended only if missing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Honor core.hooksPath (beads sets it); fall back to .git/hooks.
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
  exit 0
fi

if [ ! -f "$HOOK" ]; then
  printf '%s\n' '#!/usr/bin/env sh' > "$HOOK"
fi

# Ensure the hook is executable and append our block.
chmod +x "$HOOK"
printf '\n' >> "$HOOK"
block >> "$HOOK"
chmod +x "$REPO_ROOT/scripts/git-hooks/pre-commit-version" "$REPO_ROOT/scripts/bump-version.sh"

echo "installed version bump hook into $HOOK"
