#!/usr/bin/env bash
#
# Run MCT2 integration tests against real HAPI FHIR instances.
#
# Usage:
#   ./scripts/run-integration-tests.sh
#
# This script:
#   1. Starts docker-compose.test.yml in background
#   2. Waits for health checks to pass
#   3. Runs pytest -m integration
#   4. Stops and removes test containers
#   5. Exits with pytest's exit code

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.test.yml"

CDR_METADATA="http://localhost:8180/fhir/metadata"
MEASURE_METADATA="http://localhost:8181/fhir/metadata"

MAX_WAIT=300  # seconds
POLL_INTERVAL=5

cleanup() {
    echo ""
    echo "==> Stopping test containers..."
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
}

trap cleanup EXIT

echo "==> Pulling HAPI FHIR image (avoids compose startup timeout on cold pull)..."
docker pull hapiproject/hapi:v8.6.0-1

echo "==> Starting test infrastructure..."
docker compose -f "$COMPOSE_FILE" up -d

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

    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
    echo "  Waiting... (${elapsed}s / ${MAX_WAIT}s)"
done

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

echo "==> Running integration tests..."
cd "$PROJECT_ROOT/backend"
pip install -r requirements.txt -r requirements-test.txt
pytest_exit=0
python -m pytest tests/integration/ -m integration -v --tb=short || pytest_exit=$?

echo ""
if [ "$pytest_exit" -eq 0 ]; then
    echo "==> All integration tests passed."
else
    echo "==> Integration tests failed (exit code: $pytest_exit)."
fi

exit "$pytest_exit"
