#!/bin/sh
# Checks out a commit on origin/main, and only such a commit. Called by
# setup-instance.sh; prints the full sha it checked out.
#
#   checkout-ref.sh <checkout dir> <main | commit sha>
#
# Only the remote's main branch is fetched, and never a tag: git looks up a
# short name such as "origin/main" in refs/tags before refs/remotes, so a
# pushed tag of that name could otherwise stand in for main and pass the
# ancestry check. Every name below is a full ref for the same reason.
set -eu

SRC=$1
REF=$2
MAIN=refs/remotes/origin/main

fail() {
    echo "checkout: $1" >&2
    exit 1
}

case "$REF" in
    main) ;;
    *) printf '%s' "$REF" | grep -Eqx '[0-9a-f]{7,40}' || fail "ref must be main or a commit sha" ;;
esac
[ -d "$SRC/.git" ] || fail "$SRC is not a git checkout"

git -C "$SRC" config remote.origin.tagOpt --no-tags
# Drop every local tag, including any fetched before this rule existed.
git -C "$SRC" for-each-ref --format='%(refname)' refs/tags | while IFS= read -r t; do
    git -C "$SRC" update-ref -d "$t"
done
git -C "$SRC" fetch --quiet --no-tags --prune origin "+refs/heads/main:$MAIN"

if [ "$REF" = main ]; then
    sha=$(git -C "$SRC" rev-parse --verify --quiet "$MAIN^{commit}") || fail "origin/main is missing"
else
    sha=$(git -C "$SRC" rev-parse --verify --quiet "$REF^{commit}") || fail "unknown commit"
fi
git -C "$SRC" merge-base --is-ancestor "$sha" "$MAIN" || fail "commit is not on origin/main"
git -C "$SRC" checkout --quiet --force --detach "$sha"
git -C "$SRC" clean --quiet -fdx
echo "$sha"
