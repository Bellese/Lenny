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
PREBAKED_OVERLAY="$PROJECT_ROOT/docker-compose.prebaked.yml"

CDR_METADATA="http://localhost:8180/fhir/metadata"
MEASURE_METADATA="http://localhost:8181/fhir/metadata"

MAX_WAIT=300  # seconds
POLL_INTERVAL=5

# ---------------------------------------------------------------------------
# Pre-baked image resolution (CI fast path)
# Set USE_PREBAKED=1 to pull pre-seeded GHCR images instead of running the
# full seed + reindex + ValueSet expansion on every test run.
# ---------------------------------------------------------------------------
USE_PREBAKED="${USE_PREBAKED:-0}"
REQUIRE_PREBAKED="${REQUIRE_PREBAKED:-0}"
_using_prebaked=false

if [ "$USE_PREBAKED" = "1" ]; then
    echo "==> USE_PREBAKED=1 — resolving pre-baked HAPI images from GHCR..."
    SEED_HASH=$(cd "$PROJECT_ROOT" && find seed/ seed/connectathon-bundles/ docker/seed-hapi.sh docker-compose.test.yml \
        docker/hapi-cdr-seeded.Dockerfile docker/hapi-measure-seeded.Dockerfile \
        -type f 2>/dev/null | sort | xargs sha256sum 2>/dev/null | sha256sum | cut -c1-12)
    echo "  Seed hash: ${SEED_HASH}"

    _try_pull() {
        local image="$1"
        if docker pull "$image" > /dev/null 2>&1; then
            echo "$image"
            return 0
        fi
        return 1
    }

    CDR_HASH_IMAGE="ghcr.io/bellese/mct2-hapi-cdr:${SEED_HASH}"
    CDR_LATEST_IMAGE="ghcr.io/bellese/mct2-hapi-cdr:latest"
    MEASURE_HASH_IMAGE="ghcr.io/bellese/mct2-hapi-measure:${SEED_HASH}"
    MEASURE_LATEST_IMAGE="ghcr.io/bellese/mct2-hapi-measure:latest"
    VANILLA_IMAGE="hapiproject/hapi:v8.8.0-1"

    if _cdr_image=$(_try_pull "$CDR_HASH_IMAGE") && _measure_image=$(_try_pull "$MEASURE_HASH_IMAGE"); then
        echo "  Using hash-tagged prebaked images."
        _using_prebaked=true
    elif _cdr_image=$(_try_pull "$CDR_LATEST_IMAGE") && _measure_image=$(_try_pull "$MEASURE_LATEST_IMAGE"); then
        echo "  WARNING: hash-tagged images not found; falling back to :latest prebaked images."
        _using_prebaked=true
    else
        if [ "$REQUIRE_PREBAKED" = "1" ]; then
            echo ""
            echo "ERROR: REQUIRE_PREBAKED=1 but no prebaked GHCR image was reachable."
            echo "       Tried: $CDR_HASH_IMAGE"
            echo "              $CDR_LATEST_IMAGE"
            echo "       This usually means a GHCR auth failure or the image has not been built yet."
            echo "       Fix options:"
            echo "         1. Log in to GHCR: docker login ghcr.io -u <user> -p <token>"
            echo "         2. Trigger a bake: gh workflow run bake-hapi-image.yml"
            echo "         3. Allow vanilla fallback: unset REQUIRE_PREBAKED (CI correctness not guaranteed)"
            echo ""
            echo "USED PREBAKED: no — EXITING (REQUIRE_PREBAKED=1)"
            exit 1
        fi
        echo "  WARNING: prebaked images unavailable; falling back to vanilla $VANILLA_IMAGE + full seed."
        _cdr_image="$VANILLA_IMAGE"
        _measure_image="$VANILLA_IMAGE"
    fi

    export HAPI_CDR_IMAGE="$_cdr_image"
    export HAPI_MEASURE_IMAGE="$_measure_image"
    echo "  CDR image:     $HAPI_CDR_IMAGE"
    echo "  Measure image: $HAPI_MEASURE_IMAGE"

    if $_using_prebaked; then
        export HAPI_PREBAKED=1
        echo "USED PREBAKED: yes"
    else
        echo "USED PREBAKED: no (vanilla fallback — seed will run in-container)"
    fi
fi

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

_compose_up() {
    if $_using_prebaked; then
        docker compose -f "$COMPOSE_FILE" -f "$PREBAKED_OVERLAY" up -d
    else
        docker compose -f "$COMPOSE_FILE" up -d
    fi
}

_compose_down() {
    if $_using_prebaked; then
        docker compose -f "$COMPOSE_FILE" -f "$PREBAKED_OVERLAY" down -v 2>/dev/null || true
    else
        docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    fi
}

cleanup() {
    echo ""
    echo "==> Stopping test containers..."
    _compose_down

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

if ! $_using_prebaked; then
    echo "==> Pulling HAPI FHIR image (avoids compose startup timeout on cold pull)..."
    docker pull hapiproject/hapi:v8.8.0-1
fi
_elapsed_pull=$(( $(_ts) - _t_start ))
_phase_done "docker pull"

echo "==> Starting test infrastructure..."
_compose_up
_elapsed_compose_up=$(( $(_ts) - _t_start ))
_phase_done "docker compose up -d"

echo "==> Waiting for HAPI FHIR instances to be ready..."
_poll_count=0
_poll_interval=1
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

    _poll_count=$(( _poll_count + 1 ))
    if [ "$_poll_count" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Timed out waiting for HAPI FHIR instances after ${MAX_WAIT} polls"
        echo "  CDR ready: $cdr_ok"
        echo "  Measure ready: $measure_ok"
        exit 1
    fi

    # Exponential-ish backoff: 1s for first 10 polls, 2s up to 20, then 5s
    if [ "$_poll_count" -gt 20 ]; then
        _poll_interval=5
    elif [ "$_poll_count" -gt 10 ]; then
        _poll_interval=2
    fi
    sleep "$_poll_interval"
    echo "  Waiting... (poll ${_poll_count}/${MAX_WAIT}, interval ${_poll_interval}s)"
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
