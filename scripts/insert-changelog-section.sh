#!/usr/bin/env bash
# Insert a changelog section (read from stdin) into a CHANGELOG.md.
#
# Usage:
#   changelog-for-release.sh 0.6.0 ... | insert-changelog-section.sh CHANGELOG.md
#
# The file keeps one "# Changelog" header with whatever preamble sits under it,
# and its sections stay ordered highest version first. The section goes in by
# version rather than always at the top, so a release cut while an earlier
# release's PR is still open cannot land out of order.
#
# Inserting a version the file already carries is a no-op, so re-running the
# release job cannot stack a duplicate section. Empty or headless stdin is an
# error rather than a silent rewrite, which is what a failed generator upstream
# looks like.
#
# Creates the file if it does not exist yet.

set -euo pipefail

FILE="${1:-}"
if [[ -z "$FILE" ]]; then
  echo "usage: $0 <changelog-file> < section" >&2
  exit 64
fi

SECTION="$(cat)"
if [[ -z "$SECTION" ]]; then
  echo "$0: empty section on stdin" >&2
  exit 65
fi

VERSION=$(sed -n '1s/^## \[\([^]]*\)\].*/\1/p' <<<"$SECTION")
if [[ -z "$VERSION" ]]; then
  echo "$0: stdin does not start with a '## [version]' heading" >&2
  exit 65
fi

if [[ -f "$FILE" ]] && awk -v h="## [${VERSION}]" 'index($0, h) == 1 {found = 1; exit} END {exit !found}' "$FILE"; then
  echo "$0: ${FILE} already has ${VERSION}, leaving it alone" >&2
  exit 0
fi

# True when $1 sorts strictly below $2 under version ordering.
version_lt() {
  [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$1" ]]
}

PREAMBLE=""
SECTIONS=""
if [[ -f "$FILE" ]]; then
  PREAMBLE=$(awk '/^## \[/{exit} {print}' "$FILE")
  SECTIONS=$(awk 'f || /^## \[/{f = 1; print}' "$FILE")
fi
if [[ -z "${PREAMBLE//[[:space:]]/}" ]]; then
  PREAMBLE='# Changelog'
fi

# Line number of the first section this one outranks.
AT=""
while IFS=$'\t' read -r nr ver; do
  [[ -n "$nr" ]] || continue
  if version_lt "$ver" "$VERSION"; then
    AT="$nr"
    break
  fi
done < <(awk '/^## \[/ {
  ver = $0
  sub(/^## \[/, "", ver)
  sub(/\].*/, "", ver)
  print NR "\t" ver
}' <<<"$SECTIONS")

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

{
  printf '%s\n\n' "$PREAMBLE"
  if [[ -z "$SECTIONS" ]]; then
    printf '%s\n' "$SECTION"
  elif [[ -z "$AT" ]]; then
    # Every section already in the file outranks this one.
    printf '%s\n\n%s\n' "$SECTIONS" "$SECTION"
  elif [[ "$AT" == 1 ]]; then
    printf '%s\n\n%s\n' "$SECTION" "$SECTIONS"
  else
    printf '%s\n\n%s\n\n%s\n' \
      "$(head -n "$((AT - 1))" <<<"$SECTIONS")" "$SECTION" "$(tail -n "+${AT}" <<<"$SECTIONS")"
  fi
} >"$TMP"

# mktemp creates the file 0600; the changelog is meant to be readable.
chmod 644 "$TMP"
mv "$TMP" "$FILE"
