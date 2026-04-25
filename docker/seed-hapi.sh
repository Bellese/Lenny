#!/bin/bash
# seed-hapi.sh — run from the CI workflow runner to seed a live HAPI container.
#
# This script runs ON THE RUNNER (not inside the container) because the base
# hapiproject/hapi image is distroless and has no shell. The target HAPI
# container must already be started with "docker run" before calling this script.
#
# Environment variables:
#   HAPI_PORT   Port on localhost that maps to the container's 8080 (default: 8080)
#   SEED_TYPE   "cdr" (default), "measure", or "igs"
#               cdr     → POST patient-bundle.json only, $reindex, no ValueSet expansion poll
#               measure → POST measure-bundle.json + patient-bundle.json, $reindex, ValueSet expansion poll
#               igs     → wait for IGs to install via Spring env vars; no bundle posts, no $reindex.
#                         Use for the per-measure-isolation HAPI image (no measure or patient
#                         data baked in, so reset gives a truly empty engine).
set -euo pipefail

HAPI_PORT="${HAPI_PORT:-8080}"
HAPI_BASE="http://localhost:${HAPI_PORT}/fhir"
SEED_TYPE="${SEED_TYPE:-cdr}"

METADATA_TIMEOUT=600
METADATA_POLL=2
REINDEX_TIMEOUT=300
REINDEX_POLL=1
VALUESET_TIMEOUT=600
VALUESET_POLL=2

log() {
    echo "[$(date +%T)] $*"
}

# ---------------------------------------------------------------------------
# Wait for /fhir/metadata
# ---------------------------------------------------------------------------
log "Waiting for HAPI at ${HAPI_BASE}/metadata (max ${METADATA_TIMEOUT}s, SEED_TYPE=${SEED_TYPE}) ..."
deadline=$(( $(date +%s) + METADATA_TIMEOUT ))
until curl -sf "${HAPI_BASE}/metadata" -o /dev/null; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        log "ERROR: HAPI did not respond within ${METADATA_TIMEOUT}s"
        exit 1
    fi
    sleep "${METADATA_POLL}"
done
log "HAPI is up."

# ---------------------------------------------------------------------------
# IGs-only short-circuit
# ---------------------------------------------------------------------------
# Spring auto-installs IGs from the env vars set in the image's Dockerfile
# during HAPI startup. The /metadata 200 above means startup completed and
# IGs are loaded. No data to seed, no reindex needed (no Encounters).
if [ "${SEED_TYPE}" = "igs" ]; then
    log "SEED_TYPE=igs: IGs installed by HAPI on startup. No bundles posted, no \$reindex."
    log "Seed complete."
    exit 0
fi

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
    post_bundle "seed/measure-bundle.json" "measure-bundle.json"
fi

# Both CDR and measure servers receive patient data.
# (Measure server needs patient data because $evaluate-measure resolves
# patient resources from its own HAPI instance.)
post_bundle "seed/patient-bundle.json" "patient-bundle.json"

# ---------------------------------------------------------------------------
# Extract probe IDs from patient-bundle.json (first Encounter)
# ---------------------------------------------------------------------------
log "Extracting probe patient/encounter IDs ..."
PROBE_PATIENT_ID=$(jq -r '
    [.entry[] | select(.resource.resourceType == "Encounter")]
    | first | .resource.subject.reference
    | ltrimstr("Patient/")' \
    seed/patient-bundle.json)

log "Probe patient=${PROBE_PATIENT_ID}"

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

    large_ids=$(jq -r '
        .entry[]
        | select(.resource.resourceType == "ValueSet")
        | . as $entry
        | (.resource.compose.include // []
           | map(.concept // [] | length)
           | add // 0) as $cnt
        | select($cnt >= 900)
        | $entry.resource.id' \
        seed/measure-bundle.json 2>/dev/null || true)

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

log "Seed complete."
