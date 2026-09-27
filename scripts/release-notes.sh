#!/usr/bin/env bash
# Print the GitHub Release description for one version, from CHANGELOG.md.
#
# Usage:
#   scripts/release-notes.sh VERSION [CHANGELOG]
#
# VERSION may carry a leading "v" (v0.19.6 and 0.19.6 both work). Prints the
# body of the "## [VERSION]" section, up to the next "## [" header. A patch
# release without a header of its own gets the "## [Unreleased]" section
# instead. Either way a footer follows with the license and source location of
# the Bandit Cub firmware attached to the release.
#
# Both release workflows (release-image.yml and build-cub-firmware.yml) write
# the release body from this script, so whichever finishes last leaves the
# same text. Does not touch git or the network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ $# -lt 1 ] || [ -z "$1" ]; then
  echo "usage: release-notes.sh VERSION [CHANGELOG]" >&2
  exit 2
fi

VERSION="${1#v}"
CHANGELOG="${2:-$REPO_ROOT/CHANGELOG.md}"

if [ ! -f "$CHANGELOG" ]; then
  echo "release-notes: cannot find $CHANGELOG" >&2
  exit 1
fi

# The body under "## [NAME]" up to the next "## [" header, with leading and
# trailing blank lines trimmed. Prints nothing when the header is missing.
section() {
  awk -v want="## [$1]" '
    index($0, "## [") == 1 {
      if (found) exit
      if (index($0, want) == 1) { found = 1; next }
    }
    found { lines[++n] = $0 }
    END {
      first = 1
      while (first <= n && lines[first] ~ /^[[:space:]]*$/) first++
      last = n
      while (last >= first && lines[last] ~ /^[[:space:]]*$/) last--
      for (i = first; i <= last; i++) print lines[i]
    }
  ' "$CHANGELOG"
}

BODY="$(section "$VERSION")"
if [ -z "$BODY" ]; then
  BODY="$(section "Unreleased")"
fi

if [ -n "$BODY" ]; then
  printf '%s\n\n' "$BODY"
fi
printf '%s\n' "Bandit Cub firmware in this release is GPL-3.0-or-later. Complete source: https://github.com/Syracuse3DPrintingOrg/PantryRaider/tree/v${VERSION}/esphome"
