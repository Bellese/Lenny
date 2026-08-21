# GHCR Pull Auth on Prod

## Current state

`ghcr.io/bellese/lenny-hapi-cdr` and `ghcr.io/bellese/lenny-hapi-measure` are
**public**. No authentication is required to pull them. `docker compose pull`
on EC2 works without login, for both automated deploys and manual runs.

Push still requires auth. The bake workflow (`.github/workflows/bake-hapi-image.yml`)
authenticates with the workflow's ephemeral `GITHUB_TOKEN` (`packages: write`
permission) — no change there.

## Manual deploy

```bash
ssh ec2-user@api.lenny.bellese.dev -i ~/.ssh/leonard-ec2.pem
cd /opt/leonard && sudo ./scripts/deploy-prod.sh
```

No GHCR login step is needed. `docker compose pull` will pull from
`ghcr.io/bellese/lenny-hapi-{cdr,measure}` without credentials.

## Verification

> **Note (superseded 2026-08-21):** the earlier note here said production runs vanilla `hapiproject/hapi:v8.8.0-1` rather than GHCR images. That was wrong. `/opt/leonard/.env` pins `HAPI_CDR_IMAGE`/`HAPI_MEASURE_IMAGE` to `ghcr.io/bellese/mct2-hapi-*:latest` — the **pre-rename** package names, which return 403. Every prod deploy logs:
>
> ```
> hapi-fhir-cdr Error Head "https://ghcr.io/v2/bellese/mct2-hapi-cdr/manifests/latest": denied: denied
> ```
>
> `--ignore-pull-failures` swallows it, so prod keeps running a stale cached image and the weekly bake never lands there. Note this is *not* a pull-auth problem — `lenny-hapi-*` is public and pulls fine unauthenticated; the pinned names are simply obsolete. Tracked separately; see `docs/deploy.md` § Production and GHCR.

After deploy (if using prebaked images), confirm images are present:

```bash
sudo docker images | grep ghcr.io/bellese
# Should list lenny-hapi-cdr:latest and lenny-hapi-measure:latest
```

## History

Previously, the deploy workflow used an ephemeral `GITHUB_TOKEN` (scope
`packages: read`) staged into SSM SecureString `/leonard/prod/GHCR_TOKEN` for
the duration of each deploy. That mechanism was removed in the PR that resolved
issue #200 when the packages were made public. Images contain only public
artifacts (HAPI binary, connectathon bundles, IGs) — no secrets.

## Related

- Issue #200 — the decision to make packages public
- Bake workflow: `.github/workflows/bake-hapi-image.yml`
- `docs/deploy.md` — GHCR's role in the pipeline, and the stale prod pin
