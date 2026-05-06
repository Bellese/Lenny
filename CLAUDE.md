# Lenny

## Build & Test

| Suite | Command | Runs when |
|---|---|---|
| Lint | `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` | every PR + before push |
| Unit | `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v` | every PR + before push |
| Coverage (≥70% floor) | `cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing` | optional locally; CI reports |
| Integration (CI-equivalent, what `pr-checks.yml` runs) | `./scripts/run-integration-tests.sh --ignore=tests/integration/test_golden_measures.py --ignore=tests/integration/test_connectathon_measures.py --ignore=tests/integration/test_full_workflow.py --ignore=tests/integration/test_groups_dropdown.py --ignore=tests/integration/test_full_jobs_pipeline.py` | **every PR + before push** (most-flaky-in-CI suite, ~3–5 min) |
| Full workflow only | `./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py` | before merging any change to the measure pipeline / FHIR data flow / job orchestration |
| Integration (full / connectathon source-of-truth) | `./scripts/run-integration-tests.sh` (no flags — adds 600+ connectathon-measure patient tests CI skips on PRs) — or trigger nightly via Actions → Connectathon Measures | nightly automatic + manual pre-merge for measure-engine or HAPI-bump changes |
| Frontend dev server | `cd frontend && npm start` (port 3001) | local dev only |

**Decision tree:**
- Pushing a PR? → Lint + Unit + CI-equivalent integration (no skipping).
- Touching `measure_*` / `orchestrator.py` / `fhir_client.py` / `validation.py`? → Add Full workflow + Jobs pipeline validation (see below).
- Adding measures or bumping HAPI? → Run the full integration suite (or manually trigger the weekly Connectathon Measures workflow) before merge.
- Validating that Lenny's Jobs API produces correct numerator/denominator counts? → `USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_full_jobs_pipeline.py` (requires prebaked images with Groups; ~30–50 min for all 11 measures). Or run the standalone script: `python scripts/validate_all_measures.py`.

The weekly Connectathon Measures workflow has four independent jobs: **Bundle Loader Test** (vanilla HAPI), **Connectathon Eval** (pre-baked HAPI), **Jobs Pipeline Validation** (pre-baked HAPI, validates Lenny orchestration layer), and **Full Workflow** (clean nightly run). See `docs/testing.md` for the full strategy.

## Recurring bug: HAPI async-indexing race

**Read this before chasing any "wrong populations" / "validation pass-rate" / "$everything returns only Patient" / "Encounter?patient= returns 0" symptom.**

### Triage rule (30 seconds)

1. Read the resource directly: `GET /{Type}/{id}` — works regardless of index state.
2. Compare to what `/{Type}?patient=...` returns.
3. Direct read shows the data and search doesn't? → it's the index, not your code.

### What's actually happening

PUT/POST 200 means the resource is durable. Search consistency is async, governed by `hibernate.search.backend.io.refresh_interval` (we have 100ms). Hibernate Search 6's default strategy commits to disk but does NOT request an index refresh — searches see stale snapshots until the next refresh tick fires, or forever if it stalls under load.

### Current consistency gate

`HAPI_SYNC_AFTER_UPLOAD=true` (default) makes the backend wait for index refresh after uploads — code paths in `backend/app/services/validation.py` and `backend/app/services/orchestrator.py`. Setting it to `false` is the first lever to pull when debugging: if the symptom changes, async-indexing is the cause.

### Pitfalls (each one cost a PR)

- Use `trigger_reindex_and_wait_for_patients(base_url, [pids], timeout)` in `backend/app/services/fhir_client.py` — **not** `trigger_reindex_and_wait(base_url)` (no probe id), which falls back to `Patient?_count=1` and silent-skips when the index isn't ready.
- Reindex Condition / Observation / Procedure / MedicationRequest / MedicationAdministration — measures don't query Encounter alone.
- `/validation/upload-bundle` returns 200 before CDR indexing completes; subsequent `/jobs` runs race the index. There is no CDR-side wait.
- `$everything` is a victim of this bug, not a cause. Don't propose replacing it.

### Structural fix (applied in PR #206)

`spring.jpa.properties.hibernate.search.indexing.plan.synchronization.strategy=sync` is now set on both HAPI services in `docker-compose.yml`, `docker-compose.test.yml`, and both seeded Dockerfiles. POST/PUT blocks until the Lucene index is refreshed, eliminating the bug class. The Python-side compensator (`HAPI_SYNC_AFTER_UPLOAD` + `trigger_reindex_and_wait*`) is still present as a rollback safety net; removal is a follow-up.

### History

PRs #142, #155, #159, #161, #167+ each patched a slice of this same disease.

## Local-first iteration — MANDATORY pre-push checklist

> **DO NOT `git push` ANY PR until ALL of the checks below have run successfully on your machine.**
> "Validate locally" does NOT mean "ran unit tests." It means EVERY check below.
> CI is not a debugger. Prod is not a debugger. Reviewers' time is not a debugger.

**Required local checks before `git push` of ANY PR (no exceptions for "small" or "obvious" fixes):**

1. **Lint** (Build & Test table, "Lint" row) — clean.
2. **Unit suite** ("Unit" row) — passes.
3. **CI-equivalent integration suite** ("Integration (CI-equivalent…)" row) — **passes against real HAPI containers.** This is the suite that fails most often in CI; it MUST pass locally first (~3–5 min). The full integration suite (no `--ignore` flags) runs 600+ connectathon-measure patient tests CI skips on PRs — only run those when changing the measure evaluation pipeline or before a nightly run.
4. End-to-end smoke against a local stack (`cp .env.example .env && docker compose up -d` — `.env.example` sets `COMPOSE_FILE=docker-compose.yml:docker-compose.prebaked.yml` plus the prebaked HAPI image vars so the fast path is the default; falls back to vanilla `hapiproject/hapi:v8.8.0-1` if those vars are removed) for any change touching:
   - The data flow (`fhir_client.py`, `validation.py`, `orchestrator.py`)
   - HAPI behavior or configuration
   - Bundle import / `$everything` / `$evaluate-measure` paths
   - After any wipe+push cycle in the smoke run, probe `$everything` on at least one patient — see `docs/runbooks/everything-probe.md` for the script (the shell strips `$`, so use Python).
5. **New or modified `tests/integration/` files** — run those exact files locally before pushing. The CI-equivalent suite uses `--ignore` flags and will **silently skip** any new integration test; you must run it yourself. For prebaked-only tests (check for `HAPI_PREBAKED` guard or `_require_prebaked_stack`): `USE_PREBAKED=1 ./scripts/run-integration-tests.sh <test_file>`. No exceptions — not even for the test you just wrote.
6. The "ship-or-not" gate: if steps 1–5 didn't all pass, **do not push.** Say what's blocking in the PR description instead.

If the change is documentation-only (`*.md`, no code), steps 1–4 are not required, but step 5 still applies — confirm in the PR description that no code changed.

**Reproduce the bug on the local stack FIRST** when investigating any "wrong populations" / "validation pass-rate" / "404 from HAPI" / "$everything returns only Patient" symptom. Don't propose code changes until you have a local repro that fails the same way as prod.

## Architecture

5 Docker services (frontend :3001, backend :8000, db, hapi-fhir-cdr, hapi-fhir-measure). Local dev (per `.env.example`) and CI use `docker-compose.prebaked.yml` (bundles + IGs baked into the image, PR #199). Production currently runs vanilla `hapiproject/hapi:v8.8.0-1` — the `seed` service POSTs the connectathon bundles into a persistent H2 volume on first boot, and the volume keeps them warm across redeploys. Whether to switch prod to prebaked is an open question; see `docs/decisions.md`. Full service map, data flow, HAPI configuration, and environment variables in `docs/architecture.md`.

## Code Conventions

- **Commits:** conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`)
- **Python:** 3.10+, `X | None` union syntax OK, type hints required
- **React:** plain JavaScript (not TypeScript), PascalCase components, co-located CSS Modules (`Foo.module.css`)
- **Config:** all values via environment variables (`backend/app/config.py`) — never hardcoded
- **PRs:** use `.github/pull_request_template.md` sections (`gh pr create` does not auto-populate — build the body explicitly)

## Workflow

Branches: `feature/*`, `fix/*`, or `chore/*` off `main`, merged via PR. Always work in a git worktree (`git worktree add ../lenny-<branch> -b <branch> origin/main`) — never commit directly on the current branch.
Work items: GitHub Issues on the [project board](https://github.com/orgs/Bellese/projects/33/views/3).

| Phase | Command | Toolkit |
|-------|---------|---------|
| Ideate | `/office-hours` | gstack |
| Plan | `/brainstorming` then `/writing-plans` | superpowers |
| Build | `/subagent-driven-development` | superpowers |
| Review | `/review` | gstack |
| Ship | `/ship` | gstack |
| Verify | `/qa` + `/browse` | gstack |

Shortcuts: bug fixes start at Build (use `/investigate` for root cause); small tasks skip Ideate and Plan; spikes are Ideate only. See `docs/workflow.md` for full details.

## AWS

**Always export `AWS_PROFILE=leonard` before any AWS CLI call.** Using any other profile/account is a bug — Claude has gotten this wrong before. Verify with `aws sts get-caller-identity` if unsure.

- Region: `us-east-1`
- Prod runs on a single t3.medium EC2 instance (look up by tag with `aws ec2 describe-instances --filters "Name=tag:Name,Values=lenny-prod"`).
- Live: `https://lenny.bellese.dev` (UI), `https://api.lenny.bellese.dev` (API)

## Do NOT

- Hardcode URLs or credentials — use environment variables
- Use Python 3.9-style `Optional[X]` — `X | None` is preferred
- Modify HAPI FHIR H2 storage paths without reading `docs/architecture.md`
- Modify `TODOS.md` — it is frozen 2026-04-27. Open a GitHub Issue for any new work item.

## External toolkit commands

These are gstack / superpowers slash commands — **not** harness-loadable skills. Don't try to invoke them via the Skill tool; surface the right one when the user's intent matches and let them run it. (The Workflow table above maps phases to commands; this list maps intents.)

- Product ideas / brainstorming → `/office-hours`
- Strategy / scope → `/plan-ceo-review`
- Architecture → `/plan-eng-review`
- Design system / plan review → `/design-consultation` or `/plan-design-review`
- Full review pipeline → `/autoplan`
- Bugs / errors → `/investigate`
- QA / testing site behavior → `/qa` or `/qa-only`
- Code review / diff check → `/review`
- Visual polish → `/design-review`
- Ship / deploy / PR → `/ship` or `/land-and-deploy`
- Save progress → `/context-save`
- Resume context → `/context-restore`
