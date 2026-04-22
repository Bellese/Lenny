# MCT2 Testing Strategy

## Overview

MCT2 has two test layers: **unit tests** (fast, mocked, run on every PR) and **integration tests** (real HAPI FHIR + PostgreSQL, run via Docker).

Coverage floor: **70%** (enforced by CI). `app/main.py` and `app/services/worker.py` are excluded (startup/lifecycle code not suited for unit testing).

---

## Unit Tests

Run from the repo root:

```bash
cd backend && python -m pytest tests/ --ignore=tests/integration -v
```

Run with coverage:

```bash
cd backend && python -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing -v
```

These tests use an **in-memory SQLite database** and mock all FHIR service calls. They complete in ~15 seconds and require no external infrastructure.

**Test files:**
| File | What it covers |
|------|----------------|
| `test_routes_health.py` | Health check endpoints |
| `test_routes_jobs.py` | Job creation and status endpoints |
| `test_routes_measures.py` | Measure listing endpoints |
| `test_routes_results.py` | Result inspection endpoints |
| `test_routes_settings.py` | CDR configuration endpoints |
| `test_routes_validation.py` | Bundle upload and validation run endpoints |
| `test_services_bundle_loader.py` | Startup connectathon bundle loading |
| `test_services_fhir_client.py` | FHIR client utilities, DataRequirementsStrategy, BatchQueryStrategy |
| `test_services_orchestrator.py` | Job orchestration service |
| `test_services_validation.py` | Bundle triage, population extraction, comparison |
| `test_cors.py` | CORS middleware behavior |

---

## Integration Tests

Integration tests spin up real HAPI FHIR instances and a PostgreSQL database via Docker.

**Prerequisites:**

```bash
# Pre-pull the HAPI image on a new machine (~2.5 GB — do this before your first run)
docker pull hapiproject/hapi:v8.8.0-1

# Start test infrastructure
docker compose -f docker-compose.test.yml up -d

# Wait ~60 seconds for HAPI to initialize
```

**Run:**

```bash
./scripts/run-integration-tests.sh
```

The script waits for HAPI health checks, runs the tests, and tears down containers automatically.

Integration tests are marked `@pytest.mark.integration` and live in `backend/tests/integration/`.

**Integration test files:**

| File | What it covers |
|------|----------------|
| `test_fhir_operations.py` | Core FHIR client operations against a live HAPI instance |
| `test_smart_load.py` | `triage_test_bundle` + bundle loader against live HAPI |
| `test_golden_measures.py` | End-to-end `$evaluate-measure` against golden bundles |
| `test_connectathon_measures.py` | Parametrized per-test-case run across all connectathon bundles (skipped on the PR gate — runs nightly via `connectathon-measures.yml`) |
| `test_full_workflow.py` | Full-stack pipeline covering job orchestration → measure eval → result storage (skipped on the PR gate — runs nightly) |

---

## Connectathon Measure Tests (manifest-driven)

`backend/tests/integration/test_connectathon_measures.py` runs one parametrized pytest case per test-case `MeasureReport` found in the connectathon bundles. The set of measures under test — and which ones trigger strict assertions — is controlled entirely by `seed/connectathon-bundles/manifest.json`.

### Manifest structure

Each entry in `manifest.json` has a `"strict"` field. All current entries have `"strict": true`. Entries with `"expected_test_cases": 0` are definition-only bundles (no patient-level test cases) and produce no parametrized test cases; they are silently skipped.

```json
{
  "id": "CMS1017FHIRHHFI",
  "bundle_file": "CMS1017FHIRHHFI-bundle.json",
  "expected_test_cases": 65,
  "strict": true,
  "known_issues": []
}
```

### STRICT_STU6 env var

The `STRICT_STU6` environment variable controls how population-count mismatches are handled during test runs:

| Value | Behavior |
|-------|----------|
| `STRICT_STU6=1` | Hard-fail on any population mismatch or HTTP error. |
| `STRICT_STU6=0` (current CI default) | Log the mismatch as a warning and mark the test as skipped rather than failed. Use this while onboarding a new measure whose CQL is known to diverge from MADiE. |

```bash
# Strict mode
STRICT_STU6=1 ./scripts/run-integration-tests.sh

# Soft mode — mismatches warn instead of fail
STRICT_STU6=0 ./scripts/run-integration-tests.sh
```

CI (`.github/workflows/pr-checks.yml`) currently sets `STRICT_STU6=0` during the rollout period. Flip to `STRICT_STU6=1` once all connectathon measures pass cleanly in CI. See `docs/connectathon-measures-status.md` for the current per-measure state.

---

## Connectathon Rehearsal Script

`scripts/connectathon-rehearsal.sh` is an operator-level end-to-end smoke test for the full connectathon workflow. Run it before a connectathon event or after a significant infrastructure change.

**What it does (six steps):**

1. Cold-starts Docker services (`docker compose down -v && up -d`) unless `--no-restart` is passed.
2. Polls `GET /health` until all services (db, measure engine, CDR) report connected — up to 5 minutes.
3. Asserts all measures listed in `manifest.json` are loaded via `GET /measures`.
4. Triggers `$evaluate-measure` for the first measure with `expected_test_cases > 0`, polls until complete, and prints population counts.
5. Prints a full status table: one row per manifest measure showing `loaded | evaluated | populations_match | notes`.
6. Exits nonzero if any row fails; exits zero on full pass.

**Usage:**

```bash
# Full cold-start rehearsal (tears down volumes — wipes HAPI data)
./scripts/connectathon-rehearsal.sh

# Rehearsal against already-running containers (no restart)
./scripts/connectathon-rehearsal.sh --no-restart
```

All output is written to the console and appended to `rehearsal.log` in the repo root. The log file is gitignored and safe to keep between runs as a timing history.

**When to run:** before any connectathon event, after pulling a new HAPI image, or after updating measure bundles. The `--no-restart` flag is useful for quick re-checks after a code fix without waiting for HAPI to reload IGs from scratch.

---

## Golden File Test Cases

Golden tests validate the **full measure evaluation pipeline**: a test bundle is loaded into HAPI, `$evaluate-measure` is called, and the MeasureReport structure is verified.

**Location:** `backend/tests/integration/golden/`

Each test case is a directory with a `bundle.json`:

```
tests/integration/golden/
  basic-measure/
    bundle.json   <- FHIR transaction bundle with Library, Measure, Patient
```

**To add a new golden test case**, create a new directory with a `bundle.json`:

```bash
mkdir -p backend/tests/integration/golden/my-measure
# Write bundle.json with Library, Measure, and Patient resources
# Use unique IDs (e.g. "my-measure-001") to avoid conflicts with seed data
```

The test runner (`test_golden_measures.py`) discovers and runs all bundles automatically.

**Golden tests currently assert structural correctness only:**
- Response is a `MeasureReport`
- At least one population group is present
- Patient reference is present

Exact population count assertions should be added once HAPI evaluation behavior is confirmed stable on CI runners.

---

## CI Checks (PR Gate)

Every PR to `main` triggers `.github/workflows/pr-checks.yml` with 4 jobs:

| Job | What it does | Fails if |
|-----|-------------|----------|
| **Unit Tests + Coverage** | Runs all unit tests with `--cov-fail-under=70` | Any test fails OR coverage < 70% |
| **Lint** | `ruff check` + `ruff format --check` | Any lint or formatting violation |
| **Integration Tests** | Spins up Docker containers, runs integration suite **minus** `test_connectathon_measures.py` and `test_full_workflow.py` (both run nightly). `STRICT_STU6=0` during rollout. | Any remaining integration test fails |
| **Frontend Build** | `npm ci && npm run build` | Build fails |

All 4 jobs must pass before a PR can merge.

The nightly `connectathon-measures.yml` workflow runs the excluded tests against the full connectathon bundle set. Trigger it manually from the Actions tab (`Run workflow`) before merging any change that touches the measure evaluation pipeline, FHIR data flow, or job orchestration.

---

## Coverage Configuration

Coverage is configured in `backend/.coveragerc`:

```ini
[run]
source = app
omit =
    app/main.py
    app/services/worker.py
```

**Excluded files and rationale:**
- `app/main.py` — FastAPI app startup/lifespan, not suited for unit testing
- `app/services/worker.py` — Background job orchestration runner, not suited for unit testing

**Check coverage locally:**

```bash
cd backend && python -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing
```

The `TOTAL` line at the bottom shows the effective coverage percentage.
