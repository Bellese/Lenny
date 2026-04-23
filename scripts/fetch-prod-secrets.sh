#!/usr/bin/env bash
# fetch-prod-secrets.sh — Fetch secrets from AWS SSM Parameter Store and
# write them to a secure tmpfs file for docker compose to consume at startup.
#
# Must run as root. Writes /run/leonard/env (mode 0600, owned root).
# Uses the EC2 instance-profile credentials — no static keys, no AWS_PROFILE.
#
# Exit codes:
#   0  success — /run/leonard/env written
#   1  SSM fetch failed or required parameter missing
#   2  a fetched value failed validation
#
# Usage:
#   sudo ./scripts/fetch-prod-secrets.sh
#
# Optional env vars:
#   LEONARD_SSM_VERSION   If set, fetch POSTGRES_PASSWORD at that exact SSM
#                         version (rollback path) instead of latest.
#   LEONARD_ENV_DIR       Override output directory (default: /run/leonard).
#                         Set to /tmp/leonard for local smoke-testing.
#
# ──────────────────────────────────────────────────────────────────────────────
# SMOKE TEST (local, no real AWS):
#
#   # 1. Create a fake `aws` shim on PATH (sudo strips exported bash functions,
#   #    so a PATH shim is the only reliable approach under sudo).
#   mkdir -p /tmp/leonard-bin
#   cat > /tmp/leonard-bin/aws <<'SHIM'
#   #!/usr/bin/env bash
#   cat <<'JSON'
#   {"Parameters":[{"Name":"/leonard/prod/POSTGRES_PASSWORD","Value":"ChangeMe1234567890abcdef12345"}],"NextToken":null}
#   JSON
#   SHIM
#   chmod +x /tmp/leonard-bin/aws
#
#   # 2. Run against a writable temp dir, overriding PATH to pick up the shim.
#   sudo env PATH="/tmp/leonard-bin:$PATH" LEONARD_ENV_DIR=/tmp/leonard-test \
#     bash ./scripts/fetch-prod-secrets.sh
#
#   sudo cat /tmp/leonard-test/env   # should show POSTGRES_PASSWORD=ChangeMe...
#   sudo rm -rf /tmp/leonard-test /tmp/leonard-bin
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── dependencies ───────────────────────────────────────────────────────────────
command -v jq >/dev/null 2>&1 || { printf '[!] jq is required but not installed.\n' >&2; exit 1; }

# ── constants ──────────────────────────────────────────────────────────────────
readonly SSM_REGION="us-east-1"
readonly SSM_PATH_PREFIX="/leonard/prod/"
readonly REQUIRED_PARAMS=("POSTGRES_PASSWORD")
readonly ENV_DIR="${LEONARD_ENV_DIR:-/run/leonard}"
readonly ENV_FILE="${ENV_DIR}/env"
# Value must be printable, no whitespace/quotes/semicolons, length 16–128.
readonly VALIDATION_REGEX='^[A-Za-z0-9_.-]{16,128}$'

# ── functions ──────────────────────────────────────────────────────────────────

die() {
    # Print to stderr and exit with given code (default 1).
    local code="${1:-1}"
    shift
    printf '[!] %s\n' "$*" >&2
    exit "$code"
}

validate_value() {
    # $1 = parameter short name (for error messages only)
    # $2 = value
    local name="$1"
    local value="$2"
    if ! printf '%s' "$value" | grep -qE "$VALIDATION_REGEX"; then
        die 2 "Value for '${name}' failed validation — check length (16–128) and allowed characters ([A-Za-z0-9_.-]). Value not logged."
    fi
}

write_env_file() {
    # $1 = env content string (KEY=VALUE lines)
    local content="$1"

    # Ensure output directory exists.
    if ! mkdir -p "$ENV_DIR" 2>/dev/null; then
        die 1 "Cannot create directory '${ENV_DIR}'. Run as root."
    fi

    # Write atomically: use install(1) to set mode 0600 and owner root in one step.
    # This avoids a window where the file is readable before chmod.
    if ! printf '%s\n' "$content" | install -o root -g root -m 0600 /dev/stdin "$ENV_FILE"; then
        die 1 "Cannot write to '${ENV_FILE}'. Check permissions."
    fi
}

# ── main ───────────────────────────────────────────────────────────────────────

# Require root so file ownership can be set to root.
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die 1 "This script must be run as root (try: sudo $0)"
fi

declare -A PARAMS

# Temp file for stderr from AWS CLI calls; cleaned up on exit.
_aws_err=$(mktemp)
trap 'rm -f "$_aws_err"' EXIT

if [[ -n "${LEONARD_SSM_VERSION:-}" ]]; then
    # ── rollback path: fetch POSTGRES_PASSWORD at a specific version ───────────
    if ! response=$(aws ssm get-parameter \
        --region "$SSM_REGION" \
        --name "${SSM_PATH_PREFIX}POSTGRES_PASSWORD:${LEONARD_SSM_VERSION}" \
        --with-decryption \
        --output json 2>"$_aws_err"); then
        die 1 "SSM get-parameter failed for POSTGRES_PASSWORD at version ${LEONARD_SSM_VERSION}: $(cat "$_aws_err")"
    fi

    value=$(printf '%s' "$response" | jq -r '.Parameter.Value // empty')
    if [[ -z "$value" ]]; then
        die 1 "SSM response did not contain a value for POSTGRES_PASSWORD."
    fi
    PARAMS["POSTGRES_PASSWORD"]="$value"

else
    # ── normal path: fetch all params under /leonard/prod/ ────────────────────
    next_token=""
    fetched=0

    while true; do
        aws_args=(
            ssm get-parameters-by-path
            --region "$SSM_REGION"
            --path "$SSM_PATH_PREFIX"
            --with-decryption
            --recursive
            --max-results 10
            --output json
        )
        [[ -n "$next_token" ]] && aws_args+=(--next-token "$next_token")
        if ! response=$(aws "${aws_args[@]}" 2>"$_aws_err"); then
            die 1 "SSM get-parameters-by-path failed: $(cat "$_aws_err")"
        fi

        # Parse each parameter into the associative array.
        while IFS=$'\t' read -r full_name value; do
            short_name="${full_name#"$SSM_PATH_PREFIX"}"
            PARAMS["$short_name"]="$value"
            (( fetched++ )) || true
        done < <(printf '%s' "$response" | jq -r '.Parameters[] | [.Name, .Value] | @tsv')

        next_token=$(printf '%s' "$response" | jq -r '.NextToken // empty')
        if [[ -z "$next_token" ]]; then
            break
        fi
    done

    if [[ "$fetched" -eq 0 ]]; then
        die 1 "No parameters returned from SSM path '${SSM_PATH_PREFIX}'. Check IAM permissions and path."
    fi
fi

# ── validate all fetched values ───────────────────────────────────────────────
for key in "${!PARAMS[@]}"; do
    validate_value "$key" "${PARAMS[$key]}"
done

# ── check required parameters are present ────────────────────────────────────
for req in "${REQUIRED_PARAMS[@]}"; do
    if [[ -z "${PARAMS[$req]:-}" ]]; then
        die 1 "Required parameter '${req}' was not returned by SSM."
    fi
done

# ── build env content (in memory, never echoed) ───────────────────────────────
env_content=""
for key in "${!PARAMS[@]}"; do
    env_content+="${key}=${PARAMS[$key]}"$'\n'
done
# Trim trailing newline for clean file.
env_content="${env_content%$'\n'}"

# ── write env file ────────────────────────────────────────────────────────────
write_env_file "$env_content"

printf '[+] Secrets fetched and written to %s\n' "$ENV_FILE"
