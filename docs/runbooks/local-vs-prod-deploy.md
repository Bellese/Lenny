# Runbook: Local vs Production Deploy

## The key difference: local runs prebaked, production runs vanilla

**Local dev (and CI) uses "prebaked" HAPI images** — Docker images that already have the connectathon bundles and FHIR IGs baked into their embedded H2 database. Cold-start completes in under 60 seconds because there's nothing to download or seed.

**Production uses vanilla `hapiproject/hapi:v8.8.0-1`** — an off-the-shelf image with no pre-loaded data. On first boot, HAPI downloads the required IGs from the HL7 registry (~5–10 min) and the `seed` service POSTs all the connectathon bundles into the running server (~5–10 min more). After that the data lives in named Docker volumes and survives every subsequent redeploy.

### Why the difference?

Prebaked images are faster for development iteration — no waiting on seed or IG download every time you `docker compose down -v && docker compose up`. But they need to be rebuilt whenever the measure bundles change, which adds a CI step. Production doesn't have that problem because it seeds into a persistent volume on first boot and never rebuilds the image just for a bundle update.

Put another way: **prebaked trades image-build time for startup time; vanilla trades startup time for a simpler CI pipeline and a prod-trustworthy seed path.**

## Comparison table

| Aspect | Local dev (default) | CI | Production |
|--------|--------------------|----|------------|
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.prebaked.yml` (`.env.example` default) | `docker-compose.yml:docker-compose.prebaked.yml` | `docker-compose.yml:docker-compose.prod.yml` |
| HAPI images | `ghcr.io/bellese/lenny-hapi-{cdr,measure}:latest` **(prebaked)** | Same prebaked images | `hapiproject/hapi:v8.8.0-1` **(vanilla)** |
| IGs pre-installed? | Yes — baked into the image H2 store | Yes | No — HAPI downloads them at first boot from the HL7 registry (~5–10 min cold start) |
| Seed bundles pre-loaded? | Yes — baked into the image | Yes | No — the `seed` service POSTs them on first boot |
| Named volumes | `leonard_cdrdata`, `leonard_measuredata`, `leonard_pgdata` | Ephemeral (CI uses `--volumes` on teardown) | `leonard_cdrdata`, `leonard_measuredata`, `leonard_pgdata` |
| What survives `docker compose down`? | Named volumes — data persists | Nothing | Named volumes — data persists |
| Reverse proxy | None (ports exposed directly) | None | Caddy (`docker-compose.prod.yml`) |
| TLS | None | None | Let's Encrypt (Caddy) |
| Secrets | `.env` file | `.env` from CI secrets | `/run/leonard/env` via `fetch-prod-secrets.sh` |

## Running locally

### Fast path (prebaked — default)

`.env.example` sets:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prebaked.yml
HAPI_CDR_IMAGE=ghcr.io/bellese/lenny-hapi-cdr:latest
HAPI_MEASURE_IMAGE=ghcr.io/bellese/lenny-hapi-measure:latest
```

Copy it to `.env` and run `docker compose up -d`. Ready in under 60 seconds.

### Vanilla local (same image as prod)

Remove `COMPOSE_FILE` from `.env` (or delete the file). HAPI downloads IGs on first boot — expect 5–10 minutes before the first job runs. The seed service still runs and loads the connectathon bundles.

Use this mode when you need to verify prod-equivalent behavior (e.g., first-boot seed path, Caddy config, IG download) rather than day-to-day feature work.

## First boot vs subsequent boot

**Production** uses named volumes that survive redeploys. The `seed` service runs on every `docker compose up` but bundles load via PUT (idempotent) — existing resources are updated in place, not duplicated. If volumes already contain data, the seed completes quickly.

**Local dev with prebaked images** bakes the bundles into the image H2 store. The `seed` service is no-op'd in `docker-compose.prebaked.yml`. On first boot the volume is empty and the image's H2 store is used directly; subsequent boots reuse the same volume.

**What happens if you `docker volume rm leonard_cdrdata`?** The CDR is empty on next boot. The seed service (prod/vanilla) or prebaked image (local fast path) repopulates it — but in prod this requires the full seed bootstrap again (~5–10 min). To factory-reset intentionally, see `docs/runbooks/factory-reset.md`.

## Differences that matter for debugging

- **Env vars for HAPI config** are identical between local and prod (`docker-compose.yml` defines them). The only differences are memory caps and the reverse proxy.
- **`synchronization.strategy=sync`** is set on both local and prod HAPI services. Writes block until the Lucene index is refreshed — search-after-write is consistent in both environments.
- **Postgres password** is a fixed `.env` value locally; in prod it's read from AWS SSM at each deploy via `scripts/fetch-prod-secrets.sh`.
- **`ALLOWED_ORIGINS`** defaults to `"*"` locally; prod sets it to `https://${CADDY_HOST}` via `docker-compose.prod.yml`.

## See also

- `docs/runbooks/factory-reset.md` — wipe Lenny back to a known-good blank slate
- `docs/runbooks/measure-engine-h2-recovery.md` — recover from measure engine H2 corruption
- `docs/architecture.md` — full service map and HAPI configuration reference
