#!/bin/sh
set -e
# In prod, assemble DATABASE_URL from Docker secret (file is 0600 root-only).
# In local dev, DATABASE_URL is already set via docker-compose.yml.
if [ -s /run/secrets/postgres_password ]; then
  PW=$(cat /run/secrets/postgres_password)
  export DATABASE_URL="postgresql+asyncpg://mct2:${PW}@db:5432/mct2"
  unset PW
fi
if [ -s /run/secrets/api_token ]; then
  export API_TOKEN=$(cat /run/secrets/api_token)
fi
# Drop from root to the app user before exec so the process never runs as root.
exec gosu app "$@"
