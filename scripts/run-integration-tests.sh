#!/usr/bin/env bash
#
# Run MCT2 integration tests against real HAPI FHIR instances.
#
# Usage:
#   ./scripts/run-integration-tests.sh
#   ./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py
#
# This script:
#   1. Starts docker-compose.test.yml in background
#   2. Waits for health checks to pass
#   3. Runs pytest -m integration
#   4. Stops and removes test containers
#   5. Exits with pytest's exit code
#
# Invocation modes:
#   - No explicit test targets: run the default integration suite.
#   - Explicit file/dir/nodeid targets: run only those targets.
# This keeps option-only CI invocations (for example --ignore=...) on the
# default suite while allowing nightly jobs to target isolated test files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.test.yml"

CDR_METADATA="http://localhost:8180/fhir/metadata"
MEASURE_METADATA="http://localhost:8181/fhir/metadata"

MAX_WAIT=300  # seconds
POLL_INTERVAL=5

# Phase timing
_ts() { date +%s; }
_t_start=$(_ts)
_t_prev=$(_ts)

_phase_done() {
    local label="$1"
    local _now
    _now=$(_ts)
    printf "  [PHASE] %-35s %4ds elapsed  %4ds this phase\n" \
        "$label" "$(( _now - _t_start ))" "$(( _now - _t_prev ))"
    _t_prev=$_now
}

cleanup() {
    echo ""
    echo "==> Stopping test containers..."
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true

    local _t_end
    _t_end=$(_ts)
    echo ""
    echo "==> CI Phase Summary (wall time from script start):"
    echo "  ┌─────────────────────────────────────────┬──────────────────────────────────────────┐"
    echo "  │ Phase                                   │ Cumulative wall time at end of phase     │"
    echo "  ├─────────────────────────────────────────┼──────────────────────────────────────────┤"
    printf "  │ %-39s │ %-40s │\n" "docker pull"            "${_elapsed_pull:-?}s"
    printf "  │ %-39s │ %-40s │\n" "docker compose up"      "${_elapsed_compose_up:-?}s"
    printf "  │ %-39s │ %-40s │\n" "HAPI FHIR ready"        "${_elapsed_hapi_ready:-?}s"
    printf "  │ %-39s │ %-40s │\n" "PostgreSQL ready"       "${_elapsed_pg_ready:-?}s"
    printf "  │ %-39s │ %-40s │\n" "pytest run + teardown"  "$(( _t_end - _t_start ))s total"
    echo "  └─────────────────────────────────────────┴──────────────────────────────────────────┘"
}

trap cleanup EXIT

has_explicit_pytest_target() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            *::*|*.py|tests/*|./tests/*|backend/tests/*)
                return 0
                ;;
        esac
    done
    return 1
}

echo "==> Pulling HAPI FHIR image (avoids compose startup timeout on cold pull)..."
docker pull hapiproject/hapi:v8.8.0-1
_elapsed_pull=$(( $(_ts) - _t_start ))
_phase_done "docker pull"

echo "==> Starting test infrastructure..."
docker compose -f "$COMPOSE_FILE" up -d
_elapsed_compose_up=$(( $(_ts) - _t_start ))
_phase_done "docker compose up -d"

echo "==> Waiting for HAPI FHIR instances to be ready..."
elapsed=0
while true; do
    cdr_ok=false
    measure_ok=false

    if curl -sf "$CDR_METADATA" > /dev/null 2>&1; then
        cdr_ok=true
    fi
    if curl -sf "$MEASURE_METADATA" > /dev/null 2>&1; then
        measure_ok=true
    fi

    if $cdr_ok && $measure_ok; then
        echo "==> Both HAPI FHIR instances are ready."
        break
    fi

    if [ "$elapsed" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Timed out waiting for HAPI FHIR instances after ${MAX_WAIT}s"
        echo "  CDR ready: $cdr_ok"
        echo "  Measure ready: $measure_ok"
        exit 1
    fi

    # Poll at 1 s during first 60 s, then 5 s, to keep MAX_WAIT total accurate
    if [ "$elapsed" -lt 60 ]; then
        sleep 1
        elapsed=$((elapsed + 1))
    else
        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    fi
    echo "  Waiting... (${elapsed}s / ${MAX_WAIT}s)"
done
_elapsed_hapi_ready=$(( $(_ts) - _t_start ))
_phase_done "HAPI FHIR ready"

echo "==> Waiting for PostgreSQL to be ready..."
elapsed=0
while true; do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U mct2 > /dev/null 2>&1; then
        echo "==> PostgreSQL is ready."
        break
    fi

    if [ "$elapsed" -ge 60 ]; then
        echo "ERROR: Timed out waiting for PostgreSQL after 60s"
        exit 1
    fi

    sleep 2
    elapsed=$((elapsed + 2))
done
_elapsed_pg_ready=$(( $(_ts) - _t_start ))
_phase_done "PostgreSQL ready"

echo "==> Running integration tests..."
cd "$PROJECT_ROOT/backend"
pytest_exit=0
pytest_args=(-m integration -v --tb=short)
if has_explicit_pytest_target "$@"; then
    pytest_args+=("$@")
else
    pytest_args+=(tests/integration/ "$@")
fi
python3 -m pytest "${pytest_args[@]}" || pytest_exit=$?

echo ""
if [ "$pytest_exit" -eq 0 ]; then
    echo "==> All integration tests passed."
else
    echo "==> Integration tests failed (exit code: $pytest_exit)."
fi

exit "$pytest_exit"
