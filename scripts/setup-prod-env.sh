#!/bin/bash
# Create the production .env file required by docker-compose.prod.yml.
#
# Run this once on the EC2 instance before the first `docker compose up`.
# Without this file, POSTGRES_PASSWORD and CADDY_HOST substitute as empty
# strings, causing the backend to fail on any restart after `docker compose down`.
#
# Usage:
#   ./scripts/setup-prod-env.sh [--force]
#
# Options:
#   --force   Overwrite an existing .env file

set -euo pipefail

FORCE=false
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=true
done

ENV_FILE=".env"

if [ -f "$ENV_FILE" ] && [ "$FORCE" = false ]; then
  echo "Error: $ENV_FILE already exists. Use --force to overwrite."
  exit 1
fi

# --- CADDY_HOST ---
if [ -z "${CADDY_HOST:-}" ]; then
  read -rp "CADDY_HOST (e.g. 98-89-219-217.nip.io): " CADDY_HOST
fi
if [ -z "$CADDY_HOST" ]; then
  echo "Error: CADDY_HOST is required."
  exit 1
fi

# --- POSTGRES_PASSWORD ---
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  # Generate a random 32-char password if not provided
  POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)
  echo "Generated POSTGRES_PASSWORD (save this somewhere safe):"
  echo "  POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
fi

cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
CADDY_HOST=$CADDY_HOST
EOF

echo ""
echo "Written: $ENV_FILE"
echo ""
echo "Next steps:"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
