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

## Workflow

Branches: `feature/*` or `fix/*` off `master`, merged via PR.
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

## Do NOT

- Hardcode URLs or credentials — use environment variables
- Use Python 3.9-style `Optional[X]` — `X | None` is preferred
- Modify HAPI FHIR H2 storage paths without reading `docs/architecture.md`
