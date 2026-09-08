#!/usr/bin/env bash
# check-version-consistency.sh — Fail when the repo's version files drift apart.
#
# Background (issue #420): three files carry a version and they diverged across
# two consecutive releases. VERSION reached 0.2.0.0 while frontend/package.json
# sat at 0.0.22.0 and the lockfile trailed both. That is not cosmetic —
# frontend/src/App.js renders `Lenny · v{pkg.version}` in the status bar, so the
# running app told users it was v0.0.22.0 while the shipped release was 0.2.0.0.
#
# Root cause was mechanical, not a missing policy. /ship's version-bump step
# already writes VERSION, the manifest and the manifest's lockfiles together,
# resolving the manifest as `--package-json-path` -> `.gstack/package-json-path`
# -> `./package.json`. This repo has no root package.json (the only one lives in
# frontend/) and had no pin, so every /ship run silently did a VERSION-only
# bump. The hand-bumped releases before it moved both files; the shipped ones
# did not. `.gstack/package-json-path` is now committed for that reason — it was
# gitignored, so it could not survive the `git worktree add` that CLAUDE.md's
# workflow mandates, and each new worktree would have started unpinned.
#
# The version relationship this enforces:
#
#   VERSION                     4-component MAJOR.MINOR.PATCH.MICRO. The source
#                               of truth. Nothing machine-reads it; it drives
#                               the CHANGELOG heading and the PR title.
#   frontend/package.json       The first 3 components of VERSION. npm rejects a
#                               fourth component (`0.0.22.0` is not valid semver
#                               -- `semver.valid()` returns null), so the
#                               manifest carries the npm-valid translation. This
#                               matches what /ship's writer does, rather than
#                               fighting it.
#   frontend/package-lock.json  Both of its version fields track package.json.
#
# Consequence, stated rather than discovered later: a MICRO-only bump
# (0.2.0.0 -> 0.2.0.1) leaves the manifest unchanged at 0.2.0, so the on-screen
# version does not move for MICRO releases. The CHANGELOG still records them.
#
# Exit codes:
#   0  all version files agree
#   1  a version file has drifted
#   2  a version file is missing or malformed, or a required tool is absent --
#      a hard failure rather than a silent skip, since skipping would
#      reintroduce the exact drift this exists to catch
#
# Usage:
#   ./scripts/check-version-consistency.sh
#
# Optional env vars:
#   REPO_DIR   Repository root (default: parent of this script's directory).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

VERSION_FILE="${REPO_DIR}/VERSION"
PIN_FILE="${REPO_DIR}/.gstack/package-json-path"
readonly EXPECTED_PIN="frontend/package.json"

die() { printf '[!] check-version-consistency: %s\n' "$1" >&2; exit "$2"; }

command -v jq >/dev/null 2>&1 || die "jq is required but not installed" 2

# ── VERSION ───────────────────────────────────────────────────────────────────
[[ -f "$VERSION_FILE" ]] || die "VERSION not found at $VERSION_FILE" 2
version="$(tr -d '[:space:]' < "$VERSION_FILE")"

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "VERSION is '$version', expected 4-component MAJOR.MINOR.PATCH.MICRO" 2
fi

# The npm-valid translation: first three components. Mirrors gstack's
# npmVersion() (~/.claude/skills/gstack/lib/version-source.ts), which is what
# /ship writes into the manifest.
expected_npm="${version%.*}"

printf '[+] check-version-consistency: VERSION=%s -> manifest expects %s\n' \
    "$version" "$expected_npm"

# ── the pin that keeps /ship writing the manifest at all ──────────────────────
# Without this, /ship resolves the manifest to a root package.json that does not
# exist here and silently bumps VERSION alone. Verifying the pin is what stops
# the drift recurring; the checks below only detect it after the fact.
[[ -f "$PIN_FILE" ]] || die \
    "missing $PIN_FILE — /ship would bump VERSION without frontend/package.json (#420)" 1
pin="$(head -n1 "$PIN_FILE" | tr -d '[:space:]')"
if [[ "$pin" != "$EXPECTED_PIN" ]]; then
    die "$PIN_FILE points at '$pin', expected '$EXPECTED_PIN'" 1
fi
printf '[+] check-version-consistency: .gstack/package-json-path -> %s\n' "$pin"

# ── the manifest and its lockfile ─────────────────────────────────────────────
# Each entry is "<path-relative-to-repo>|<jq filter>|<label>".
CHECKS=(
    "frontend/package.json|.version|frontend/package.json"
    "frontend/package-lock.json|.version|frontend/package-lock.json (.version)"
    "frontend/package-lock.json|.packages[\"\"].version|frontend/package-lock.json (.packages[\"\"].version)"
)

drift=0
for entry in "${CHECKS[@]}"; do
    rel="${entry%%|*}"
    rest="${entry#*|}"
    filter="${rest%%|*}"
    label="${rest#*|}"
    path="${REPO_DIR}/${rel}"

    [[ -f "$path" ]] || die "$rel not found at $path" 2

    # `// empty` turns a JSON null into an empty string so a missing key is
    # caught below rather than compared as the literal "null".
    actual="$(jq -r "${filter} // empty" "$path")" \
        || die "$rel is not valid JSON" 2
    [[ -n "$actual" ]] || die "$label has no version field" 2

    if [[ "$actual" != "$expected_npm" ]]; then
        printf '[!] check-version-consistency: %s is %s, expected %s\n' \
            "$label" "$actual" "$expected_npm" >&2
        drift=1
    else
        printf '[+] check-version-consistency: %s = %s\n' "$label" "$actual"
    fi
done

if [[ "$drift" -ne 0 ]]; then
    cat >&2 <<MSG
[!] FATAL: version files have drifted (#420).
    VERSION is $version, so frontend/package.json and frontend/package-lock.json
    must both read $expected_npm.

    Fix with /ship's own writer, which updates all three together:
      bun run ~/.claude/skills/gstack/bin/gstack-version-bump repair

    See docs/workflow.md, section "Versioning".
MSG
    exit 1
fi

printf '[+] check-version-consistency: all version files agree\n'
