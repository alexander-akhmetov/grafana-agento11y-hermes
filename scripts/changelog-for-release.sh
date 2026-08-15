#!/usr/bin/env bash
# Generate the markdown changelog section for one release.
#
# Usage:
#   changelog-for-release.sh [options] <version> [<from-ref>] [<to-ref>]
#
# Options:
#   --from-root   start at the root commit, for a first release with no predecessor
#   --no-heading  print the bullets only, for release notes whose title carries the version
#   --hashes      append the short commit hash to every bullet
#
# Examples:
#   scripts/changelog-for-release.sh 0.6.0              # since the previous tag, up to HEAD
#   scripts/changelog-for-release.sh 0.6.0 "" 0.6.0     # since the previous tag, up to the tag
#   scripts/changelog-for-release.sh 0.6.0 0.5.0 0.6.0  # explicit range
#   scripts/changelog-for-release.sh --from-root 0.1.0 "" 0.1.0
#
# This repository writes plain imperative commit subjects, not conventional
# commits, so there is nothing to group by. Every non-merge subject becomes a
# bullet, except the release bot's own commits, and dependency bumps move to a
# trailing section.
#
# Tags carry no v prefix here, because hatch-vcs reads the version straight off
# the tag name.
#
# The date is the target commit's date rather than today's, and the range ends
# at <to-ref>, so two checkouts emit the same bytes for the same tag.
#
# Environment:
#   CHANGELOG_DATE      section date (YYYY-MM-DD). Default: the <to-ref> commit date.
#   CHANGELOG_REPO_URL  base URL for compare links. Default: $GITHUB_SERVER_URL/
#                       $GITHUB_REPOSITORY, else the origin remote. With no URL
#                       the heading carries no compare link.
#
# Prints the section to stdout and touches no files. See
# insert-changelog-section.sh for writing it into CHANGELOG.md.

set -euo pipefail

# The changelog PR's own commit lands inside the next release's range. It is
# bookkeeping, not a change, so it never becomes a bullet.
BOT_SUBJECTS='^- Release [0-9]+\.[0-9]+\.[0-9]+[^:]*: changelog'

# Dependabot's subject shapes: a single bump, and the grouped form the update
# groups in .github/dependabot.yml produce. A bare "^- Bump " prefix would also
# swallow prose such as "Bump the default sample rate to 0.5".
DEPENDENCY_SUBJECTS='^- Bump ([^[:space:]]+ from [^[:space:]]+ to [^[:space:]]+|the [^[:space:]]+ group)'

usage() {
  echo "usage: $0 [--from-root] [--no-heading] [--hashes] <version> [<from-ref>] [<to-ref>]" >&2
}

from_root=0
heading=1
hashes=0
while [[ $# -gt 0 ]]; do
  case "$1" in
  --from-root)
    from_root=1
    shift
    ;;
  --no-heading)
    heading=0
    shift
    ;;
  --hashes)
    hashes=1
    shift
    ;;
  --)
    shift
    break
    ;;
  -*)
    echo "$0: unknown option: $1" >&2
    usage
    exit 64
    ;;
  *) break ;;
  esac
done

NEW="${1:-}"
if [[ -z "$NEW" ]]; then
  usage
  exit 64
fi

# Where the range starts. The fallback takes the highest tag strictly below this
# release rather than the newest tag in the repository, so a backport cut after
# a higher tag still compares against its own predecessor.
if [[ $from_root -eq 1 ]]; then
  if [[ -n "${2:-}" ]]; then
    echo "$0: --from-root and a <from-ref> are mutually exclusive" >&2
    exit 64
  fi
  PREV=""
elif [[ -n "${2:-}" ]]; then
  PREV="$2"
else
  PREV=$(git tag -l '[0-9]*' | sort -V | awk -v cur="$NEW" '$0 == cur {exit} {last=$0} END {print last}')
fi

TO="${3:-HEAD}"
DATE="${CHANGELOG_DATE:-$(git log -1 --format=%cd --date=short "$TO")}"

# Follow the remote rather than a baked-in constant, so a fork or a move to the
# Grafana org does not leave every link pointing at the old repository.
repo_url() {
  if [[ -n "${CHANGELOG_REPO_URL:-}" ]]; then
    printf '%s' "${CHANGELOG_REPO_URL%/}"
    return
  fi
  if [[ -n "${GITHUB_SERVER_URL:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
    printf '%s/%s' "${GITHUB_SERVER_URL%/}" "$GITHUB_REPOSITORY"
    return
  fi
  local url
  url=$(git remote get-url origin 2>/dev/null) || return 0
  url="${url%.git}"
  url="${url#ssh://}"
  url="${url#git+ssh://}"
  if [[ "$url" == git@*:* ]]; then
    # scp-style: git@host:owner/repo
    local host="${url%%:*}"
    printf 'https://%s/%s' "${host#git@}" "${url#*:}"
  elif [[ "$url" == git@* ]]; then
    printf 'https://%s' "${url#git@}"
  else
    printf '%s' "$url"
  fi
}

# The right-hand side of a compare link has to be a ref the remote knows. That
# is usually the tag being released, but when the range ends at HEAD or at a
# branch tip the tag does not exist yet, so link the commit instead.
link_ref() {
  local ref="$1"
  if git show-ref --verify --quiet "refs/tags/$ref" || git show-ref --verify --quiet "refs/heads/$ref"; then
    printf '%s' "$ref"
  else
    git rev-parse --short "$ref"
  fi
}

REPO_URL="$(repo_url)"

if [[ -n "$PREV" ]]; then
  RANGE="${PREV}..${TO}"
else
  RANGE="$TO"
fi

if [[ $heading -eq 1 ]]; then
  if [[ -n "$PREV" && -n "$REPO_URL" ]]; then
    printf '## [%s](%s/compare/%s...%s) - %s\n\n' "$NEW" "$REPO_URL" "$PREV" "$(link_ref "$TO")" "$DATE"
  else
    printf '## [%s] - %s\n\n' "$NEW" "$DATE"
  fi
fi

FORMAT='- %s'
if [[ $hashes -eq 1 ]]; then
  FORMAT='- %s (%h)'
fi

COMMITS=$(git log --no-merges --pretty="format:$FORMAT" "$RANGE" --)
COMMITS=$(grep -vE "$BOT_SUBJECTS" <<<"$COMMITS" || true)

if [[ -z "$COMMITS" ]]; then
  printf '_No changes._\n'
  exit 0
fi

CHANGES=$(grep -vE "$DEPENDENCY_SUBJECTS" <<<"$COMMITS" || true)
DEPS=$(grep -E "$DEPENDENCY_SUBJECTS" <<<"$COMMITS" || true)

if [[ -n "$CHANGES" ]]; then
  printf '%s\n' "$CHANGES"
fi

if [[ -n "$DEPS" ]]; then
  if [[ -n "$CHANGES" ]]; then
    printf '\n'
  fi
  printf '### Dependencies\n\n%s\n' "$DEPS"
fi
