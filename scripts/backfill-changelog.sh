#!/usr/bin/env bash
# Regenerate CHANGELOG.md from every release tag in the repository.
#
# Usage:
#   scripts/backfill-changelog.sh [--dry-run] [--force]
#
# This rewrites the whole file from git history. Anything hand-written into
# CHANGELOG.md is lost, so the script refuses to run while the file has
# uncommitted changes: --force overrides that, --dry-run prints the result to
# stdout and writes nothing.
#
# Release builds do not call this: they insert one section for the tag being
# released.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$(git rev-parse --show-toplevel)"

file=CHANGELOG.md
dry_run=0
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
  --dry-run)
    dry_run=1
    shift
    ;;
  --force)
    force=1
    shift
    ;;
  *)
    echo "usage: $0 [--dry-run] [--force]" >&2
    exit 64
    ;;
  esac
done

if [[ $dry_run -eq 0 && $force -eq 0 && -f "$file" && -n "$(git status --porcelain -- "$file")" ]]; then
  echo "$0: $file has uncommitted changes; commit them or pass --force" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

prev=""
for tag in $(git tag -l '[0-9]*' | sort -V); do
  if [[ -z "$prev" ]]; then
    # First release: no predecessor, so walk from the root commit.
    "$here/changelog-for-release.sh" --from-root "$tag" "" "$tag"
  else
    "$here/changelog-for-release.sh" "$tag" "$prev" "$tag"
  fi | "$here/insert-changelog-section.sh" "$tmp"
  prev="$tag"
done

if [[ -z "$prev" ]]; then
  echo "no release tags found" >&2
  exit 1
fi

if [[ $dry_run -eq 1 ]]; then
  cat "$tmp"
  exit 0
fi

cp "$tmp" "$file"
chmod 644 "$file"
echo "wrote $file"
