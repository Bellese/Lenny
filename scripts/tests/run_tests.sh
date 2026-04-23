#!/usr/bin/env bash
# run_tests.sh — Run all unit tests for MCT2 bash scripts.
#
# Usage:
#   bash scripts/tests/run_tests.sh          # native bash (reconcile tests only on macOS)
#   bash scripts/tests/run_tests.sh --docker # all tests via bash:5 Docker image
#
# Exits 0 if all tests pass, 1 if any fail.
# Each test file runs in its own subshell so failures are isolated.
#
# macOS note: fetch-prod-secrets.sh requires bash 4+ (declare -A).
# macOS ships bash 3.2. Use --docker to run fetch tests on macOS.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

USE_DOCKER=0
[[ "${1:-}" == "--docker" ]] && USE_DOCKER=1

PASS_FILES=0
FAIL_FILES=0

run_file() {
    local file="$1"
    local name
    name="$(basename "$file")"
    # Compute path relative to repo root for Docker runs.
    local rel_file="${file#"$REPO_ROOT/"}"
    printf '=== %s ===\n' "$name"
    local rc=0
    if [[ "$USE_DOCKER" -eq 1 ]]; then
        docker run --rm \
            -v "$REPO_ROOT:/repo" \
            -w /repo \
            bash:5 \
            bash -c "apk add --quiet --no-progress jq >/dev/null 2>&1 && bash '$rel_file'" || rc=$?
    else
        bash "$file" || rc=$?
    fi
    if [[ "$rc" -eq 0 ]]; then
        PASS_FILES=$(( PASS_FILES + 1 ))
    else
        FAIL_FILES=$(( FAIL_FILES + 1 ))
    fi
    printf '\n'
}

run_file "$SCRIPT_DIR/test_fetch.sh"
run_file "$SCRIPT_DIR/test_reconcile.sh"

printf '=== Summary: %d file(s) passed, %d file(s) failed ===\n' "$PASS_FILES" "$FAIL_FILES"
[[ "$FAIL_FILES" -eq 0 ]]
