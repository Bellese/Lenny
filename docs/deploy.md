# Deploying Lenny to Production

Orientation for anyone who needs to understand how code gets from a merged PR to
`https://lenny.bellese.dev` — what runs, what it depends on, and what lives outside this
repository. For step-by-step procedures (rotating a password, recovering a corrupt H2
store, factory-resetting the stack), see `docs/runbooks/`.

Three questions, in order:

1. [What happens when a PR merges](#1-what-happens-when-a-pr-merges)
2. [What GHCR is for](#2-what-ghcr-is-for)
3. [What lives outside this repo](#3-what-lives-outside-this-repo)

---

## 1. What happens when a PR merges

Every merge to `main` deploys to production automatically. There is no staging
environment and no manual approval step.

| # | Stage | What happens | Defined in |
|---|-------|--------------|------------|
| 1 | **Gate** | Six checks must pass before the PR can merge | `.github/workflows/pr-checks.yml` |
| 2 | **Trigger** | Push to `main` starts the Deploy workflow | `.github/workflows/deploy.yml` |
| 3 | **Auth** | GitHub assumes an AWS role via OIDC — no stored AWS keys | `iam/github-deploy-trust-policy.json` |
| 4 | **Execute** | GitHub sends an SSM Run Command to the EC2 instance | `.github/workflows/deploy.yml` |
| 5 | **On the box** | Instance resets to `origin/main`, runs the deploy script | `ssm/leonard-deploy-document.json` |
| 6 | **Deploy** | 9 steps: secrets → db → build → up | `scripts/deploy-prod.sh` |
| 7 | **Verify** | `/health` polled from the box *and* from GitHub | both of the above |

### The parts a table can't carry

**Stage 1 — the gate.** `pr-checks.yml` defines six jobs: `Lint`, `Unit Tests + Coverage`,
`Integration Tests`, `Frontend Build`, `Config Validation`, and `Script Security Lint`.
That these are *required*, that a PR must be up to date with `main` first (`strict: true`),
and that admins can't bypass them (`enforce_admins: true`) are **GitHub branch-protection
settings, not checked-in configuration** — you won't find them in any file here. Verified
against the branch-protection API on 2026-08-21. No review approval is required; the
status checks are the whole gate.

**Stage 3 — why there are no AWS keys.** GitHub Actions authenticates to AWS by exchanging
a short-lived OIDC token for temporary credentials, scoped to a single deploy role. Nothing
long-lived is stored in GitHub. The trust policy limits which repository and branch can
assume the role; the permissions policy limits it to sending this one SSM command.

**Stage 4 — why SSM instead of SSH.** GitHub never opens a connection to the instance. It
asks AWS Systems Manager to run a pre-registered document, and the SSM agent already
running on the box picks up the work. The instance needs no inbound port open, and no
private key exists in CI. The workflow then polls `aws ssm get-command-invocation` up to
64 × 15s (~16 min), dumping the command's stdout and stderr into the Actions log if it
fails.

**Stage 6 — what `deploy-prod.sh` actually does.** Nine steps, in order: preflight checks →
reclaim disk (prune dangling images and build cache; added after a deploy failed on a full
root volume, issue #368) → fetch secrets from SSM → write Docker secret files → start the
database → wait for it to pass its healthcheck → reconcile the DB role password → `docker
compose pull` → `docker compose up -d --build` → poll `/health`. Read the header comment in
that script; it is the authoritative sequence.

**Stage 7 — verified twice.** The script polls `/health` from the instance, then the
workflow polls `https://api.lenny.bellese.dev/health` from GitHub (24 × 5s). A red deploy
means one of those two checks failed.

### One caveat worth knowing

The SSM document runs `git fetch origin && git reset --hard origin/main` on the instance.
The triggering commit SHA is never passed to the box — so a deploy installs **whatever is
on `origin/main` at the moment the command executes**, not necessarily the commit that
triggered it. Because the workflow uses `cancel-in-progress: false`, two merges landing
close together can result in the first run deploying the second commit.

Production always converges to `main`, so this is not a correctness problem in practice.
But "the deploy run for commit X" is not a guarantee that commit X is what got deployed,
which qualifies the CI-parity claim elsewhere in our docs.

### Watching, redeploying, and break-glass

- **Watch a deploy:** the run log under the Actions tab. From the CLI:
  ```bash
  gh run list --repo Bellese/Lenny --workflow deploy.yml --branch main --limit 1 \
    --json status,conclusion,headSha,url
  ```
- **Redeploy without a new commit:** Actions → Deploy → Run workflow → `main`.
- **See what happened on the instance:** CloudWatch log group `/leonard/deploy` holds the
  SSM command's stdout and stderr. This is the first place to look when a deploy goes red
  after the SSM step starts — it captures things the Actions log does not.
- **The rule:** never run `docker compose` directly in production. Always
  `scripts/deploy-prod.sh`, which handles the secret-fetch and password-reconcile steps
  that a bare `compose up` skips. Break-glass SSH access is documented in
  `docs/workflow.md`.

---

## 2. What GHCR is for

**GHCR is the GitHub Container Registry.** We publish two images there:
`ghcr.io/bellese/lenny-hapi-cdr` and `ghcr.io/bellese/lenny-hapi-measure`.

**What's in them.** HAPI FHIR with the connectathon bundles, the QI-Core / US-Core / CQL
implementation guides, and expanded ValueSets already loaded into the image's embedded H2
database. A vanilla HAPI container has to download IGs from the HL7 registry and be seeded
over HTTP on first boot, which takes 10–20 minutes. Starting from a baked image takes under
a minute. That difference is the entire reason these images exist — they make local dev and
CI fast.

**How they're built.** `.github/workflows/bake-hapi-image.yml`, weekly on Monday at 05:00
UTC, plus on any change under `seed/` and on manual dispatch. The build is unusual:
`hapiproject/hapi` is a distroless image with no shell, so `RUN` steps in a Dockerfile
aren't possible. Instead the workflow starts a container, seeds it over HTTP from the
runner, then `docker commit`s the result.

**How a bad image is prevented from shipping.** The bake job pushes only a content-hash tag
(`:${seed-hash}`). A second job, `verify-bake`, pulls that exact tag, stands up the test
stack against it, and runs `backend/tests/integration/test_connectathon_measures.py` with `STRICT_STU6=1`. Only if
those assertions pass is `:latest` retagged and pushed. So `:latest` is never an unverified
image.

**Credentials: there are none for pulling.** Both packages are public — an unauthenticated
manifest request returns HTTP 200 (verified 2026-08-21). Pushing uses the workflow's own
ephemeral `GITHUB_TOKEN` with `packages: write`, which exists only for the duration of the
job. There is no long-lived GHCR personal access token anywhere in this system. An earlier
design did stage a token into SSM for each deploy; that was removed when the packages were
made public (issue #200, `docs/runbooks/ghcr-pull-auth.md`).

**Who actually consumes them.** Local dev and CI, via `HAPI_CDR_IMAGE` / `HAPI_MEASURE_IMAGE`
in `.env` (see `.env.example`) plus the `docker-compose.prebaked.yml` overlay, which strips
the named volume mounts so the baked data inside the image is visible.

### Production and GHCR: a known problem

Production's `/opt/leonard/.env` pins `HAPI_CDR_IMAGE` and `HAPI_MEASURE_IMAGE` to
`ghcr.io/bellese/mct2-hapi-cdr:latest` and `ghcr.io/bellese/mct2-hapi-measure:latest` —
the **old package names**, from before the project was renamed from MCT2 to Lenny. Those
packages return HTTP 403; the current ones are `lenny-hapi-*`.

The result, visible in the `/leonard/deploy` CloudWatch logs on every deploy:

```
hapi-fhir-cdr Error Head "https://ghcr.io/v2/bellese/mct2-hapi-cdr/manifests/latest": denied: denied
hapi-fhir-measure Error Head "https://ghcr.io/v2/bellese/mct2-hapi-measure/manifests/latest": denied: denied
```

`deploy-prod.sh` runs `docker compose pull --ignore-pull-failures || true` by design, so a
registry hiccup can't block an otherwise-healthy deploy. That also means this failure is
silent: the pull fails, the deploy continues, and the HAPI containers keep running on
whatever image is already cached on the instance.

**What this does and does not affect.** Three things could plausibly be stale, and only one
of them is:

- **HAPI Spring configuration** — not stale. It comes from the `environment:` blocks in
  `docker-compose.yml`, which is refreshed from git on every deploy. This includes
  `synchronization.strategy=sync`, the IG variables, and the memory settings.
- **FHIR data** — not stale, and not from the image at all. Production does *not* apply
  `docker-compose.prebaked.yml` (`deploy-prod.sh:42-44` uses only `docker-compose.yml` +
  `docker-compose.prod.yml`), so the `cdrdata` and `measuredata` named volumes mount over
  `/data/hapi` and shadow whatever H2 store the image contains. Production's data is what
  the `seed` service loaded into those volumes, and it persists across redeploys.
- **The HAPI binary version** — this is the stale one. Weekly bakes never reach production.

So the practical effect is narrow: production runs an old HAPI build, while its config and
data are current. It is easy to miss precisely because the two things people check are both
sourced from somewhere else.

Repointing the pin at `lenny-hapi-*` would fix the pull, but it is a decision rather than a
cleanup: it would newly subject production to the weekly bake cycle. Tracked separately —
see the note at the end of this document.

---

## 3. What lives outside this repo

Everything production depends on that is not in git. Each row names what it is and what
creates it, so this table doubles as a disaster-recovery checklist.

| Where | What | Created by |
|-------|------|------------|
| **AWS SSM Parameter Store** (`/leonard/prod/`) | `POSTGRES_PASSWORD` and `CDR_FERNET_KEY` — the only two infrastructure secrets. `fetch-prod-secrets.sh` requires exactly these two | `scripts/bootstrap-aws.sh` |
| **AWS SSM Document** | `leonard-deploy` — the commands the instance runs on deploy | `scripts/bootstrap-ssm-document.sh`, from `ssm/leonard-deploy-document.json` |
| **AWS IAM** | The GitHub OIDC provider; the `leonard-github-deploy` role (assumed by CI); the `leonard-ec2-prod` role and instance profile; the `leonard-prod-ssm-read` policy; an inline `CloudWatchLogsWrite` policy | `scripts/bootstrap-github-deploy.sh` and `scripts/bootstrap-aws.sh`, from the JSON in `iam/` |
| **EC2 instance** | The `/opt/leonard` git checkout; `/opt/leonard/.env` (**not in git** — holds `CADDY_HOST` and the HAPI image pins); `/run/leonard/*` secret files (tmpfs, rewritten every deploy); the `cdrdata`, `measuredata`, and `pgdata` named volumes; Caddy's TLS certificates in `caddy_data` | manual instance setup, then `scripts/deploy-prod.sh` |
| **CloudWatch** | Five log groups — `/leonard/{caddy,backend,hapi-cdr,hapi-measure,frontend}`, 90-day retention — plus `/leonard/deploy` for SSM output, two metric filters, and two alarms | `scripts/bootstrap-aws.sh` |
| **SNS** | The `leonard-alerts` topic and its email subscription, which the alarms publish to | `scripts/bootstrap-aws.sh` |
| **GHCR** | The two baked HAPI packages (public) | `.github/workflows/bake-hapi-image.yml` |
| **GitHub** | Branch protection on `main`; the `github-pages` environment; the Actions secret `AWS_DEPLOY_ROLE_ARN` — which is **vestigial**: no workflow reads it, `deploy.yml` hardcodes the role ARN directly | manual |
| **PostgreSQL** | User-entered CDR and MCS connection credentials, in the `auth_credentials` column of `cdr_configs` and `mcs_configs`, encrypted with `CDR_FERNET_KEY` | entered through Settings in the app |
| **DNS** | `lenny.bellese.dev` and `api.lenny.bellese.dev`, pointing at the instance | manual, outside AWS |

### Two kinds of credentials

Worth separating, because they behave differently:

**Infrastructure secrets** are the two SSM parameters. They exist before the application
starts and are needed to boot it.

**Application credentials** are what a user types into Settings to connect Lenny to their
own CDR or measure engine — passwords and bearer tokens. Those live encrypted in Postgres,
not in SSM. `CDR_FERNET_KEY` is the key that protects them, which is why losing that key
means losing every stored connection credential, while losing the *database* means losing
the encrypted values themselves.

### What no script can recreate

Re-running the bootstrap scripts idempotently rebuilds everything in that table except two
things:

- **`/opt/leonard/.env`** — nothing in this repo generates it. It holds `CADDY_HOST` and the
  HAPI image pins.
- **DNS** — managed outside AWS.

Those are the two gaps that would bite during a rebuild.

### How a secret reaches a container

```
AWS SSM Parameter Store  (/leonard/prod/*)
  └─ scripts/fetch-prod-secrets.sh   (instance-profile creds; no static keys)
       └─ /run/leonard/env           (tmpfs, mode 0600, gone on reboot)
            └─ /run/leonard/{POSTGRES_PASSWORD,CDR_FERNET_KEY}
                 └─ Docker secrets → /run/secrets/* inside the container
```

`docs/architecture.md` § Prod secrets documents each hop, including which service reads
which file and why the DB password needs a reconcile step.

One asymmetry that looks like a bug and isn't: `/run/leonard/POSTGRES_PASSWORD` is written
mode `0600`, but `/run/leonard/CDR_FERNET_KEY` is written `0644`. The reason is *when* each
is read. `backend/docker-entrypoint.sh` reads the password as root and then drops to an
unprivileged user via `gosu`, so `0600` is fine. The Fernet key is read later, by
`backend/app/services/credential_crypto.py` at application startup — after the privilege drop. Compose
bind-mounts file-based secrets with host permissions intact, so a root-only key would be
unreadable to the app user and the backend would fail to start. Tightening this without
breaking it is tracked separately, below.

---

## Known issues, tracked separately

Two items found while documenting this pipeline that need fixing outside a docs change:

1. **The production HAPI image pins are stale** (`mct2-hapi-*` → 403), so `docker compose
   pull` fails silently on every deploy and production's HAPI binary never updates. Fixing
   it means deciding whether production should track the weekly bake at all.
2. **`/run/leonard/CDR_FERNET_KEY` is world-readable** (`0644`) on the instance. The fix is
   to grant the app user without granting everyone (`-g 1000 -m 0640`), or to read the key
   as root in the entrypoint and pass it down the way the DB password already is. Not a
   one-character change; it needs a redeploy to verify.

---

## See also

| Document | What it covers |
|----------|----------------|
| `docs/architecture.md` | Service map, data flow, HAPI configuration, environment variables, prod secrets |
| `docs/workflow.md` | Branch and PR workflow, manual redeploy, break-glass SSH, GitHub Actions secrets |
| `docs/runbooks/local-vs-prod-deploy.md` | Why local and prod differ, and what that means when debugging |
| `docs/runbooks/ghcr-pull-auth.md` | GHCR package visibility and pull authentication |
| `docs/runbooks/factory-reset.md` | Wiping Lenny back to a blank slate |
| `docs/runbooks/rotate-db-password.md` | Rotating `POSTGRES_PASSWORD` |
| `docs/runbooks/measure-engine-h2-recovery.md` | Recovering a corrupt measure-engine H2 store |
| `docs/runbooks/cloudwatch-logs.md` | Reading production logs and alarms |
