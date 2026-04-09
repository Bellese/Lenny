# MCT2 (Leonard)

## Build & Test

```bash
# Unit tests (run from repo root)
cd backend && python -m pytest tests/ --ignore=tests/integration -v

# Integration tests (spins up real HAPI FHIR + Postgres containers)
./scripts/run-integration-tests.sh

# Frontend dev server (port 3001)
cd frontend && npm start
```

## Architecture

5 Docker services (frontend :3001, backend :8000, db, hapi-fhir-cdr, hapi-fhir-measure). Full service map, data flow, HAPI configuration, and environment variables in `docs/architecture.md`.

## Code Conventions

- **Commits:** conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`)
- **Python:** 3.10+, `X | None` union syntax OK, type hints required
- **React:** plain JavaScript (not TypeScript), PascalCase components, co-located CSS Modules (`Foo.module.css`)
- **Config:** all values via environment variables (`backend/app/config.py`) — never hardcoded
- **PRs:** construct PR bodies to match the sections in `.github/pull_request_template.md` exactly (Summary, Related issue, Type of change, Checklist, Test plan). `gh pr create` does not auto-populate from the template file — build the body explicitly.

## Workflow

Branches: `feature/*`, `fix/*`, or `chore/*` off `master`, merged via PR. Always use a git worktree for development (`git worktree add ../mct2-<branch> -b <branch> origin/master`) — create a new branch in an isolated sibling directory rather than working directly on the current branch. If you are already inside a worktree, do not create another nested worktree.
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

## AWS

- **Profile:** `leonard` (account `439475769170`). Always use `AWS_PROFILE=leonard` for any AWS CLI commands.
- EC2 instance: `i-0f00585639d2f3ef1`, t3.medium (4 GB RAM), Elastic IP `98.89.219.217`, region `us-east-1`
- Live URLs: `https://98-89-219-217.nip.io` (UI), `https://api.98-89-219-217.nip.io` (API)

## Do NOT

- Hardcode URLs or credentials — use environment variables
- Use Python 3.9-style `Optional[X]` — `X | None` is preferred
- Modify HAPI FHIR H2 storage paths without reading `docs/architecture.md`
