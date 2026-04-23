#!/usr/bin/env bash
# test_reconcile.sh — Unit tests for scripts/reconcile-db-password.sh
#
# Requires: bash
# Run standalone:  bash scripts/tests/test_reconcile.sh
# Run via runner:  scripts/tests/run_tests.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECONCILE_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/reconcile-db-password.sh"
STUB_DIR="$SCRIPT_DIR"
# The docker stub is named "docker" and lives in $STUB_DIR.
# Prepend $STUB_DIR to PATH so it shadows the real docker.

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; PASS=$(( PASS + 1 )); }
fail() { printf '[FAIL] %s\n' "$1"; FAIL=$(( FAIL + 1 )); }

# ---------------------------------------------------------------------------
# Test 1 — Happy path: valid env file, docker stub exits 0, psql invoked
# ---------------------------------------------------------------------------
t1() {
    local tmpdir calls_file
    tmpdir=$(mktemp -d)
    calls_file=$(mktemp)
    trap 'rm -rf "$tmpdir"; rm -f "$calls_file"' RETURN

    printf 'POSTGRES_PASSWORD=testpassword12345678901234567\n' > "$tmpdir/env"

    local rc=0
    LEONARD_ENV_FILE="$tmpdir/env" \
    LEONARD_DIR="$tmpdir" \
    STUB_DOCKER_SCENARIO=ok \
    STUB_DOCKER_CALLS="$calls_file" \
    PATH="$STUB_DIR:$PATH" \
    bash "$RECONCILE_SCRIPT" >/dev/null 2>&1 || rc=$?

    if [[ "$rc" -ne 0 ]]; then
        fail "happy path: expected exit 0, got $rc"
        return
    fi

    if grep -qF 'psql' "$calls_file" && grep -qF 'ALTER ROLE mct2 PASSWORD' "$calls_file"; then
        pass "happy path: exits 0 and psql ALTER ROLE invoked correctly"
    else
        fail "happy path: exited 0 but expected psql ALTER ROLE call — calls: $(cat "$calls_file")"
    fi
}

# ---------------------------------------------------------------------------
# Test 2 — Missing password: empty env file, exit 1 with stderr message
# ---------------------------------------------------------------------------
t2() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' RETURN

    # Write a file with no POSTGRES_PASSWORD line
    printf '# empty\n' > "$tmpdir/env"

    local rc=0 stderr_out
    stderr_out=$(LEONARD_ENV_FILE="$tmpdir/env" \
                 LEONARD_DIR="$tmpdir" \
                 STUB_DOCKER_SCENARIO=ok \
                 PATH="$STUB_DIR:$PATH" \
                 bash "$RECONCILE_SCRIPT" 2>&1 >/dev/null) || rc=$?

    if [[ "$rc" -eq 1 ]] && [[ -n "$stderr_out" ]]; then
        pass "missing password: exits 1 with non-empty stderr"
    elif [[ "$rc" -ne 1 ]]; then
        fail "missing password: expected exit 1, got $rc"
    else
        fail "missing password: exited 1 but stderr was empty"
    fi
}

# ---------------------------------------------------------------------------
# Test 3 — psql failure: docker stub exits 1, script exits 2, password
#           not printed in plaintext on stderr
# ---------------------------------------------------------------------------
t3() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' RETURN

    local pw="testpassword12345678901234567"
    printf 'POSTGRES_PASSWORD=%s\n' "$pw" > "$tmpdir/env"

    local rc=0 stderr_out
    stderr_out=$(LEONARD_ENV_FILE="$tmpdir/env" \
                 LEONARD_DIR="$tmpdir" \
                 STUB_DOCKER_SCENARIO=fail \
                 PATH="$STUB_DIR:$PATH" \
                 bash "$RECONCILE_SCRIPT" 2>&1 >/dev/null) || rc=$?

    if [[ "$rc" -ne 2 ]]; then
        fail "psql failure: expected exit 2, got $rc"
        return
    fi

    if printf '%s' "$stderr_out" | grep -qF "$pw"; then
        fail "psql failure: plaintext password leaked in stderr output"
    else
        pass "psql failure: exits 2 and password not in plaintext on stderr"
    fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
t1
t2
t3

echo ""
echo "reconcile tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
