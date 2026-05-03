# GHCR Pull Auth on Prod

## Current state

`ghcr.io/bellese/mct2-hapi-cdr` and `ghcr.io/bellese/mct2-hapi-measure` are
**public**. No authentication is required to pull them. `docker compose pull`
on EC2 works without login, for both automated deploys and manual runs.

Push still requires auth. The bake workflow (`.github/workflows/bake-hapi-image.yml`)
authenticates with the workflow's ephemeral `GITHUB_TOKEN` (`packages: write`
permission) — no change there.

## Manual deploy

```bash
ssh leonard@98.89.219.217 -i ~/.ssh/leonard.pem
sudo ./scripts/deploy-prod.sh
```

No GHCR login step is needed. `docker compose pull` will pull from
`ghcr.io/bellese/mct2-hapi-{cdr,measure}` without credentials.

## Verification

After deploy, confirm images are present:

```bash
sudo docker images | grep ghcr.io/bellese
# Should list mct2-hapi-cdr:latest and mct2-hapi-measure:latest
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
