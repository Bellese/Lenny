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

## Recurring bug: HAPI async-indexing race

**Read this before chasing any "wrong populations" or "validation pass-rate" symptom.**

PRs #142, #155, #159, #161, #167+ have all patched a different slice of the same disease: HAPI's writes are async-indexed by default; reads against that data race the indexing. Symptoms include `/jobs` returning 0 errors but populations zero or way off, `$everything` returning only the Patient (no clinical resources), `Encounter?patient=` returning 0 for a patient that's clearly in the database, and same-input evaluations producing different results across runs.

**Quick triage rule:** for any of those symptoms, suspect indexing latency BEFORE suspecting CQL bugs, terminology contamination, or reset architecture. Verify by reading the resource directly via `/{Type}/{id}` (works regardless of index) and comparing to what search returns.

**HAPI's actual behavior** (verified empirically 2026-04-25): PUT/POST 200 means the resource is durable. Search consistency is async, governed by `hibernate.search.backend.io.refresh_interval` (we have 100ms). Hibernate Search 6's default strategy commits to disk but does NOT request an index refresh — searches see stale snapshots until the next refresh tick, OR forever if the refresh somehow stalls. The structural fix is `hibernate.search.indexing.plan.synchronization.strategy=sync` on both HAPI services' Spring config (write throughput hit, but the bug class becomes impossible).

**Pitfalls to avoid:**
- `trigger_reindex_and_wait(base_url)` in `backend/app/services/fhir_client.py` without a probe_patient_id falls back to `Patient?_count=1` and silent-skips with a warning when the index isn't ready (exactly when waits matter most). Always use `trigger_reindex_and_wait_for_patients(base_url, [pids], timeout)` and pass the actual just-pushed patient IDs.
- Reindex calls that target only `Encounter` — measures also query Condition/Observation/Procedure/MedicationRequest/MedicationAdministration. Reindex all relevant types.
- `/validation/upload-bundle` currently returns 200 before CDR is fully indexed; subsequent `/jobs` runs race the index. There is no CDR-side wait.
- `$everything` is a victim of this same async-index, not a cause. Don't propose replacing it — it's a key FHIR-standard operation. Fix the index, not the call.

Design doc with full evidence and option analysis: `~/.gstack/projects/Bellese-mct2/2026-04-25-hapi-consistency-model.md`.

## Local-first iteration — MANDATORY pre-push checklist

> **DO NOT `git push` ANY PR until ALL of the checks below have run successfully on your machine.**
> "Validate locally" does NOT mean "ran unit tests." It means EVERY check below.
> CI is not a debugger. Prod is not a debugger. Sutton's time is not a debugger.

**Required local checks before `git push` of ANY PR (no exceptions for "small" or "obvious" fixes):**

1. `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` — lint clean
2. `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q` — unit suite passes
3. Run the CI-equivalent integration suite — **same ignore flags that `pr-checks.yml` uses, against real HAPI containers.** This is the suite that fails most often in CI; it MUST pass locally first. Takes ~3-5 min.
   ```bash
   ./scripts/run-integration-tests.sh \
     --ignore=tests/integration/test_golden_measures.py \
     --ignore=tests/integration/test_connectathon_measures.py \
     --ignore=tests/integration/test_full_workflow.py
   ```
   (The full suite — `./scripts/run-integration-tests.sh` with no flags — runs 600+ connectathon-measure patient tests that CI skips on PRs. Only run those when changing the measure evaluation pipeline or before a nightly run.)
4. End-to-end smoke against a local stack (`docker compose up -d` with `HAPI_CDR_IMAGE` and `HAPI_MEASURE_IMAGE` set in `.env` for the fast path — see `.env.example`; falls back to vanilla hapiproject/hapi:v8.8.0-1 if vars are unset) for any change touching:
   - The data flow (`fhir_client.py`, `validation.py`, `orchestrator.py`)
   - HAPI behavior or configuration
   - Bundle import / `$everything` / `$evaluate-measure` paths
   - After any wipe+push cycle in the smoke run, probe `$everything` on at least one patient to confirm the full clinical bundle comes back (not just the Patient resource). Use Python — the shell strips `$` from these URLs:
     ```bash
     docker exec leonard-backend-1 python3 -c "
     import httpx, sys
     pid = sys.argv[1]
     r = httpx.get(f'http://hapi-fhir-measure:8080/fhir/Patient/{pid}/\$everything', timeout=30)
     types = {e['resource']['resourceType'] for e in r.json().get('entry', [])}
     print('resource types in bundle:', types)
     assert 'Encounter' in types, 'FAIL: $everything returned only Patient — HAPI index not ready'
     " <a-patient-id-in-scope>
     ```
5. The "ship-or-not" gate: if step 1, 2, 3, or 4 didn't run successfully, **do not push**. Tell Sutton what's blocking instead.

If the change is documentation-only (`*.md`, no code), steps 1–4 are not required, but step 5 still applies — confirm in the PR description that no code changed.

**Reproduce the bug on the local stack FIRST** when investigating any "wrong populations" / "validation pass-rate" / "404 from HAPI" / "$everything returns only Patient" symptom. Don't propose code changes until you have a local repro that fails the same way as prod. The local stack catches the same bugs in minutes; running blind in prod has cost multi-hour iteration loops on this codebase already.

**Track record this rule responds to** (kept here so a future agent doesn't repeat the cycle):
- 2026-04-25 session: ~6 hours of iteration on PRs #167 → #178 chasing /jobs CMS124 wrong populations. Multiple PRs were pushed without running the integration suite locally; CI surfaced breakages that local would have caught in minutes. The actual root cause (HAPI bundle-import forward-reference index miss) was not isolated until the full local A/B/C empirical experiment was run. Don't repeat the pattern.

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
