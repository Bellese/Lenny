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

> **Note (2026-05-06):** Production currently runs vanilla `hapiproject/hapi:v8.8.0-1`, not the pre-baked GHCR images. The steps below apply only if prod is switched to prebaked. For CI verification, this section remains accurate.

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
