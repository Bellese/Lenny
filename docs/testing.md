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

**Golden tests validate exact population counts** against the expected MeasureReports
embedded in each bundle. For each test case MeasureReport in the bundle, the test
calls `$evaluate-measure` and compares actual vs. expected population counts for
`initial-population`, `denominator`, `denominator-exclusion`, `numerator`, and
`numerator-exclusion`.

**Active golden bundles** (EXM124 + 8 connectathon bundles, all in `golden/`):

| Bundle | Test cases | Notes |
|--------|-----------|-------|
| EXM124_FHIR4-8.2.000 | 2 | numer + denom patients |
| EXM104_FHIR4-8.1.000 | 2 | numer + denom patients |
| EXM105_FHIR4-8.1.000 | 2 | numer + denom patients |
| EXM108_FHIR4-8.2.000 | 2 | numer + denom patients |
| EXM125_FHIR4-7.2.000 | 2 | numer + denom patients |
| EXM130_FHIR4-7.2.000 | 2 | numer + denom patients |
| EXM165_FHIR4-8.5.000 | 0 | measure-only bundle, no patients — skipped |
| EXM506_FHIR4-2.1.000 | 0 | patients present but no expected MeasureReports — skipped |
| EXM529_FHIR4-1.0.000 | 0 | patient present but no expected MeasureReports — skipped |

Bundles with 0 test cases are skipped by pytest (not failed); a warning is emitted.

---

## CI Checks (PR Gate)

Every PR to `main` triggers `.github/workflows/pr-checks.yml` with 4 jobs:

| Job | What it does | Fails if |
|-----|-------------|----------|
| **Unit Tests + Coverage** | Runs all unit tests with `--cov-fail-under=70` | Any test fails OR coverage < 70% |
| **Lint** | `ruff check` + `ruff format --check` | Any lint or formatting violation |
| **Integration Tests** | Spins up Docker containers, runs full integration suite | Any integration test fails |
| **Frontend Build** | `npm ci && npm run build` | Build fails |

All 4 jobs must pass before a PR can merge.

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
