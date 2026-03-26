#!/bin/sh
set -e

# ============================================================
# MCT2 Seed Data Loader
# Waits for HAPI FHIR instances, then loads demo data.
# Idempotent: uses PUT-based transaction bundles (safe to re-run).
# ============================================================

CDR_URL="${CDR_URL:-http://hapi-fhir-cdr:8080/fhir}"
MEASURE_URL="${MEASURE_URL:-http://hapi-fhir-measure:8080/fhir}"
SEED_DIR="${SEED_DIR:-/seed}"
MAX_RETRIES=60
RETRY_INTERVAL=5

log() {
  echo "[seed] $(date '+%Y-%m-%d %H:%M:%S') $1"
}

wait_for_server() {
  local url="$1/metadata"
  local name="$2"
  local attempt=1

  log "Waiting for $name at $url ..."
  while [ "$attempt" -le "$MAX_RETRIES" ]; do
    if curl -sf -o /dev/null "$url" 2>/dev/null; then
      log "$name is ready."
      return 0
    fi
    log "$name not ready yet (attempt $attempt/$MAX_RETRIES). Retrying in ${RETRY_INTERVAL}s..."
    sleep "$RETRY_INTERVAL"
    attempt=$((attempt + 1))
  done

  log "ERROR: $name did not become ready after $((MAX_RETRIES * RETRY_INTERVAL))s. Exiting."
  exit 1
}

post_bundle() {
  local url="$1"
  local file="$2"
  local name="$3"

  log "Loading $name to $url ..."
  response=$(curl -s -w "\n%{http_code}" -X POST "$url" \
    -H "Content-Type: application/fhir+json" \
    -d @"$file")

  http_code=$(echo "$response" | tail -n1)
  body=$(echo "$response" | sed '$d')

  if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
    log "$name loaded successfully (HTTP $http_code)."
  else
    log "ERROR: Failed to load $name (HTTP $http_code)."
    log "Response: $body"
    exit 1
  fi
}

verify_data() {
  local url="$1"
  local resource_type="$2"
  local name="$3"
  local attempt=1

  while [ "$attempt" -le "$MAX_RETRIES" ]; do
    count=$(curl -s "$url/${resource_type}?_summary=count" | sed -n 's/.*"total":\([0-9]*\).*/\1/p')
    if [ -n "$count" ] && [ "$count" -gt 0 ]; then
      log "Verified: $count $resource_type resource(s) found on $name."
      return 0
    fi
    log "Waiting for $resource_type indexing on $name (attempt $attempt/$MAX_RETRIES)..."
    sleep "$RETRY_INTERVAL"
    attempt=$((attempt + 1))
  done

  log "WARNING: Could not verify $resource_type resources on $name after $MAX_RETRIES attempts."
}

# ============================================================
# Main
# ============================================================

log "Starting MCT2 seed data loader..."

# Step 1: Wait for both HAPI FHIR servers
wait_for_server "$CDR_URL" "HAPI FHIR CDR"
wait_for_server "$MEASURE_URL" "HAPI FHIR Measure Engine"

# Step 2: Load patient data into CDR
post_bundle "$CDR_URL" "$SEED_DIR/patient-bundle.json" "patient bundle (CDR)"

# Step 3: Load measure data into Measure Engine
post_bundle "$MEASURE_URL" "$SEED_DIR/measure-bundle.json" "measure bundle (Measure Engine)"

# Step 4: Verify data was loaded
log "Verifying loaded data..."
verify_data "$CDR_URL" "Patient" "CDR"
verify_data "$CDR_URL" "Condition" "CDR"
verify_data "$CDR_URL" "Observation" "CDR"
verify_data "$MEASURE_URL" "Measure" "Measure Engine"
verify_data "$MEASURE_URL" "Library" "Measure Engine"

# Done
log "============================================"
log "  MCT2 demo data loaded successfully!"
log "  CDR:     55 patients with clinical data"
log "  Engine:  1 measure (CMS122 - Diabetes HbA1c)"
log "============================================"
