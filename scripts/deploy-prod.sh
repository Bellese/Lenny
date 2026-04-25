#!/usr/bin/env bash
# deploy-prod.sh — Single authoritative entry point for all prod deployments.
#
# Boot flow (full deploy, no arguments):
#   1. Preflight checks (root, fetch script present)
#   2. env -i → fetch-prod-secrets.sh (writes /run/leonard/env)
#   3. Verify /run/leonard/env; extract POSTGRES_PASSWORD to Docker secret file
#   4. docker compose up -d db
#   5. Wait for db healthcheck to pass (12 × 5s = 60s max)
#   6. scripts/reconcile-db-password.sh
#   7. docker compose up -d (remaining services)
#   8. Health check: curl https://api.98-89-219-217.nip.io/health (24 × 5s = 2 min max)
#
# --post-db-restart mode:
#   Runs only steps 1–3 and 6 (preflight, fetch, extract secret, reconcile).
#   Use this when the db container has been restarted manually and you need
#   to re-sync the role password without touching the rest of the stack.
#
# Usage:
#   sudo ./scripts/deploy-prod.sh
#   sudo ./scripts/deploy-prod.sh --post-db-restart
#
# Optional env vars:
#   LEONARD_DIR            Root of the project (default: parent of this script).
#   LEONARD_SSM_VERSION    If set, passed through to fetch-prod-secrets.sh for
#                          rollback deploys (fetch a specific SSM version).
#
# Security:
#   - No set -x anywhere — would leak POSTGRES_PASSWORD to logs
#   - Password value is never printed to stdout or stderr
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── path resolution ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEONARD_DIR="${LEONARD_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export LEONARD_DIR

COMPOSE_BASE="${LEONARD_DIR}/docker-compose.yml"
COMPOSE_PROD="${LEONARD_DIR}/docker-compose.prod.yml"
# Pre-baked HAPI images: IGs + seed data baked into image layer (no /data/hapi
# volume needed). Required by per-measure reset architecture — recreating the
# container without baked data would re-fetch IGs from the internet (60-120s).
COMPOSE_PREBAKED="${LEONARD_DIR}/docker-compose.prebaked.yml"
COMPOSE=( docker compose -f "$COMPOSE_BASE" -f "$COMPOSE_PROD" -f "$COMPOSE_PREBAKED" )

readonly ENV_DIR="/run/leonard"
readonly ENV_FILE="${ENV_DIR}/env"
readonly SECRET_FILE="${ENV_DIR}/POSTGRES_PASSWORD"
readonly FETCH_SCRIPT="${LEONARD_DIR}/scripts/fetch-prod-secrets.sh"
readonly RECONCILE_SCRIPT="${LEONARD_DIR}/scripts/reconcile-db-password.sh"
readonly HEALTH_URL="https://api.98-89-219-217.nip.io/health"

# ── argument parsing ───────────────────────────────────────────────────────────
POST_DB_RESTART=0
if [[ "${1:-}" == "--post-db-restart" ]]; then
    POST_DB_RESTART=1
elif [[ $# -gt 0 ]]; then
    printf '[!] Unknown argument: %s\n' "$1" >&2
    printf '[!] Usage: %s [--post-db-restart]\n' "$0" >&2
    exit 1
fi

# ── helpers ───────────────────────────────────────────────────────────────────
die() {
    local code="${1:-1}"
    shift
    printf '[!] %s\n' "$*" >&2
    exit "$code"
}

# ── step 1: preflight checks ──────────────────────────────────────────────────
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die 1 "This script must be run as root (try: sudo $0)"
fi

if [[ ! -f "$FETCH_SCRIPT" ]]; then
    die 1 "fetch-prod-secrets.sh not found at '${FETCH_SCRIPT}'"
fi
if [[ ! -x "$FETCH_SCRIPT" ]]; then
    die 1 "fetch-prod-secrets.sh is not executable at '${FETCH_SCRIPT}'"
fi
if [[ ! -f "$RECONCILE_SCRIPT" ]]; then
    die 1 "reconcile-db-password.sh not found at '${RECONCILE_SCRIPT}'"
fi
if [[ ! -x "$RECONCILE_SCRIPT" ]]; then
    die 1 "reconcile-db-password.sh is not executable at '${RECONCILE_SCRIPT}'"
fi

# ── step 2: fetch secrets ─────────────────────────────────────────────────────
# env -i ensures any POSTGRES_PASSWORD in the caller's environment cannot
# contaminate the fetch process. LEONARD_SSM_VERSION is propagated explicitly
# to support rollback deploys (fetching a specific SSM version).
printf '[+] Fetching prod secrets...\n'
env -i \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/root" \
    ${LEONARD_SSM_VERSION:+LEONARD_SSM_VERSION="$LEONARD_SSM_VERSION"} \
    "$FETCH_SCRIPT"

# ── step 3: verify env file and extract Docker secret ─────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    die 1 "fetch-prod-secrets.sh ran but '${ENV_FILE}' was not created"
fi
if [[ ! -r "$ENV_FILE" ]]; then
    die 1 "'${ENV_FILE}' exists but is not readable"
fi
if ! grep -qE '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
    die 1 "'${ENV_FILE}' does not contain a POSTGRES_PASSWORD= line"
fi

# Extract POSTGRES_PASSWORD to the Docker-secrets file.
# Use install(1) to write mode 0600 atomically — avoids a window where the
# file is readable at umask permissions before chmod runs.
# Note: grep+cut emits a trailing newline; postgres POSTGRES_PASSWORD_FILE
# trims trailing whitespace so this is safe.
printf '[+] Extracting Docker secret file...\n'
grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" \
    | cut -d= -f2- \
    | install -o root -g root -m 0600 /dev/stdin "$SECRET_FILE"

# ── docker login to GHCR (if token was staged by the deploy workflow) ─────────
# The deploy workflow writes GITHUB_TOKEN to /leonard/prod/GHCR_TOKEN as a
# SecureString just before invoking this script and deletes it after. Manual
# runs without that staging step skip login and will fail loudly at compose
# pull time if the prebaked overlay references private GHCR images.
if grep -qE '^GHCR_TOKEN=' "$ENV_FILE"; then
    printf '[+] Logging in to GHCR for pre-baked HAPI image pulls...\n'
    GHCR_TOKEN_VALUE=$(grep -E '^GHCR_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
    # Username can be any non-empty string when the password is a GitHub token;
    # GHCR ignores it. Use a label that's recognizable in `docker logout` output.
    if ! printf '%s' "$GHCR_TOKEN_VALUE" \
        | docker login ghcr.io --username leonard-deploy --password-stdin >/dev/null 2>&1; then
        unset GHCR_TOKEN_VALUE
        die 1 "docker login ghcr.io failed — check workflow packages:read scope"
    fi
    unset GHCR_TOKEN_VALUE
    printf '[+] GHCR login OK\n'
else
    printf '[!] GHCR_TOKEN not in %s — skipping ghcr.io login.\n' "$ENV_FILE"
    printf '    Manual deploys cannot pull the private pre-baked HAPI images.\n'
    printf '    Trigger via the deploy workflow or omit docker-compose.prebaked.yml.\n'
fi

# ── shared preflight done; branch on mode ─────────────────────────────────────
if [[ "$POST_DB_RESTART" -eq 1 ]]; then
    # ── --post-db-restart: reconcile only, no compose operations ──────────────
    printf '[+] --post-db-restart mode: reconciling DB password...\n'
    LEONARD_DIR="$LEONARD_DIR" "$RECONCILE_SCRIPT"
    printf '[+] DB password reconciled — stack not touched\n'
    exit 0
fi

# ── step 4: bring up db ───────────────────────────────────────────────────────
printf '[+] Starting db container...\n'
"${COMPOSE[@]}" up -d db

# ── step 5: wait for db healthcheck ──────────────────────────────────────────
printf '[+] Waiting for db to become healthy...\n'
for i in $(seq 1 12); do
    # Get the container ID; may be empty for a beat right after up -d — treat
    # as not-healthy-yet rather than erroring.
    container_id=$("${COMPOSE[@]}" ps -q db 2>/dev/null || true)
    if [[ -n "$container_id" ]]; then
        health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || echo "unknown")
        if [[ "$health" == "healthy" ]]; then
            printf '[+] db is healthy\n'
            break
        fi
        if [[ "$health" == "unhealthy" ]]; then
            die 1 "db container became unhealthy — check: docker compose logs db"
        fi
    fi
    if [[ "$i" -eq 12 ]]; then
        die 1 "db healthcheck timed out after 60s"
    fi
    sleep 5
done

# ── step 6: reconcile DB password ─────────────────────────────────────────────
printf '[+] Reconciling DB password...\n'
LEONARD_DIR="$LEONARD_DIR" "$RECONCILE_SCRIPT"

# ── step 7: build and bring up remaining services ────────────────────────────
# --build ensures locally-built images (backend, frontend, seed) are rebuilt
# from the current Dockerfile so the new entrypoint and code are picked up.
# Services using pre-built images (hapi, postgres, caddy) are unaffected.
printf '[+] Building and starting remaining services...\n'
"${COMPOSE[@]}" up -d --build

# ── step 8: health check ──────────────────────────────────────────────────────
printf '[+] Waiting for API health check...\n'
for i in $(seq 1 24); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        printf '[+] Deploy complete — %s is healthy\n' "$HEALTH_URL"
        exit 0
    fi
    if [[ "$i" -eq 24 ]]; then
        die 1 "Health check failed after 2 min — ${HEALTH_URL} did not respond"
    fi
    sleep 5
done
