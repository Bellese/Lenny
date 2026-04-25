# GHCR Pull Auth on Prod

## Why this exists

The Phase 1 per-measure HAPI reset architecture (PR #167, design doc
`2026-04-24-main-design-ephemeral-hapi-per-measure.md`) requires the prod EC2
instance to pull pre-baked HAPI images from `ghcr.io/bellese/mct2-hapi-{cdr,measure}`.
GHCR packages owned by an org default to **private**, so an unauthenticated
`docker compose pull` returns 401 and the deploy fails:

```
hapi-fhir-measure Error Head "https://ghcr.io/v2/bellese/mct2-hapi-measure/manifests/latest": unauthorized
```

## How auth works

No long-lived credential. The deploy workflow's own ephemeral `GITHUB_TOKEN`
(scope: `packages: read`) is relayed to the EC2 box for one deploy and then
deleted.

```
.github/workflows/deploy.yml
  ├─ Stage step:    aws ssm put-parameter --name /leonard/prod/GHCR_TOKEN
  │                                       --value $GITHUB_TOKEN
  │                                       --type SecureString --overwrite
  ├─ Deploy step:   aws ssm send-command  (runs scripts/deploy-prod.sh on EC2)
  │                   └─ deploy-prod.sh:  docker login ghcr.io < /run/leonard/env
  └─ Cleanup step:  aws ssm delete-parameter (always-run, even on deploy fail)
```

The token sits in SSM only for the duration of the deploy (~3-5 min). It would
expire on its own at workflow end (~1 h) regardless.

## IAM requirements

The OIDC role `arn:aws:iam::439475769170:role/leonard-github-deploy` (assumed
by the deploy workflow) needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "ssm:PutParameter",
    "ssm:DeleteParameter"
  ],
  "Resource": "arn:aws:ssm:us-east-1:439475769170:parameter/leonard/prod/GHCR_TOKEN"
},
{
  "Effect": "Allow",
  "Action": ["kms:Encrypt"],
  "Resource": "arn:aws:kms:us-east-1:439475769170:alias/aws/ssm"
}
```

The EC2 instance role needs `ssm:GetParameter*` on `/leonard/prod/*` (already
in place for `POSTGRES_PASSWORD`). No additional IAM change.

## Failure modes

**`Stage GHCR pull token in SSM` step fails with AccessDenied** — IAM doesn't
yet allow `ssm:PutParameter` on the token path. Apply the policy snippet
above and re-run the workflow.

**Deploy step fails with `docker login ghcr.io failed`** — the workflow may
not have `packages: read` permission. Check `.github/workflows/deploy.yml`
permissions block. The token is also `packages: read`-scoped — if a future
change reduces the workflow's permission, GHCR returns 401.

**Manual deploy via `sudo ./scripts/deploy-prod.sh` on the EC2 box** —
deploy-prod.sh prints a warning and skips `docker login`. If
`docker-compose.prebaked.yml` is in the compose stack, the next pull will
401. To run a manual deploy: either push to main (auto-redeploy via workflow
with auth), or temporarily drop `-f docker-compose.prebaked.yml` from the
COMPOSE array in `deploy-prod.sh`.

## Verification

After deploy:

```
ssh leonard@98.89.219.217 -i ~/.ssh/leonard.pem  # via session manager
sudo docker images | grep ghcr.io/bellese
# Should list mct2-hapi-cdr:latest and mct2-hapi-measure:latest
```

In CloudWatch logs (group `/leonard/deploy`), the deploy run should contain:

```
[+] Logging in to GHCR for pre-baked HAPI image pulls...
[+] GHCR login OK
```

## Why not a long-lived PAT

Considered. Rejected because:
- A PAT is a long-lived credential needing rotation, monitoring, and SSM hygiene.
- The workflow `GITHUB_TOKEN` is auto-rotated, scoped per-run, and free.
- Bake workflow already pushes images using the same `GITHUB_TOKEN` pattern;
  symmetry between push and pull auth is operationally clean.

## Related

- PR that introduced this: TBD (chore/ghcr-pull-auth)
- PR that introduced the prebaked dependency: #167
- Bake workflow: `.github/workflows/bake-hapi-image.yml`
