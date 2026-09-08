#!/usr/bin/env bash
# test_version_consistency.sh — Unit tests for scripts/check-version-consistency.sh
#
# Requires: bash, jq
# Run standalone:  bash scripts/tests/test_version_consistency.sh
# Run via runner:  scripts/tests/run_tests.sh
#
# Every test builds a throwaway REPO_DIR fixture, so nothing here reads or
# writes the real repository's version files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/check-version-consistency.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; PASS=$(( PASS + 1 )); }
fail() { printf '[FAIL] %s\n' "$1"; FAIL=$(( FAIL + 1 )); }

# Build a fixture repo: $1 VERSION, $2 package.json version, $3 lock .version,
# $4 lock .packages[""].version, $5 pin contents ("-" writes no pin file).
make_repo() {
    local version="$1" pkg="$2" lock_top="$3" lock_pkgs="$4" pin="$5"
    local d
    d=$(mktemp -d)
    mkdir -p "$d/frontend" "$d/.gstack"
    [[ "$version" == "-" ]] || printf '%s\n' "$version" > "$d/VERSION"
    [[ "$pin" == "-" ]] || printf '%s\n' "$pin" > "$d/.gstack/package-json-path"
    printf '{\n  "name": "lenny-frontend",\n  "version": "%s"\n}\n' "$pkg" \
        > "$d/frontend/package.json"
    printf '{\n  "name": "lenny-frontend",\n  "version": "%s",\n  "packages": {\n    "": {\n      "version": "%s"\n    }\n  }\n}\n' \
        "$lock_top" "$lock_pkgs" > "$d/frontend/package-lock.json"
    printf '%s' "$d"
}

# Run the guard against a fixture and assert its exit code.
expect_rc() {
    local want="$1" name="$2" repo="$3"
    local rc=0
    REPO_DIR="$repo" bash "$CHECK_SCRIPT" >/dev/null 2>&1 || rc=$?
    rm -rf "$repo"
    if [[ "$rc" -eq "$want" ]]; then
        pass "$name"
    else
        fail "$name: expected exit $want, got $rc"
    fi
}

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
expect_rc 0 "all three agree -> exit 0" \
    "$(make_repo 0.2.0.0 0.2.0 0.2.0 0.2.0 frontend/package.json)"

# A MICRO-only bump leaves the manifest at the same 3-digit translation. This
# is intended (see the script's header), so it must pass rather than drift.
expect_rc 0 "MICRO bump keeps the same manifest version -> exit 0" \
    "$(make_repo 0.2.0.7 0.2.0 0.2.0 0.2.0 frontend/package.json)"

# ---------------------------------------------------------------------------
# Drift -> exit 1. These are the states issue #420 actually observed.
# ---------------------------------------------------------------------------
# The real drift on main: VERSION moved twice, package.json never followed.
expect_rc 1 "package.json behind VERSION -> exit 1" \
    "$(make_repo 0.2.0.0 0.0.22.0 0.0.22.0 0.0.22.0 frontend/package.json)"

expect_rc 1 "package.json alone drifted -> exit 1" \
    "$(make_repo 0.2.0.0 0.1.0 0.2.0 0.2.0 frontend/package.json)"

# The lockfile carries the version twice; a bump that updates one and not the
# other is exactly what #401-#403 did, so both fields are checked separately.
expect_rc 1 "lockfile .version drifted -> exit 1" \
    "$(make_repo 0.2.0.0 0.2.0 0.0.19.3 0.2.0 frontend/package.json)"

expect_rc 1 "lockfile .packages[\"\"].version drifted -> exit 1" \
    "$(make_repo 0.2.0.0 0.2.0 0.2.0 0.0.19.3 frontend/package.json)"

# Mirroring VERSION verbatim is invalid semver and not what /ship writes.
expect_rc 1 "manifest mirroring the 4-digit VERSION -> exit 1" \
    "$(make_repo 0.2.0.0 0.2.0.0 0.2.0.0 0.2.0.0 frontend/package.json)"

# ---------------------------------------------------------------------------
# The pin — the part that stops drift recurring rather than just detecting it
# ---------------------------------------------------------------------------
expect_rc 1 "missing .gstack/package-json-path -> exit 1" \
    "$(make_repo 0.2.0.0 0.2.0 0.2.0 0.2.0 -)"

expect_rc 1 "pin pointing at the wrong manifest -> exit 1" \
    "$(make_repo 0.2.0.0 0.2.0 0.2.0 0.2.0 package.json)"

# ---------------------------------------------------------------------------
# Malformed input -> exit 2, never a silent skip
# ---------------------------------------------------------------------------
expect_rc 2 "missing VERSION -> exit 2" \
    "$(make_repo - 0.2.0 0.2.0 0.2.0 frontend/package.json)"

expect_rc 2 "3-component VERSION -> exit 2" \
    "$(make_repo 0.2.0 0.2.0 0.2.0 0.2.0 frontend/package.json)"

expect_rc 2 "non-numeric VERSION -> exit 2" \
    "$(make_repo v0.2.0.0 0.2.0 0.2.0 0.2.0 frontend/package.json)"

# A manifest that is not valid JSON must be a hard error, not a passed check.
malformed=$(make_repo 0.2.0.0 0.2.0 0.2.0 0.2.0 frontend/package.json)
printf 'not json at all' > "$malformed/frontend/package.json"
expect_rc 2 "unparseable package.json -> exit 2" "$malformed"

# A manifest with no version field at all.
noversion=$(make_repo 0.2.0.0 0.2.0 0.2.0 0.2.0 frontend/package.json)
printf '{\n  "name": "lenny-frontend"\n}\n' > "$noversion/frontend/package.json"
expect_rc 2 "package.json without a version field -> exit 2" "$noversion"

# ---------------------------------------------------------------------------
# Drift canary: the real repository must satisfy its own guard.
# ---------------------------------------------------------------------------
rc=0
REPO_DIR="$REPO_ROOT" bash "$CHECK_SCRIPT" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "the repository itself passes the guard"
else
    fail "the repository itself fails the guard (exit $rc) — run the script for detail"
fi

printf '\n=== test_version_consistency.sh: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
