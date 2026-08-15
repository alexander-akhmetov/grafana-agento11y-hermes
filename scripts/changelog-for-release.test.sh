#!/usr/bin/env bash
# Tests for the changelog scripts: changelog-for-release.sh,
# insert-changelog-section.sh and backfill-changelog.sh. Exits non-zero on any
# failure. There is no `set -e`: every assertion runs so one broken case does
# not hide the rest.

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
GEN="${DIR}/changelog-for-release.sh"
INSERT="${DIR}/insert-changelog-section.sh"
BACKFILL="${DIR}/backfill-changelog.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

REMOTE=https://github.com/alexander-akhmetov/grafana-agento11y-hermes

# A stray GITHUB_* in the caller's environment would change the compare links.
unset GITHUB_SERVER_URL GITHUB_REPOSITORY CHANGELOG_REPO_URL CHANGELOG_DATE

fail=0
pass=0

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if grep -Fq -- "$needle" <<<"$haystack"; then
    pass=$((pass + 1))
  else
    echo "FAIL ${desc}: missing '${needle}'"
    echo "--- output ---"
    printf '%s\n' "$haystack"
    echo "--------------"
    fail=$((fail + 1))
  fi
}

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if grep -Fq -- "$needle" <<<"$haystack"; then
    echo "FAIL ${desc}: unexpected '${needle}'"
    echo "--- output ---"
    printf '%s\n' "$haystack"
    echo "--------------"
    fail=$((fail + 1))
  else
    pass=$((pass + 1))
  fi
}

assert_matches() {
  local desc="$1" pattern="$2" haystack="$3"
  if grep -Eq -- "$pattern" <<<"$haystack"; then
    pass=$((pass + 1))
  else
    echo "FAIL ${desc}: nothing matches /${pattern}/"
    echo "--- output ---"
    printf '%s\n' "$haystack"
    echo "--------------"
    fail=$((fail + 1))
  fi
}

assert_count() {
  local desc="$1" needle="$2" want="$3" haystack="$4" got
  got=$(grep -F -c -- "$needle" <<<"$haystack")
  if [[ "$got" == "$want" ]]; then
    pass=$((pass + 1))
  else
    echo "FAIL ${desc}: '${needle}' appeared ${got} times, want ${want}"
    echo "--- output ---"
    printf '%s\n' "$haystack"
    echo "--------------"
    fail=$((fail + 1))
  fi
}

assert_eq() {
  local desc="$1" want="$2" got="$3"
  if [[ "$want" == "$got" ]]; then
    pass=$((pass + 1))
  else
    echo "FAIL ${desc}"
    echo "--- want ---"
    printf '%s\n' "$want"
    echo "--- got ----"
    printf '%s\n' "$got"
    echo "------------"
    fail=$((fail + 1))
  fi
}

# BSD stat and GNU stat spell the mode flag differently.
file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

commit() {
  local date="$1" msg="$2"
  printf '%s\n' "$msg" >>file.txt
  git add file.txt
  GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date" git commit -q -m "$msg"
}

new_repo() {
  mkdir -p "$1"
  cd "$1" || exit 1
  git init -q -b main
  git config user.name 'Test User'
  git config user.email 'test@example.com'
  git config commit.gpgsign false
  git config tag.gpgsign false
}

new_repo "${TMP}/repo"
git remote add origin "${REMOTE}.git"

commit '2026-03-11T10:00:00+00:00' 'Add the hook handlers'
git tag 0.5.0
commit '2026-08-08T10:00:00+00:00' 'Record tool executions'
commit '2026-08-08T11:00:00+00:00' 'Bump ruff from 0.16.3 to 0.16.4 (#44)'
git tag 0.27.0
commit '2026-08-10T09:00:00+00:00' 'Install the MeterProvider only when the global is a proxy'
git tag 0.28.0
commit '2026-08-11T09:00:00+00:00' 'Land an unreleased change'

# First release: no predecessor, so no compare link and the range starts at the
# root commit.
out=$("$GEN" --from-root 0.5.0 "" 0.5.0)
assert_contains 'first release heading is plain' '## [0.5.0] - 2026-03-11' "$out"
assert_not_contains 'first release has no compare link' '/compare/' "$out"
assert_contains 'first release walks from the root commit' '- Add the hook handlers' "$out"

"$GEN" --from-root 0.5.0 0.27.0 0.5.0 >/dev/null 2>&1
assert_eq '--from-root with a from-ref is a usage error' 64 "$?"

# Dependency bumps move to their own trailing section; everything else stays in
# the flat leading list.
out=$("$GEN" 0.27.0 "" 0.27.0)
assert_contains 'compare-linked heading' \
  "## [0.27.0](${REMOTE}/compare/0.5.0...0.27.0) - 2026-08-08" "$out"
assert_contains 'plain subject listed' '- Record tool executions' "$out"
assert_contains 'dependency bump listed' '- Bump ruff from 0.16.3 to 0.16.4 (#44)' "$out"
assert_not_contains 'previous release excluded' '- Add the hook handlers' "$out"
deps=$(awk '/^### Dependencies/{seen=1} seen' <<<"$out")
assert_contains 'bump sits under Dependencies' '- Bump ruff from 0.16.3' "$deps"
assert_not_contains 'plain subject stays out of Dependencies' '- Record tool executions' "$deps"

# An explicit end ref pins the range even when the checkout is ahead of the tag,
# so two checkouts cannot disagree.
from_main=$("$GEN" 0.28.0 "" 0.28.0)
assert_contains 'released commit listed' '- Install the MeterProvider only when the global is a proxy' "$from_main"
assert_not_contains 'end ref excludes later commits' 'Land an unreleased change' "$from_main"
git checkout -q --detach 0.28.0
from_tag=$("$GEN" 0.28.0 "" 0.28.0)
git checkout -q main
assert_eq 'same section from two checkouts' "$from_main" "$from_tag"

# The date comes from the target commit, not from the clock.
assert_contains 'date comes from the tagged commit' ' - 2026-08-10' "$from_main"
out=$(CHANGELOG_DATE=2030-01-02 "$GEN" 0.28.0 "" 0.28.0)
assert_contains 'CHANGELOG_DATE overrides the commit date' ' - 2030-01-02' "$out"

# --no-heading is what the release notes use, since their title carries the
# version. --hashes puts the short hash back on every bullet.
out=$("$GEN" --no-heading 0.28.0 "" 0.28.0)
assert_not_contains 'no-heading drops the heading' '## [0.28.0]' "$out"
assert_contains 'no-heading keeps the bullets' '- Install the MeterProvider only when the global is a proxy' "$out"
out=$("$GEN" --hashes 0.28.0 "" 0.28.0)
assert_matches 'hashes are appended to bullets' \
  '^- Install the MeterProvider only when the global is a proxy \([0-9a-f]{7,}\)$' "$out"
assert_not_contains 'hashes are off by default' '- Install the MeterProvider only when the global is a proxy (' "$from_main"

# The right side of the compare link names the end of the range. It is the tag
# in a release, but a tag that does not exist yet would 404.
head_short=$(git rev-parse --short HEAD)
out=$("$GEN" 9.9.9 0.28.0 HEAD)
assert_contains 'compare link ends at the to-ref' "/compare/0.28.0...${head_short})" "$out"
assert_not_contains 'compare link does not invent a tag' '9.9.9)' "$out"

# Two tags on one commit: the heading still appears, the body says so.
git tag 0.29.0 0.28.0
out=$("$GEN" 0.29.0 "" 0.29.0)
assert_contains 'empty range keeps its heading' \
  "## [0.29.0](${REMOTE}/compare/0.28.0...0.29.0) - 2026-08-10" "$out"
assert_contains 'empty range says so' '_No changes._' "$out"
assert_eq 'empty range has no bullets' 0 "$(grep -c '^- ' <<<"$out")"

# A backport tagged while a higher tag exists must compare against the tag below
# it, not against the newest tag in the repository.
git checkout -q -b backport 0.27.0
commit '2026-08-12T09:00:00+00:00' 'Stop the exporter thread from retrying forever'
git tag 0.27.1
git checkout -q main
out=$("$GEN" 0.27.1 "" 0.27.1)
assert_contains 'backport compares against the tag below it' \
  "(${REMOTE}/compare/0.27.0...0.27.1)" "$out"
assert_contains 'backport commit listed' '- Stop the exporter thread from retrying forever' "$out"
assert_not_contains 'backport excludes earlier history' '- Record tool executions' "$out"
assert_not_contains 'backport excludes the higher tag' '- Install the MeterProvider' "$out"

# The changelog PR's own commit lands in the next release's range and is not a
# change anyone reads about. "Bump" in prose is not a dependency bump.
commit '2026-08-13T09:00:00+00:00' 'Release 0.29.0: changelog'
commit '2026-08-13T11:00:00+00:00' 'Bump the default sample rate to 0.5'
commit '2026-08-13T12:00:00+00:00' 'Bump agento11y from 0.14.0 to 0.14.1 (#60)'
commit '2026-08-13T13:00:00+00:00' 'Bump the actions group with 3 updates (#61)'
git tag 0.30.0
out=$("$GEN" 0.30.0 0.29.0 0.30.0)
assert_not_contains 'the changelog PR commit is dropped' 'Release 0.29.0' "$out"
deps=$(awk '/^### Dependencies/{seen=1} seen' <<<"$out")
assert_contains 'prose bump stays a change' '- Bump the default sample rate to 0.5' "$out"
assert_not_contains 'prose bump is not a dependency' 'default sample rate' "$deps"
assert_contains 'dependabot bump is a dependency' '- Bump agento11y from 0.14.0 to 0.14.1 (#60)' "$deps"
assert_contains 'a grouped bump is a dependency too' '- Bump the actions group with 3 updates (#61)' "$deps"

# A range holding nothing but bot commits reads as no changes at all.
git checkout -q -b bots-only 0.29.0
commit '2026-08-14T09:00:00+00:00' 'Release 0.29.0: changelog'
git tag 0.29.1
git checkout -q main
out=$("$GEN" 0.29.1 0.29.0 0.29.1)
assert_contains 'a bot-only range has no changes' '_No changes._' "$out"

# A pre-release tag is a tag like any other. It normalizes to 0.31.0rc1, which
# sorts below 0.31.0 but above 0.30.0.
commit '2026-08-15T09:00:00+00:00' 'Publish releases to PyPI'
git tag 0.31.0rc1
out=$("$GEN" 0.31.0rc1 "" 0.31.0rc1)
assert_contains 'a pre-release compares against the release below it' \
  "(${REMOTE}/compare/0.30.0...0.31.0rc1)" "$out"
assert_contains 'pre-release commit listed' '- Publish releases to PyPI' "$out"

# The compare host follows the remote, so a fork or an org move does not leave
# every link pointing at the old repository.
out=$(CHANGELOG_REPO_URL=https://example.com/acme/fork "$GEN" 0.28.0 0.27.0 0.28.0)
assert_contains 'CHANGELOG_REPO_URL wins' 'https://example.com/acme/fork/compare/0.27.0...0.28.0' "$out"
out=$(GITHUB_SERVER_URL=https://github.example.com GITHUB_REPOSITORY=acme/plugin "$GEN" 0.28.0 0.27.0 0.28.0)
assert_contains 'CI environment names the repository' \
  'https://github.example.com/acme/plugin/compare/' "$out"
git remote set-url origin git@github.com:acme/plugin.git
out=$("$GEN" 0.28.0 0.27.0 0.28.0)
assert_contains 'an ssh remote becomes an https link' 'https://github.com/acme/plugin/compare/' "$out"
git remote remove origin
out=$("$GEN" 0.28.0 0.27.0 0.28.0)
assert_contains 'no remote means no compare link' '## [0.28.0] - 2026-08-10' "$out"
assert_not_contains 'no remote emits no broken link' '/compare/' "$out"
git remote add origin "${REMOTE}.git"

# insert-changelog-section.sh: creates the file, keeps one H1, stays readable.
FILE="${TMP}/CHANGELOG.md"
printf '## [0.5.0] - 2026-03-11\n\n- Add the hook handlers\n' | "$INSERT" "$FILE"
assert_eq 'creates the file with one header' \
  "# Changelog

## [0.5.0] - 2026-03-11

- Add the hook handlers" "$(cat "$FILE")"
assert_eq 'created file is readable by everyone' 644 "$(file_mode "$FILE")"

printf '## [0.6.0] - 2026-04-01\n\n- Record tool executions\n' | "$INSERT" "$FILE"
assert_eq 'newer version goes on top and the header stays single' \
  "# Changelog

## [0.6.0] - 2026-04-01

- Record tool executions

## [0.5.0] - 2026-03-11

- Add the hook handlers" "$(cat "$FILE")"
assert_count 'exactly one top-level header' '# Changelog' 1 "$(cat "$FILE")"
assert_eq 'mode survives repeated inserts' 644 "$(file_mode "$FILE")"

# Re-running a release must not stack a second copy of its section.
before=$(cat "$FILE")
printf '## [0.6.0] - 2026-04-01\n\n- Record tool executions\n' | "$INSERT" "$FILE" 2>/dev/null
assert_eq 'inserting a version already in the file is a no-op' 0 "$?"
assert_eq 'the file is untouched' "$before" "$(cat "$FILE")"

# A version cut while an older release's PR is still open goes in by version,
# not at the head of the file.
printf '## [0.5.5] - 2026-03-20\n\n- Stop the exporter thread from retrying forever\n' | "$INSERT" "$FILE"
assert_eq 'sections stay ordered by version' \
  "## [0.6.0] - 2026-04-01
## [0.5.5] - 2026-03-20
## [0.5.0] - 2026-03-11" "$(grep '^## ' "$FILE")"
printf '## [0.10.0] - 2026-05-01\n\n- Add the redaction bounds\n' | "$INSERT" "$FILE"
assert_eq 'version ordering is not lexical' '## [0.10.0] - 2026-05-01' "$(grep -m1 '^## ' "$FILE")"

# Anything written between the H1 and the first section stays above the
# sections instead of being pushed under the newest one.
PREAMBLE_FILE="${TMP}/preamble.md"
printf '# Changelog\n\nAll notable changes to this plugin.\n\n## [0.5.0] - 2026-03-11\n\n- Add the hook handlers\n' \
  >"$PREAMBLE_FILE"
printf '## [0.6.0] - 2026-04-01\n\n- Record tool executions\n' | "$INSERT" "$PREAMBLE_FILE"
assert_eq 'the preamble stays at the top' \
  "# Changelog

All notable changes to this plugin.

## [0.6.0] - 2026-04-01

- Record tool executions

## [0.5.0] - 2026-03-11

- Add the hook handlers" "$(cat "$PREAMBLE_FILE")"

# A generator that failed upstream sends nothing down the pipe. Rewriting the
# file from that is worse than stopping.
before=$(cat "$FILE")
printf '' | "$INSERT" "$FILE" 2>/dev/null
assert_eq 'empty stdin fails' 65 "$?"
printf 'just some text\n' | "$INSERT" "$FILE" 2>/dev/null
assert_eq 'stdin without a heading fails' 65 "$?"
assert_eq 'a failed insert leaves the file alone' "$before" "$(cat "$FILE")"

# backfill-changelog.sh: rebuilds the whole file from the tags of whatever
# repository it is run in.
new_repo "${TMP}/backfill"
git remote add origin "${REMOTE}.git"
commit '2026-03-11T10:00:00+00:00' 'Add the hook handlers'
git tag 0.5.0
commit '2026-04-01T10:00:00+00:00' 'Record tool executions'
git tag 0.6.0
commit '2026-05-01T10:00:00+00:00' 'Add the redaction bounds'
git tag 0.28.0

"$BACKFILL" >/dev/null
assert_eq 'backfill writes newest version first' \
  "## [0.28.0](${REMOTE}/compare/0.6.0...0.28.0) - 2026-05-01
## [0.6.0](${REMOTE}/compare/0.5.0...0.6.0) - 2026-04-01
## [0.5.0] - 2026-03-11" "$(grep '^## ' CHANGELOG.md)"
assert_count 'backfill writes one top-level header' '# Changelog' 1 "$(cat CHANGELOG.md)"
assert_eq 'backfilled file is readable by everyone' 644 "$(file_mode CHANGELOG.md)"

backfilled=$(cat CHANGELOG.md)
"$BACKFILL" --force >/dev/null
assert_eq 'a re-run reproduces the same file' "$backfilled" "$(cat CHANGELOG.md)"

# The file is a full rewrite, so hand edits must not disappear silently.
git add CHANGELOG.md
git commit -q -m 'Add CHANGELOG.md'
printf '\nHand-written note.\n' >>CHANGELOG.md
edited=$(cat CHANGELOG.md)
"$BACKFILL" >/dev/null 2>&1
assert_eq 'backfill refuses to clobber uncommitted edits' 1 "$?"
assert_eq 'the edited file survives' "$edited" "$(cat CHANGELOG.md)"

out=$("$BACKFILL" --dry-run)
assert_contains 'a dry run prints the file' '## [0.28.0]' "$out"
assert_eq 'a dry run writes nothing' "$edited" "$(cat CHANGELOG.md)"

"$BACKFILL" --force >/dev/null
assert_eq '--force overwrites the edits' "$backfilled" "$(cat CHANGELOG.md)"

cd "$TMP" || exit 1
echo "passed: ${pass}, failed: ${fail}"
[[ $fail -eq 0 ]]
