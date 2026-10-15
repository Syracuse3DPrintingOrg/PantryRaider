#!/usr/bin/env bash
# Put a release back on its tag when it was published as "untagged-...".
#
# release-image.yml creates the release as a draft, uploads the SD image and
# publishes it at the end; build-cub-firmware.yml attaches its firmware to the
# same release meanwhile. When the two overlap, the release can come out
# published but detached from its tag (tag_name "untagged-<hash>"), and
# sign-release.sh then cannot find it. Both workflows run this after their
# release step, so whichever finishes last leaves the release on its tag.
#
# usage: scripts/release-fix-tag.sh <tag>   (needs GH_TOKEN and GITHUB_REPOSITORY)
set -euo pipefail
TAG="${1:?usage: release-fix-tag.sh <tag>}"
REPO="${GITHUB_REPOSITORY:?}"
for _ in 1 2 3 4 5 6; do
  ids="$(gh api "repos/$REPO/releases?per_page=100" \
    --jq ".[] | select(.name == \"$TAG\" and .tag_name != \"$TAG\" and (.tag_name | startswith(\"untagged-\"))) | .id")"
  for id in $ids; do
    gh api --method PATCH "repos/$REPO/releases/$id" -f tag_name="$TAG" > /dev/null
    echo "Put release $id back on tag $TAG."
  done
  if gh api "repos/$REPO/releases/tags/$TAG" --jq .id > /dev/null 2>&1; then
    exit 0
  fi
  sleep 10
done
# Still a draft (the other workflow has not published it yet): that one runs
# this script too when it finishes.
echo "Release $TAG is not published yet; leaving it to the other workflow."
