#!/usr/bin/env bash
# check-pinned-images.sh — Fail loud when a deliberately pinned HAPI image
# can't be pulled, instead of silently falling back to whatever is cached.
#
# Background (issue #407): `/opt/leonard/.env` pinned HAPI_CDR_IMAGE and
# HAPI_MEASURE_IMAGE at package names that were renamed away in PR #261.
# `docker compose pull --ignore-pull-failures || true` swallowed the
# resulting 403s on every deploy for months — prod kept running whatever
# image was already cached, and nothing in the logs made that obvious.
#
# This script resolves each HAPI service's image the same way `docker
# compose` itself resolves it (via `config --images`, which reads .env
# through the normal project-directory lookup) rather than re-parsing
# .env — a parsing bug here must never be able to take down a deploy.
# It then:
#   - prints the resolved ref for every service, every run, pinned or
#     not — so a future .env drift leaves a trace even when the pull
#     succeeds;
#   - for a service NOT pinned to the compose default, does nothing
#     further — the caller's own blanket `compose pull
#     --ignore-pull-failures` still covers it;
#   - for a service pinned away from the default, pulls that exact ref
#     and fails loudly if the pull fails. A pin is deliberate; a pin
#     that can't be pulled is a broken deploy, not a blip.
#
# Exit codes:
#   0  all resolved images either match the compose default, or were
#      pinned and pulled successfully
#   1  a pinned image failed to pull
#   2  compose could not resolve an image for a known HAPI service
#      (e.g. a typo'd service name, or an incompatible compose version) —
#      treated as a hard failure rather than silently skipped, since a
#      silent skip here would reintroduce the exact bug class this script
#      exists to catch
#
# Usage:
#   ./scripts/check-pinned-images.sh
#
# Optional env vars:
#   LEONARD_DIR   Root directory containing docker-compose.yml (default:
#                 parent of this script).

set -euo pipefail

# ── path resolution ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEONARD_DIR="${LEONARD_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_BASE="${LEONARD_DIR}/docker-compose.yml"
COMPOSE_PROD="${LEONARD_DIR}/docker-compose.prod.yml"
# --project-directory makes the .env lookup explicit rather than relying on
# it being inferred from the first -f path — no behavior change today (same
# directory either way), but it stops this depending on an assumption.
COMPOSE=( docker compose --project-directory "$LEONARD_DIR" -f "$COMPOSE_BASE" -f "$COMPOSE_PROD" )

# The compose-file default for both HAPI services (docker-compose.yml:52,85).
# Duplicated here deliberately — see scripts/tests/test_pinned_images.sh for
# the drift canary that keeps this in sync with the compose file.
readonly VANILLA_HAPI_IMAGE="hapiproject/hapi:v8.8.0-1"

HAPI_SERVICES=( hapi-fhir-cdr hapi-fhir-measure )

for svc in "${HAPI_SERVICES[@]}"; do
    # `config --images <service>` returns the resolved image on stdout;
    # unrelated warnings (e.g. an unset CADDY_HOST) land on stderr, hence
    # the redirect — never let a warning be mistaken for the resolved ref.
    # An unknown service name prints nothing on stdout and does NOT error,
    # which is exactly why the empty check below exists.
    resolved="$("${COMPOSE[@]}" config --images "$svc" 2>/dev/null | head -n1)"

    if [[ -z "$resolved" ]]; then
        printf '[!] check-pinned-images: could not resolve an image for service "%s"\n' "$svc" >&2
        exit 2
    fi

    printf '[+] check-pinned-images: %s -> %s\n' "$svc" "$resolved"

    if [[ "$resolved" == "$VANILLA_HAPI_IMAGE" ]]; then
        continue
    fi

    printf '[+] check-pinned-images: %s is pinned away from the default — verifying it pulls...\n' "$svc"
    if ! docker pull "$resolved"; then
        printf '[!] FATAL: pinned image for %s (%s) failed to pull\n' "$svc" "$resolved" >&2
        exit 1
    fi
done

printf '[+] check-pinned-images: all pinned images pulled successfully\n'
