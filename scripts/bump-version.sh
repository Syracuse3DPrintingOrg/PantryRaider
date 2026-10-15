#!/usr/bin/env bash
# Bump the single source-of-truth app version in service/app/config.py.
#
# Usage:
#   scripts/bump-version.sh [patch|minor|major]   # bump and write (default: patch)
#   scripts/bump-version.sh --current             # print current version, do not change
#   scripts/bump-version.sh --set X.Y.Z           # set an explicit version
#
# Prints "OLD -> NEW" on a bump. Exits non-zero if the version line can't be found
# or the requested version is not valid semver. Does not touch git.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO_ROOT/service/app/config.py"

if [ ! -f "$CONFIG" ]; then
  echo "bump-version: cannot find $CONFIG" >&2
  exit 1
fi

current() {
  # Extract X.Y.Z from: APP_VERSION = "X.Y.Z"
  sed -n 's/^APP_VERSION *= *"\([0-9]\+\.[0-9]\+\.[0-9]\+\)".*/\1/p' "$CONFIG" | head -1
}

CUR="$(current)"
if [ -z "$CUR" ]; then
  echo "bump-version: no 'APP_VERSION = \"X.Y.Z\"' line in $CONFIG" >&2
  exit 1
fi

write() {
  local new="$1"
  # Portable in-place edit (GNU and BSD sed differ on -i).
  if sed --version >/dev/null 2>&1; then
    sed -i "s/^APP_VERSION *= *\"[0-9]\+\.[0-9]\+\.[0-9]\+\"/APP_VERSION = \"$new\"/" "$CONFIG"
  else
    sed -i '' "s/^APP_VERSION *= *\"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/APP_VERSION = \"$new\"/" "$CONFIG"
  fi
}

valid_semver() {
  printf '%s' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'
}

ARG="${1:-patch}"

case "$ARG" in
  --current)
    echo "$CUR"
    exit 0
    ;;
  --set)
    NEW="${2:-}"
    if ! valid_semver "$NEW"; then
      echo "bump-version: --set requires a valid X.Y.Z version" >&2
      exit 1
    fi
    ;;
  patch|minor|major)
    IFS='.' read -r MA MI PA <<EOF
$CUR
EOF
    case "$ARG" in
      patch) PA=$((PA + 1)) ;;
      minor) MI=$((MI + 1)); PA=0 ;;
      major) MA=$((MA + 1)); MI=0; PA=0 ;;
    esac
    NEW="$MA.$MI.$PA"
    ;;
  *)
    echo "bump-version: unknown argument '$ARG' (use patch|minor|major|--set X.Y.Z|--current)" >&2
    exit 1
    ;;
esac

if [ "$NEW" = "$CUR" ]; then
  echo "$CUR"
  exit 0
fi

write "$NEW"
echo "$CUR -> $NEW"
