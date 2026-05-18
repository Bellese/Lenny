# Runbook: Local vs Production Deploy

## At a glance

| Aspect | Local dev (default) | CI | Production |
|--------|--------------------|----|------------|
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.prebaked.yml` (`.env.example` default) | `docker-compose.yml:docker-compose.prebaked.yml` | `docker-compose.yml:docker-compose.prod.yml` |
| HAPI images | `ghcr.io/bellese/lenny-hapi-{cdr,measure}:latest` (prebaked) | Same prebaked images | `hapiproject/hapi:v8.8.0-1` (vanilla) |
| IGs pre-installed? | Yes — baked into the image H2 store | Yes | No — HAPI downloads them at first boot from the HL7 registry (~5–10 min cold start) |
| Seed bundles pre-loaded? | Yes — baked into the image | Yes | No — the `seed` service POSTs them on first boot |
| Named volumes | `leonard_cdrdata`, `leonard_measuredata`, `leonard_pgdata` | Ephemeral (no `--volumes` flag on CI teardown) | `leonard_cdrdata`, `leonard_measuredata`, `leonard_pgdata` |
| What survives `docker compose down`? | Named volumes — data persists | Nothing (CI uses `--volumes` on teardown) | Named volumes — data persists |
| Reverse proxy | None (ports exposed directly) | None | Caddy (`docker-compose.prod.yml`) |
| TLS | None | None | Let's Encrypt (Caddy) |
| Secrets | `.env` file | `.env` from CI secrets | `/run/leonard/env` via `fetch-prod-secrets.sh` |

## Local fast path vs vanilla local

`.env.example` sets:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prebaked.yml
HAPI_CDR_IMAGE=ghcr.io/bellese/lenny-hapi-cdr:latest
HAPI_MEASURE_IMAGE=ghcr.io/bellese/lenny-hapi-measure:latest
```

Copy it to `.env` and run `docker compose up -d`. The prebaked images already contain the connectathon bundles and IGs so cold-start completes in under 60 seconds.

To run with vanilla HAPI (same image as prod), remove `COMPOSE_FILE` from `.env` or delete the file entirely. HAPI will download IGs on first boot — expect 5–10 minutes before the first job runs. The seed service still runs and loads the connectathon bundles.

## First boot vs subsequent boot

**Production** uses named volumes (`leonard_cdrdata`, `leonard_measuredata`) that survive redeploys. The `seed` service runs on every `docker compose up` but the bundles are loaded via PUT (idempotent) — existing resources are updated in place, not duplicated. If a volume already contains data, the seed completes quickly.

**Local dev with prebaked images** bakes the bundles into the image H2 store. On first boot the volume is empty and the image's H2 store is used directly; subsequent boots reuse the same volume. The `seed` service does not run in the prebaked layout (`docker-compose.prebaked.yml` overrides `seed` command to no-op).

**What happens if you `docker volume rm leonard_cdrdata`?** The CDR is empty on next boot. The seed service (prod) or the prebaked image (local) will repopulate it — but in prod this requires the seed container to run through its full bootstrap again (~5 min).

## Differences that matter for debugging

- **Env vars for HAPI config** are identical between local and prod (`docker-compose.yml` defines them). The only differences are memory caps and the reverse proxy.
- **`synchronization.strategy=sync`** is set on both local and prod HAPI services (`docker-compose.yml`). Writes block until the Lucene index is refreshed — so search-after-write is consistent in both environments.
- **Postgres password** is a fixed `.env` value locally; in prod it's read from AWS SSM at each deploy via `scripts/fetch-prod-secrets.sh`.
- **`ALLOWED_ORIGINS`** defaults to `"*"` locally; prod sets it to `https://${CADDY_HOST}` via `docker-compose.prod.yml`.

## See also

- `docs/runbooks/factory-reset.md` — wipe Lenny back to a known-good blank slate
- `docs/runbooks/measure-engine-h2-recovery.md` — recover from measure engine H2 corruption
- `docs/architecture.md` — full service map and HAPI configuration reference
