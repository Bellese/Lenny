#!/usr/bin/env bash
# test_fetch.sh — Unit tests for scripts/fetch-prod-secrets.sh
#
# Requires: bash 4+, jq
# Run standalone:  bash scripts/tests/test_fetch.sh
# Run via runner:  scripts/tests/run_tests.sh
#
# Root guard: tests use SKIP_ROOT_CHECK=1 (already supported by the script).
# Never set SKIP_ROOT_CHECK=1 outside of test environments.
#
# macOS note: fetch-prod-secrets.sh uses `declare -A` (bash 4+ only).
# macOS ships bash 3.2. These tests require bash 4+.
# On macOS, run via Docker: scripts/tests/run_tests.sh --docker

set -euo pipefail

# Require bash 4+ (fetch-prod-secrets.sh uses declare -A).
if (( BASH_VERSINFO[0] < 4 )); then
    printf '[SKIP] test_fetch.sh requires bash 4+ (found %s). On macOS, run via:\n' "$BASH_VERSION"
    printf '         scripts/tests/run_tests.sh --docker\n'
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FETCH_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/fetch-prod-secrets.sh"
STUB_DIR="$SCRIPT_DIR"

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; PASS=$(( PASS + 1 )); }
fail() { printf '[FAIL] %s\n' "$1"; FAIL=$(( FAIL + 1 )); }

# ---------------------------------------------------------------------------
# Test 1 — Happy path: valid POSTGRES_PASSWORD written to env file
# ---------------------------------------------------------------------------
t1() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' RETURN

    local result
    if result=$(SKIP_ROOT_CHECK=1 \
                LEONARD_ENV_DIR="$tmpdir" \
                STUB_AWS_SCENARIO=happy \
                PATH="$STUB_DIR:$PATH" \
                bash "$FETCH_SCRIPT" 2>&1); then
        if grep -qF 'POSTGRES_PASSWORD=testpassword12345678901234567' "$tmpdir/env"; then
            pass "happy path: env file contains correct POSTGRES_PASSWORD"
        else
            fail "happy path: env file missing POSTGRES_PASSWORD"
        fi
    else
        fail "happy path: script exited non-zero — $result"
    fi
}

# ---------------------------------------------------------------------------
# Test 2 — Validation failure: value contains '/' (forbidden char), exit 2
# ---------------------------------------------------------------------------
t2() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' RETURN

    local rc=0
    SKIP_ROOT_CHECK=1 \
    LEONARD_ENV_DIR="$tmpdir" \
    STUB_AWS_SCENARIO=invalid \
    PATH="$STUB_DIR:$PATH" \
    bash "$FETCH_SCRIPT" >/dev/null 2>&1 || rc=$?

    if [[ "$rc" -eq 2 ]]; then
        pass "validation failure: exits 2 for forbidden character in value"
    else
        fail "validation failure: expected exit 2, got $rc"
    fi
}

# ---------------------------------------------------------------------------
# Test 3 — Missing required param: empty Parameters array, exit 1
# ---------------------------------------------------------------------------
t3() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' RETURN

    local rc=0
    SKIP_ROOT_CHECK=1 \
    LEONARD_ENV_DIR="$tmpdir" \
    STUB_AWS_SCENARIO=empty \
    PATH="$STUB_DIR:$PATH" \
    bash "$FETCH_SCRIPT" >/dev/null 2>&1 || rc=$?

    if [[ "$rc" -eq 1 ]]; then
        pass "missing required param: exits 1 when Parameters array is empty"
    else
        fail "missing required param: expected exit 1, got $rc"
    fi
}

# ---------------------------------------------------------------------------
# Test 4 — LEONARD_SSM_VERSION: get-parameter called with version-pinned name
# ---------------------------------------------------------------------------
t4() {
    local tmpdir calls_file
    tmpdir=$(mktemp -d)
    calls_file=$(mktemp)
    trap 'rm -rf "$tmpdir"; rm -f "$calls_file"' RETURN

    SKIP_ROOT_CHECK=1 \
    LEONARD_ENV_DIR="$tmpdir" \
    LEONARD_SSM_VERSION=1 \
    STUB_AWS_CALLS="$calls_file" \
    PATH="$STUB_DIR:$PATH" \
    bash "$FETCH_SCRIPT" >/dev/null 2>&1

    if grep -qF 'get-parameter' "$calls_file" && \
       grep -qF 'POSTGRES_PASSWORD:1' "$calls_file"; then
        pass "SSM version path: get-parameter called with version-pinned name"
    else
        fail "SSM version path: expected get-parameter call with POSTGRES_PASSWORD:1 — calls: $(cat "$calls_file")"
    fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
t1
t2
t3
t4

echo ""
echo "fetch tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
