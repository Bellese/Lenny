#!/usr/bin/env bash
# reconcile-db-password.sh — Reconcile the postgres DB volume's embedded
# password to match the current SSM-sourced password.
#
# Runs on every deploy after the `db` container is healthy, before the
# backend starts. Idempotent: ALTER ROLE is a no-op when the password is
# already correct.
#
# Exit codes:
#   0  success (password reconciled or already matches)
#   1  env file missing or POSTGRES_PASSWORD not set
#   2  docker compose exec / psql call failed
#
# Usage:
#   sudo ./scripts/reconcile-db-password.sh
#
# Optional env vars:
#   LEONARD_DIR   Root directory containing docker-compose.yml (default:
#                 parent of this script, resolved via realpath).
#   LEONARD_ENV_FILE  Full path to the env file (default: /run/leonard/env).
#
# Security:
#   - No set -x  (would leak PGPASSWORD to logs)
#   - PGPASSWORD passed only in the subprocess env via `exec -e`
#   - Password value is never printed to stdout or stderr

set -euo pipefail

# ── path resolution ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEONARD_DIR="${LEONARD_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_BASE="${LEONARD_DIR}/docker-compose.yml"
COMPOSE_PROD="${LEONARD_DIR}/docker-compose.prod.yml"

# ── constants ──────────────────────────────────────────────────────────────────
readonly ENV_FILE="${LEONARD_ENV_FILE:-/run/leonard/env}"

# ── helpers ───────────────────────────────────────────────────────────────────
die() {
    local code="${1:-1}"
    shift
    printf '[!] reconcile-db-password: %s\n' "$*" >&2
    exit "$code"
}

# ── read password from env file ───────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    die 1 "Env file '${ENV_FILE}' not found. Run fetch-prod-secrets.sh first."
fi

# Read only the POSTGRES_PASSWORD line — avoid sourcing the whole file.
PW_LINE="$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | head -1 || true)"
if [[ -z "$PW_LINE" ]]; then
    die 1 "POSTGRES_PASSWORD not set in '${ENV_FILE}'."
fi

# Strip the key prefix; everything after the first '=' is the value.
PW="${PW_LINE#POSTGRES_PASSWORD=}"
if [[ -z "$PW" ]]; then
    die 1 "POSTGRES_PASSWORD is empty in '${ENV_FILE}'."
fi

# ── run ALTER ROLE via psql stdin ─────────────────────────────────────────────
# Pipe the SQL via stdin to avoid psql variable-interpolation issues with
# `docker compose exec -c`. The password is safe for direct SQL string
# substitution: fetch-prod-secrets.sh validates it against
# ^[A-Za-z0-9+/=_.-]{16,128}$ so it can never contain single quotes.
# PGPASSWORD is scoped to the subprocess only (via `exec -e`).
if ! out=$(printf "ALTER ROLE mct2 PASSWORD '%s';" "$PW" | docker compose \
    -f "$COMPOSE_BASE" \
    -f "$COMPOSE_PROD" \
    exec -T \
    -e PGPASSWORD="$PW" \
    db \
    psql -U mct2 -d mct2 \
    -v ON_ERROR_STOP=1 2>&1); then
    # Redact the password from any captured output before printing.
    safe_out="${out//$PW/***REDACTED***}"
    printf '[!] reconcile-db-password: ALTER ROLE via psql failed\n' >&2
    printf '%s\n' "$safe_out" >&2
    exit 2
fi

printf '[+] DB password reconciled\n'
