#!/bin/bash
# seed-hapi.sh — run at Docker image build time to bake seed data into H2 storage.
#
# Starts HAPI in the background, loads seed bundles, waits for reindex (and
# optionally ValueSet pre-expansion), then sends SIGTERM so H2 flushes to disk.
#
# Environment variables:
#   SEED_TYPE  "cdr" (default) or "measure"
#              cdr     → POST patient-bundle.json only
#              measure → POST measure-bundle.json then patient-bundle.json
set -euo pipefail

HAPI_BASE="http://localhost:8080/fhir"
SEED_DIR="/seed"
METADATA_TIMEOUT=300
METADATA_POLL=2
REINDEX_TIMEOUT=300
REINDEX_POLL=1
VALUESET_TIMEOUT=600
VALUESET_POLL=2

SEED_TYPE="${SEED_TYPE:-cdr}"

log() {
    echo "[$(date +%T)] $*"
}

# ---------------------------------------------------------------------------
# Start HAPI in the background
# ---------------------------------------------------------------------------
log "Starting HAPI (SEED_TYPE=${SEED_TYPE}) ..."
java \
  --class-path /app/main.war \
  "-Dloader.path=main.war!/WEB-INF/classes/,main.war!/WEB-INF/,/app/extra-classes" \
  org.springframework.boot.loader.PropertiesLauncher &
HAPI_PID=$!
log "HAPI PID=${HAPI_PID}"

# ---------------------------------------------------------------------------
# Wait for /fhir/metadata
# ---------------------------------------------------------------------------
log "Waiting for HAPI metadata (max ${METADATA_TIMEOUT}s) ..."
deadline=$(( $(date +%s) + METADATA_TIMEOUT ))
until curl -sf "${HAPI_BASE}/metadata" -o /dev/null; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        log "ERROR: HAPI did not respond within ${METADATA_TIMEOUT}s"
        kill "${HAPI_PID}" 2>/dev/null || true
        exit 1
    fi
    sleep "${METADATA_POLL}"
done
log "HAPI is up."

# ---------------------------------------------------------------------------
# Load seed bundles
# ---------------------------------------------------------------------------
post_bundle() {
    local file="$1"
    local label="$2"
    log "POSTing ${label} ..."
    curl -sf -X POST \
        -H "Content-Type: application/fhir+json" \
        --data-binary "@${file}" \
        "${HAPI_BASE}" \
        -o /dev/null
    log "${label} loaded."
}

if [ "${SEED_TYPE}" = "measure" ]; then
    post_bundle "${SEED_DIR}/measure-bundle.json" "measure-bundle.json"
fi

# Both CDR and measure servers receive patient data.
# (Measure server needs patient data because $evaluate-measure resolves
# patient resources from its own HAPI instance.)
post_bundle "${SEED_DIR}/patient-bundle.json" "patient-bundle.json"

# ---------------------------------------------------------------------------
# Extract probe IDs from patient-bundle.json (first Encounter)
# ---------------------------------------------------------------------------
log "Extracting probe patient/encounter IDs ..."
PROBE_ENC_ID=$(jq -r '
    [.entry[] | select(.resource.resourceType == "Encounter")]
    | first | .resource.id' \
    "${SEED_DIR}/patient-bundle.json")

PROBE_PATIENT_ID=$(jq -r '
    [.entry[] | select(.resource.resourceType == "Encounter")]
    | first | .resource.subject.reference
    | ltrimstr("Patient/")' \
    "${SEED_DIR}/patient-bundle.json")

log "Probe patient=${PROBE_PATIENT_ID} encounter=${PROBE_ENC_ID}"

# ---------------------------------------------------------------------------
# Trigger $reindex and wait for reference search params to settle
# ---------------------------------------------------------------------------
log "Triggering \$reindex ..."
reindex_body='{"resourceType":"Parameters","parameter":[{"name":"type","valueString":"Encounter"}]}'
reindex_status=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/fhir+json" \
    -d "${reindex_body}" \
    "${HAPI_BASE}/\$reindex")
if [ "${reindex_status}" -ge 400 ]; then
    log "WARNING: \$reindex returned HTTP ${reindex_status} — continuing anyway"
fi

log "Polling Encounter?patient=${PROBE_PATIENT_ID} (max ${REINDEX_TIMEOUT}s) ..."
deadline=$(( $(date +%s) + REINDEX_TIMEOUT ))
until [ "$(curl -sf "${HAPI_BASE}/Encounter?patient=${PROBE_PATIENT_ID}&_count=1" \
             | jq -r '.entry | length' 2>/dev/null || echo 0)" -gt 0 ]; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        log "ERROR: reference-param indexing did not complete within ${REINDEX_TIMEOUT}s"
        kill "${HAPI_PID}" 2>/dev/null || true
        exit 1
    fi
    sleep "${REINDEX_POLL}"
done
log "\$reindex complete."

# ---------------------------------------------------------------------------
# ValueSet pre-expansion (measure server only)
# ---------------------------------------------------------------------------
if [ "${SEED_TYPE}" = "measure" ]; then
    log "Polling ValueSet pre-expansion (max ${VALUESET_TIMEOUT}s) ..."
    deadline=$(( $(date +%s) + VALUESET_TIMEOUT ))

    # Collect IDs of ValueSets with >900 compose concepts (need pre-expansion)
    large_ids=$(jq -r '
        .entry[]
        | select(.resource.resourceType == "ValueSet")
        | . as $entry
        | (.resource.compose.include // []
           | map(.concept // [] | length)
           | add // 0) as $cnt
        | select($cnt >= 900)
        | $entry.resource.id' \
        "${SEED_DIR}/measure-bundle.json" 2>/dev/null || true)

    if [ -z "${large_ids}" ]; then
        log "No large ValueSets found — skipping pre-expansion poll."
    else
        log "Large ValueSets to expand: $(echo "${large_ids}" | wc -l | tr -d ' ')"
        pending="${large_ids}"
        while [ -n "${pending}" ]; do
            if [ "$(date +%s)" -ge "${deadline}" ]; then
                log "WARNING: ValueSet pre-expansion did not complete within ${VALUESET_TIMEOUT}s — continuing"
                break
            fi
            still_pending=""
            while IFS= read -r vs_id; do
                [ -z "${vs_id}" ] && continue
                status=$(curl -s -o /dev/null -w "%{http_code}" \
                    "${HAPI_BASE}/ValueSet/${vs_id}/\$expand?count=2")
                if [ "${status}" != "200" ]; then
                    still_pending="${still_pending}${vs_id}"$'\n'
                fi
            done <<< "${pending}"
            pending="${still_pending}"
            if [ -n "${pending}" ]; then
                sleep "${VALUESET_POLL}"
            fi
        done
        log "ValueSet pre-expansion complete."
    fi
fi

# ---------------------------------------------------------------------------
# Stop HAPI cleanly so H2 flushes to disk
# ---------------------------------------------------------------------------
log "Sending SIGTERM to HAPI (PID=${HAPI_PID}) ..."
kill -TERM "${HAPI_PID}"
wait "${HAPI_PID}" || true
log "HAPI stopped. Seed complete."
