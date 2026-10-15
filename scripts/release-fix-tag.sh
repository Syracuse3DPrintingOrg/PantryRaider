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
# The overlap can also leave two releases for one tag: a published one with
# nothing attached and a draft holding the files (the second creator's
# "finalize" then fails with already_exists). When that happens the empty
# published release is removed and the draft with the files is published.
#
# usage: scripts/release-fix-tag.sh <tag> [required-asset-suffix]
#   (needs GH_TOKEN and GITHUB_REPOSITORY). With a suffix, the run fails unless
#   the published release ends up holding an asset ending in it.
set -euo pipefail
TAG="${1:?usage: release-fix-tag.sh <tag> [required-asset-suffix]}"
NEED="${2:-}"
REPO="${GITHUB_REPOSITORY:?}"

merge_duplicate_draft() {
  local drafts pub pub_assets id
  drafts="$(gh api "repos/$REPO/releases?per_page=100" \
    --jq ".[] | select(.draft and .name == \"$TAG\" and (.assets | length) > 0) | .id")"
  [ -n "$drafts" ] || return 0
  pub="$(gh api "repos/$REPO/releases/tags/$TAG" --jq .id 2>/dev/null || true)"
  if [ -n "$pub" ]; then
    pub_assets="$(gh api "repos/$REPO/releases/$pub" --jq '.assets | length')"
    [ "$pub_assets" = 0 ] || return 0
    gh api --method DELETE "repos/$REPO/releases/$pub" > /dev/null
    echo "Removed the empty duplicate release $pub for $TAG."
  fi
  id="$(printf '%s\n' "$drafts" | head -1)"
  gh api --method PATCH "repos/$REPO/releases/$id" -f tag_name="$TAG" \
    -F draft=false -f make_latest=true > /dev/null
  echo "Published release $id, which holds the files, on tag $TAG."
}

for _ in 1 2 3 4 5 6; do
  merge_duplicate_draft
  ids="$(gh api "repos/$REPO/releases?per_page=100" \
    --jq ".[] | select(.name == \"$TAG\" and .tag_name != \"$TAG\" and (.tag_name | startswith(\"untagged-\"))) | .id")"
  for id in $ids; do
    gh api --method PATCH "repos/$REPO/releases/$id" -f tag_name="$TAG" > /dev/null
    echo "Put release $id back on tag $TAG."
  done
  if gh api "repos/$REPO/releases/tags/$TAG" --jq .id > /dev/null 2>&1; then
    if [ -z "$NEED" ] || gh api "repos/$REPO/releases/tags/$TAG" \
        --jq "[.assets[].name | select(endswith(\"$NEED\"))] | length" | grep -qv '^0$'; then
      exit 0
    fi
  fi
  sleep 10
done
# Still a draft (the other workflow has not published it yet): that one runs
# this script too when it finishes.
if [ -n "$NEED" ]; then
  echo "Release $TAG has no file ending in $NEED after the retries." >&2
  exit 1
fi
echo "Release $TAG is not published yet; leaving it to the other workflow."
