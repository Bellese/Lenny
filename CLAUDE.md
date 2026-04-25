# MCT2 (Leonard)

## Build & Test

```bash
# Unit tests (run from repo root)
cd backend && python3 -m pytest tests/ --ignore=tests/integration -v

# Unit tests with coverage (floor: 70%)
cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing

# Integration tests (spins up real HAPI FHIR + Postgres containers)
./scripts/run-integration-tests.sh

# Lint
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/

# Frontend dev server (port 3001)
cd frontend && npm start
```

See `docs/testing.md` for the full testing strategy.

## Architecture

5 Docker services (frontend :3001, backend :8000, db, hapi-fhir-cdr, hapi-fhir-measure). Full service map, data flow, HAPI configuration, and environment variables in `docs/architecture.md`.

The backend resets the `hapi-fhir-measure` container between measure evaluations to defeat cross-bundle terminology / CodeSystem / library-cache contamination (see `backend/app/services/measure_engine_reset.py`). This requires `/var/run/docker.sock` mounted into the backend container — already wired in `docker-compose.yml` and `docker-compose.prod.yml`.

## Pre-baked HAPI images

`docker-compose.prebaked.yml` overrides the HAPI services with pre-baked images from GHCR (`ghcr.io/bellese/mct2-hapi-{cdr,measure}:latest`) that ship with QI-Core/US-Core/CQL IGs and seed data already loaded. Required by per-measure reset — recreating a non-prebaked container would re-fetch IGs from the internet on cold start (60-120s).

```bash
# Local dev with prebaked images (recommended for measure-engine work)
docker compose -f docker-compose.yml -f docker-compose.prebaked.yml up -d
```

Prod (`scripts/deploy-prod.sh`) and integration tests already use prebaked. The bake workflow runs weekly and on `seed/**` changes (`.github/workflows/bake-hapi-image.yml`).

## Code Conventions

- **Commits:** conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`)
- **Python:** 3.10+, `X | None` union syntax OK, type hints required
- **React:** plain JavaScript (not TypeScript), PascalCase components, co-located CSS Modules (`Foo.module.css`)
- **Config:** all values via environment variables (`backend/app/config.py`) — never hardcoded
- **PRs:** use `.github/pull_request_template.md` sections (`gh pr create` does not auto-populate — build the body explicitly)

## Workflow

Branches: `feature/*`, `fix/*`, or `chore/*` off `main`, merged via PR. Always work in a git worktree (`git worktree add ../mct2-<branch> -b <branch> origin/main`) — never commit directly on the current branch.
Work items: GitHub Issues on the [project board](https://github.com/orgs/Bellese/projects/33/views/3).

Follow this lifecycle for each issue. Update the issue after each phase before moving on.

| Phase | Command | Toolkit |
|-------|---------|---------|
| Ideate | `/office-hours` | gstack |
| Plan | `/brainstorming` then `/writing-plans` | superpowers |
| Build | `/subagent-driven-development` | superpowers |
| Review | `/review` | gstack |
| Ship | `/ship` | gstack |
| Verify | `/qa` + `/browse` | gstack |

Shortcuts: bug fixes start at Build (use `/investigate` for root cause); small tasks skip Ideate and Plan; spikes are Ideate only.

See `docs/workflow.md` for full details and board attribute guidance.

## Testing Requirements

New backend features require tests in `backend/tests/`. Match existing naming (`test_routes_*.py`, `test_services_*.py`). Integration tests go in `backend/tests/integration/` and use the `@pytest.mark.integration` marker.

Frontend has no test suite yet.

**Full workflow tests on large changes:** `tests/integration/test_full_workflow.py` runs nightly in its own clean job, not on PRs. For any change that touches the measure evaluation pipeline, FHIR data flow, or job orchestration, run these locally before merging:

```bash
./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py
```

Or trigger the nightly workflow manually via GitHub Actions → Connectathon Measures → Run workflow. The workflow runs the source-of-truth suite (`test_golden_measures.py` + `test_connectathon_measures.py`) and the full workflow test in separate clean jobs.

## AWS

- **Profile:** `leonard` (account `439475769170`). Always use `AWS_PROFILE=leonard` for any AWS CLI commands.
- EC2 instance: `i-0f00585639d2f3ef1`, t3.medium (4 GB RAM), Elastic IP `98.89.219.217`, region `us-east-1`
- Live URLs: `https://98-89-219-217.nip.io` (UI), `https://api.98-89-219-217.nip.io` (API)

## Do NOT

- Hardcode URLs or credentials — use environment variables
- Use Python 3.9-style `Optional[X]` — `X | None` is preferred
- Modify HAPI FHIR H2 storage paths without reading `docs/architecture.md`
- Create or update `TODOS.md` — work items go in GitHub Issues only. When a review, ship, or planning session surfaces a new task, open a GitHub issue instead.
