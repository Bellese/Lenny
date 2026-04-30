#!/usr/bin/env bash
#
# connectathon-rehearsal.sh — Pre-connectathon cold-start operator workflow check.
#
# Validates the full connectathon workflow end-to-end:
#   1. Cold-start Docker services (unless --no-restart)
#   2. Poll Lenny health until green (all services connected)
#   3. Assert all 12 connectathon measures are loaded via the API
#   4. Trigger evaluation for one known-passing measure; poll until complete
#   5. Print a 12-row status table: measure_id | loaded | evaluated | populations_match | notes
#   6. Exit nonzero if any measure row fails
#
# Usage:
#   ./scripts/connectathon-rehearsal.sh [--no-restart]
#
# Flags:
#   --no-restart    Skip docker compose down/up (use running containers)
#
# Requirements: bash, curl, jq, docker
#
# Output goes to console AND is appended to rehearsal.log (repo root).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_ROOT/rehearsal.log"
MANIFEST="$PROJECT_ROOT/seed/connectathon-bundles/manifest.json"
API_BASE="http://localhost:8000"

HEALTH_TIMEOUT=300   # seconds to wait for Lenny to be healthy
HEALTH_POLL=5        # seconds between health polls
JOB_TIMEOUT=300      # seconds to wait for a job to complete
JOB_POLL=5           # seconds between job polls

NO_RESTART=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

for arg in "$@"; do
    case "$arg" in
        --no-restart)
            NO_RESTART=true
            ;;
        -h|--help)
            sed -n '/^# Usage:/,/^$/p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Logging — tee to both stdout and rehearsal.log
# ---------------------------------------------------------------------------

# Ensure log file exists before exec redirect
touch "$LOG_FILE"
# Redirect all output (stdout+stderr) through tee into the log file
exec > >(tee -a "$LOG_FILE") 2>&1

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

SCRIPT_START=$(date +%s)

elapsed_since() {
    local start="$1"
    local now
    now=$(date +%s)
    echo $((now - start))
}

fmt_duration() {
    local secs="$1"
    if [ "$secs" -ge 60 ]; then
        printf "%dm%02ds" $((secs / 60)) $((secs % 60))
    else
        printf "%ds" "$secs"
    fi
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

header() {
    echo ""
    echo "================================================================"
    echo "  $*"
    echo "================================================================"
}

step() {
    echo ""
    echo "--> $*"
}

ok()   { echo "    [OK]  $*"; }
warn() { echo "    [WARN] $*"; }
fail() { echo "    [FAIL] $*"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

header "Lenny Connectathon Rehearsal — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Project root : $PROJECT_ROOT"
echo "  Manifest     : $MANIFEST"
echo "  API base     : $API_BASE"
echo "  Log file     : $LOG_FILE"
echo "  No-restart   : $NO_RESTART"

# Verify dependencies
for cmd in curl jq docker; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Required command not found: $cmd" >&2
        exit 1
    fi
done

if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: Manifest not found: $MANIFEST" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Docker cold-start (unless --no-restart)
# ---------------------------------------------------------------------------

header "Step 1: Docker Services"

DOCKER_START=$(date +%s)

if $NO_RESTART; then
    step "Skipping docker compose restart (--no-restart)"
else
    step "Tearing down existing services..."
    docker compose -f "$PROJECT_ROOT/docker-compose.yml" down -v

    step "Starting services fresh..."
    docker compose -f "$PROJECT_ROOT/docker-compose.yml" up -d
    ok "Services started"
fi

DOCKER_ELAPSED=$(elapsed_since "$DOCKER_START")
ok "Docker step done in $(fmt_duration "$DOCKER_ELAPSED")"

# ---------------------------------------------------------------------------
# Step 2: Poll health endpoint until green
# ---------------------------------------------------------------------------

header "Step 2: Waiting for Lenny Health"

HEALTH_START=$(date +%s)
health_elapsed=0

step "Polling $API_BASE/health (timeout: ${HEALTH_TIMEOUT}s)..."

while true; do
    # Attempt health check; curl returns nonzero on connection failure
    http_body=$(curl -sf --max-time 5 "$API_BASE/health" 2>/dev/null || true)

    if [ -n "$http_body" ]; then
        overall=$(echo "$http_body" | jq -r '.status // "unknown"')
        db_status=$(echo "$http_body" | jq -r '.database.status // "unknown"')
        engine_status=$(echo "$http_body" | jq -r '.measure_engine.status // "unknown"')
        cdr_status=$(echo "$http_body" | jq -r '.cdr.status // "unknown"')

        if [ "$overall" = "healthy" ]; then
            ok "All services healthy (db=$db_status, engine=$engine_status, cdr=$cdr_status)"
            break
        else
            # Report which service is lagging
            lag_msg="Waiting ($health_elapsed/${HEALTH_TIMEOUT}s) — status=$overall"
            [ "$db_status" != "connected" ]     && lag_msg="$lag_msg  [database=$db_status]"
            [ "$engine_status" != "connected" ] && lag_msg="$lag_msg  [measure_engine=$engine_status]"
            [ "$cdr_status" != "connected" ]    && lag_msg="$lag_msg  [cdr=$cdr_status]"
            echo "    $lag_msg"
        fi
    else
        echo "    Waiting ($health_elapsed/${HEALTH_TIMEOUT}s) — API not yet responding..."
    fi

    if [ "$health_elapsed" -ge "$HEALTH_TIMEOUT" ]; then
        fail "Timed out waiting for Lenny to become healthy after ${HEALTH_TIMEOUT}s"
        if [ -n "$http_body" ]; then
            echo "    Last health response:"
            echo "$http_body" | jq . || echo "$http_body"
        fi
        exit 1
    fi

    sleep "$HEALTH_POLL"
    health_elapsed=$((health_elapsed + HEALTH_POLL))
done

HEALTH_ELAPSED=$(elapsed_since "$HEALTH_START")
ok "Health check passed in $(fmt_duration "$HEALTH_ELAPSED")"

# ---------------------------------------------------------------------------
# Step 3: Assert all 12 connectathon measures are loaded
# ---------------------------------------------------------------------------

header "Step 3: Measure Inventory Check"

# Parse manifest — extract all 12 measure IDs
MANIFEST_IDS=$(jq -r '.measures[].id' "$MANIFEST")
MANIFEST_COUNT=$(echo "$MANIFEST_IDS" | wc -l | tr -d ' ')

step "Fetching loaded measures from $API_BASE/measures..."

measures_body=$(curl -sf --max-time 15 "$API_BASE/measures")
if [ -z "$measures_body" ]; then
    fail "Empty response from $API_BASE/measures"
    exit 1
fi

loaded_total=$(echo "$measures_body" | jq '.total // 0')
step "API reports $loaded_total measure(s) loaded. Manifest expects $MANIFEST_COUNT."

# Check each expected measure ID is present
missing_ids=()
while IFS= read -r expected_id; do
    # Match against the 'id' field in the measures array
    found=$(echo "$measures_body" | jq -r --arg id "$expected_id" \
        '.measures[] | select(.id == $id) | .id' 2>/dev/null || true)
    if [ -z "$found" ]; then
        missing_ids+=("$expected_id")
        fail "Missing: $expected_id"
    else
        ok "Found:   $expected_id"
    fi
done <<< "$MANIFEST_IDS"

if [ "${#missing_ids[@]}" -gt 0 ]; then
    echo ""
    fail "${#missing_ids[@]} measure(s) are missing from the API."
    echo "    Missing IDs: ${missing_ids[*]}"
    echo "    The bundles may not have loaded at startup. Check docker logs for bundle_loader errors."
    exit 1
fi

ok "All $MANIFEST_COUNT connectathon measures confirmed loaded."

# ---------------------------------------------------------------------------
# Step 4: Trigger evaluation for the first measure with expected_test_cases > 0
# ---------------------------------------------------------------------------

header "Step 4: Measure Evaluation Smoke Test"

# Find the first manifest entry with expected_test_cases > 0
EVAL_MEASURE_ID=$(jq -r '[.measures[] | select(.expected_test_cases > 0)] | first | .id' "$MANIFEST")
EVAL_PERIOD_START=$(jq -r --arg id "$EVAL_MEASURE_ID" \
    '.measures[] | select(.id == $id) | .period.start' "$MANIFEST")
EVAL_PERIOD_END=$(jq -r --arg id "$EVAL_MEASURE_ID" \
    '.measures[] | select(.id == $id) | .period.end' "$MANIFEST")
EVAL_EXPECTED_CASES=$(jq -r --arg id "$EVAL_MEASURE_ID" \
    '.measures[] | select(.id == $id) | .expected_test_cases' "$MANIFEST")

step "Selected measure: $EVAL_MEASURE_ID ($EVAL_EXPECTED_CASES expected test cases)"
step "Period: $EVAL_PERIOD_START to $EVAL_PERIOD_END"

# Create the job
step "Creating evaluation job..."
job_payload=$(printf '{"measure_id":"%s","period_start":"%s","period_end":"%s"}' \
    "$EVAL_MEASURE_ID" "$EVAL_PERIOD_START" "$EVAL_PERIOD_END")

job_response=$(curl -sf --max-time 15 \
    -X POST "$API_BASE/jobs" \
    -H "Content-Type: application/json" \
    -d "$job_payload")

if [ -z "$job_response" ]; then
    fail "No response from POST $API_BASE/jobs"
    exit 1
fi

JOB_ID=$(echo "$job_response" | jq -r '.id')
if [ -z "$JOB_ID" ] || [ "$JOB_ID" = "null" ]; then
    fail "Failed to create job. Response:"
    echo "$job_response" | jq . || echo "$job_response"
    exit 1
fi

ok "Job created: id=$JOB_ID"

# Poll until job completes or fails
step "Polling job status (timeout: ${JOB_TIMEOUT}s)..."
JOB_START=$(date +%s)
job_elapsed=0
JOB_FINAL_STATUS=""

while true; do
    job_body=$(curl -sf --max-time 10 "$API_BASE/jobs/$JOB_ID" 2>/dev/null || true)

    if [ -n "$job_body" ]; then
        job_status=$(echo "$job_body" | jq -r '.status // "unknown"')
        processed=$(echo "$job_body" | jq '.processed_patients // 0')
        total=$(echo "$job_body" | jq '.total_patients // 0')

        case "$job_status" in
            completed|failed|cancelled)
                JOB_FINAL_STATUS="$job_status"
                break
                ;;
            running|queued)
                echo "    Job $JOB_ID status=$job_status processed=$processed/$total (${job_elapsed}s)..."
                ;;
            *)
                echo "    Job $JOB_ID unknown status: $job_status (${job_elapsed}s)..."
                ;;
        esac
    else
        echo "    Job $JOB_ID: no response yet (${job_elapsed}s)..."
    fi

    if [ "$job_elapsed" -ge "$JOB_TIMEOUT" ]; then
        fail "Timed out waiting for job $JOB_ID to complete after ${JOB_TIMEOUT}s"
        exit 1
    fi

    sleep "$JOB_POLL"
    job_elapsed=$((job_elapsed + JOB_POLL))
done

JOB_ELAPSED=$(elapsed_since "$JOB_START")

if [ "$JOB_FINAL_STATUS" != "completed" ]; then
    fail "Job $JOB_ID finished with status: $JOB_FINAL_STATUS"
    echo "$job_body" | jq '{status,error_message,total_patients,processed_patients,failed_patients}' \
        || echo "$job_body"
    exit 1
fi

ok "Job $JOB_ID completed in $(fmt_duration "$JOB_ELAPSED")"

# Fetch results and print populations
step "Fetching results for job $JOB_ID..."
results_body=$(curl -sf --max-time 10 "$API_BASE/results?job_id=$JOB_ID" 2>/dev/null || true)

if [ -n "$results_body" ]; then
    total_patients=$(echo "$results_body" | jq '.total_patients // 0')
    ip=$(echo "$results_body" | jq '.populations.initial_population // 0')
    denom=$(echo "$results_body" | jq '.populations.denominator // 0')
    numer=$(echo "$results_body" | jq '.populations.numerator // 0')
    denom_excl=$(echo "$results_body" | jq '.populations.denominator_exclusion // 0')
    perf_rate=$(echo "$results_body" | jq '.performance_rate // "n/a"')

    echo ""
    echo "    Population results for $EVAL_MEASURE_ID:"
    printf "      %-30s %s\n" "Total patients:"          "$total_patients"
    printf "      %-30s %s\n" "Initial population:"      "$ip"
    printf "      %-30s %s\n" "Denominator:"             "$denom"
    printf "      %-30s %s\n" "Numerator:"               "$numer"
    printf "      %-30s %s\n" "Denominator exclusions:"  "$denom_excl"
    printf "      %-30s %s\n" "Performance rate:"        "$perf_rate%"
else
    warn "Could not fetch results for job $JOB_ID"
fi

# ---------------------------------------------------------------------------
# Step 5: Build the 12-row status table
# ---------------------------------------------------------------------------

header "Step 5: Full Measure Status Table"

# Re-fetch the measures list (already have $measures_body, but re-fetch to be safe)
measures_body=$(curl -sf --max-time 15 "$API_BASE/measures" 2>/dev/null || echo '{"measures":[],"total":0}')

# Fetch all jobs so we can map measure_id -> last completed job
all_jobs=$(curl -sf --max-time 15 "$API_BASE/jobs" 2>/dev/null || echo '[]')

# Print table header
echo ""
printf "%-30s | %-6s | %-9s | %-17s | %s\n" \
    "measure_id" "loaded" "evaluated" "populations_match" "notes"
printf "%s\n" "$(printf '%.0s-' {1..100})"

OVERALL_PASS=true

while IFS= read -r manifest_line; do
    measure_id=$(echo "$manifest_line" | jq -r '.id')
    expected_cases=$(echo "$manifest_line" | jq '.expected_test_cases // 0')
    known_issues=$(echo "$manifest_line" | jq -r '(.known_issues // []) | join("; ")')

    # --- loaded? ---
    found_in_api=$(echo "$measures_body" | jq -r --arg id "$measure_id" \
        '.measures[] | select(.id == $id) | .id' 2>/dev/null || true)
    if [ -n "$found_in_api" ]; then
        loaded="yes"
    else
        loaded="NO"
        OVERALL_PASS=false
    fi

    # --- evaluated? (find most recent completed job for this measure_id) ---
    last_job=$(echo "$all_jobs" | jq --arg mid "$measure_id" \
        '[.[] | select(.measure_id == $mid and .status == "completed")] | first // null')
    last_job_id=$(echo "$last_job" | jq -r '.id // empty')

    if [ "$last_job_id" = "null" ] || [ -z "$last_job_id" ]; then
        evaluated="no"
        populations_match="n/a"
        row_notes="${known_issues:-no completed job}"
        [ "$expected_cases" -eq 0 ] && evaluated="n/a"
    else
        evaluated="yes"

        # --- populations_match? ---
        if [ "$expected_cases" -eq 0 ]; then
            populations_match="n/a"
            row_notes="${known_issues:-definition-only}"
        else
            # Try comparison endpoint
            cmp_body=$(curl -sf --max-time 10 "$API_BASE/jobs/$last_job_id/comparison" 2>/dev/null || true)
            has_expected=$(echo "$cmp_body" | jq -r '.has_expected // "false"')

            if [ "$has_expected" = "true" ]; then
                matched=$(echo "$cmp_body" | jq '.matched // 0')
                total_cmp=$(echo "$cmp_body" | jq '.total // 0')
                if [ "$matched" -eq "$total_cmp" ] && [ "$total_cmp" -gt 0 ]; then
                    populations_match="yes ($matched/$total_cmp)"
                else
                    populations_match="NO ($matched/$total_cmp)"
                    OVERALL_PASS=false
                fi
                row_notes="${known_issues:-}"
            else
                # No expected results loaded — just confirm job ran
                result_body=$(curl -sf --max-time 10 "$API_BASE/results?job_id=$last_job_id" 2>/dev/null || true)
                total_pts=$(echo "$result_body" | jq '.total_patients // 0')
                populations_match="unverified"
                row_notes="no expected results stored; $total_pts patients processed${known_issues:+; $known_issues}"
            fi
        fi
    fi

    # --- print row ---
    printf "%-30s | %-6s | %-9s | %-17s | %s\n" \
        "$measure_id" "$loaded" "$evaluated" "$populations_match" "$row_notes"

done < <(jq -c '.measures[]' "$MANIFEST")

echo ""

# ---------------------------------------------------------------------------
# Step 6: Final summary and timing
# ---------------------------------------------------------------------------

header "Rehearsal Summary"

TOTAL_ELAPSED=$(elapsed_since "$SCRIPT_START")

echo "  Timing breakdown:"
printf "    %-30s %s\n" "Docker startup:"      "$(fmt_duration "$DOCKER_ELAPSED")"
printf "    %-30s %s\n" "Health poll wait:"    "$(fmt_duration "$HEALTH_ELAPSED")"
printf "    %-30s %s\n" "Evaluation job:"      "$(fmt_duration "$JOB_ELAPSED")"
printf "    %-30s %s\n" "Total elapsed:"       "$(fmt_duration "$TOTAL_ELAPSED")"
echo ""
echo "  Log appended to: $LOG_FILE"
echo ""

if $OVERALL_PASS; then
    echo "  RESULT: PASS — all measures loaded, evaluation smoke test passed."
    echo ""
    exit 0
else
    echo "  RESULT: FAIL — one or more measures failed. See table above."
    echo ""
    exit 1
fi
