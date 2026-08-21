#!/usr/bin/env bash
# test_pinned_images.sh — Unit tests for scripts/check-pinned-images.sh
#
# Requires: bash
# Run standalone:  bash scripts/tests/test_pinned_images.sh
# Run via runner:  scripts/tests/run_tests.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/check-pinned-images.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STUB_DIR="$SCRIPT_DIR"
readonly VANILLA_HAPI_IMAGE="hapiproject/hapi:v8.8.0-1"

PASS=0
FAIL=0

pass() { printf '[PASS] %s\n' "$1"; PASS=$(( PASS + 1 )); }
fail() { printf '[FAIL] %s\n' "$1"; FAIL=$(( FAIL + 1 )); }

# A minimal LEONARD_DIR is enough — check-pinned-images.sh only needs the two
# compose file paths to exist for `docker compose -f ... -f ...` to accept
# them; the stub never reads their content.
make_leonard_dir() {
    local d
    d=$(mktemp -d)
    : > "$d/docker-compose.yml"
    : > "$d/docker-compose.prod.yml"
    printf '%s' "$d"
}

# ---------------------------------------------------------------------------
# Test 1 — Neither service pinned: exit 0, no docker pull recorded
# ---------------------------------------------------------------------------
t1() {
    local tmpdir pull_calls
    tmpdir=$(make_leonard_dir)
    pull_calls=$(mktemp)
    trap 'rm -rf "$tmpdir"; rm -f "$pull_calls"' RETURN

    local rc=0
    LEONARD_DIR="$tmpdir" \
    STUB_PULL_CALLS="$pull_calls" \
    PATH="$STUB_DIR:$PATH" \
    bash "$CHECK_SCRIPT" >/dev/null 2>&1 || rc=$?

    if [[ "$rc" -ne 0 ]]; then
        fail "unpinned: expected exit 0, got $rc"
        return
    fi

    if [[ -s "$pull_calls" ]]; then
        fail "unpinned: expected no docker pull, but got: $(cat "$pull_calls")"
    else
        pass "unpinned: exits 0 and never calls docker pull"
    fi
}

# ---------------------------------------------------------------------------
# Test 2 — CDR pinned to a ref that pulls successfully: exit 0, pull recorded
# ---------------------------------------------------------------------------
t2() {
    local tmpdir pull_calls
    tmpdir=$(make_leonard_dir)
    pull_calls=$(mktemp)
    trap 'rm -rf "$tmpdir"; rm -f "$pull_calls"' RETURN

    local pinned_ref="ghcr.io/bellese/lenny-hapi-cdr:abc123"
    local rc=0
    LEONARD_DIR="$tmpdir" \
    STUB_RESOLVE_CDR="$pinned_ref" \
    STUB_PULL_SCENARIO=ok \
    STUB_PULL_CALLS="$pull_calls" \
    PATH="$STUB_DIR:$PATH" \
    bash "$CHECK_SCRIPT" >/dev/null 2>&1 || rc=$?

    if [[ "$rc" -ne 0 ]]; then
        fail "pinned+pullable: expected exit 0, got $rc"
        return
    fi

    if grep -qF "$pinned_ref" "$pull_calls"; then
        pass "pinned+pullable: exits 0 and pulls the pinned ref"
    else
        fail "pinned+pullable: exited 0 but pinned ref not pulled — calls: $(cat "$pull_calls")"
    fi
}

# ---------------------------------------------------------------------------
# Test 3 — Measure pinned to a ref that fails to pull: exit 1, message names
#           the service and the ref
# ---------------------------------------------------------------------------
t3() {
    local tmpdir
    tmpdir=$(make_leonard_dir)
    trap 'rm -rf "$tmpdir"' RETURN

    local dead_ref="ghcr.io/bellese/mct2-hapi-measure:latest"
    local rc=0 stderr_out
    stderr_out=$(LEONARD_DIR="$tmpdir" \
                 STUB_RESOLVE_MEASURE="$dead_ref" \
                 STUB_PULL_SCENARIO=fail \
                 PATH="$STUB_DIR:$PATH" \
                 bash "$CHECK_SCRIPT" 2>&1 >/dev/null) || rc=$?

    if [[ "$rc" -ne 1 ]]; then
        fail "pinned+unpullable: expected exit 1, got $rc"
        return
    fi

    if printf '%s' "$stderr_out" | grep -qF "hapi-fhir-measure" \
        && printf '%s' "$stderr_out" | grep -qF "$dead_ref"; then
        pass "pinned+unpullable: exits 1 with a message naming the service and ref"
    else
        fail "pinned+unpullable: exited 1 but message didn't name service+ref — stderr: $stderr_out"
    fi
}

# ---------------------------------------------------------------------------
# Test 4 — compose cannot resolve an image for a service: exit 2
# ---------------------------------------------------------------------------
t4() {
    local tmpdir
    tmpdir=$(make_leonard_dir)
    trap 'rm -rf "$tmpdir"' RETURN

    local rc=0 stderr_out
    stderr_out=$(LEONARD_DIR="$tmpdir" \
                 STUB_RESOLVE_CDR="" \
                 PATH="$STUB_DIR:$PATH" \
                 bash "$CHECK_SCRIPT" 2>&1 >/dev/null) || rc=$?

    if [[ "$rc" -ne 2 ]]; then
        fail "unresolved: expected exit 2, got $rc"
        return
    fi

    if printf '%s' "$stderr_out" | grep -qF "hapi-fhir-cdr"; then
        pass "unresolved: exits 2 and names the unresolved service"
    else
        fail "unresolved: exited 2 but didn't name the service — stderr: $stderr_out"
    fi
}

# ---------------------------------------------------------------------------
# Test 5 — drift canary: the guard's hardcoded default must still match
#           both HAPI_*_IMAGE defaults in docker-compose.yml
# ---------------------------------------------------------------------------
t5() {
    local compose_file="$REPO_ROOT/docker-compose.yml"
    local matches
    matches=$(grep -cF "image: \${HAPI_CDR_IMAGE:-${VANILLA_HAPI_IMAGE}}" "$compose_file" || true)
    matches=$(( matches + $(grep -cF "image: \${HAPI_MEASURE_IMAGE:-${VANILLA_HAPI_IMAGE}}" "$compose_file" || true) ))

    if [[ "$matches" -eq 2 ]]; then
        pass "drift canary: docker-compose.yml defaults still match ${VANILLA_HAPI_IMAGE}"
    else
        fail "drift canary: docker-compose.yml default drifted from ${VANILLA_HAPI_IMAGE} — update both check-pinned-images.sh and this test"
    fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
t1
t2
t3
t4
t5

echo ""
echo "pinned-images tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
