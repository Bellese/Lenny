# Architecture — MCT2

## Service Map

| Service | Image | Role | Exposed port |
|---------|-------|------|-------------|
| frontend | local build | React web UI | 3001 |
| backend | local build | FastAPI orchestrator | 8000 |
| db | postgres:16-alpine | Job tracking, results, config | internal (5432) |
| hapi-fhir-cdr | hapiproject/hapi:v8.6.0-1 | Default clinical data repository | internal (8080) |
| hapi-fhir-measure | hapiproject/hapi:v8.6.0-1 | Measure calculation engine | internal (8080) |
| seed | local build | One-time data loader (exits after run) | none |

The CDR and Measure Engine are intentionally separate. The CDR is replaceable — users connect their own FHIR server in Settings. The Measure Engine is permanent and is the only service with `hapi.fhir.cr.enabled=true`.

## Backend Structure

```
backend/app/
  main.py           FastAPI app entry point, router registration
  config.py         pydantic-settings configuration (see Environment Variables below)
  db.py             async SQLAlchemy engine + session factory

  models/
    job.py          Job, MeasureReport
    validation.py   ExpectedResult, ValidationRun
    config.py       AppConfig (CDR URL, auth)
    base.py         SQLAlchemy declarative base

  routes/
    health.py       GET /health
    jobs.py         POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel,
                    GET /jobs/{id}/comparison (actual vs. expected population counts)
    measures.py     GET /measures, POST /measures/upload
    results.py      GET /results, GET /results/{job_id}
    settings.py     GET /settings, PUT /settings, POST /settings/test
    validation.py   POST /validation/upload, GET /validation/runs

  services/
    orchestrator.py  Core job execution. Pulls patients, runs $evaluate-measure in batches,
                     stores MeasureReports. Group filtering via group_id param.
    fhir_client.py   All FHIR server communication. DataAcquisitionStrategy ABC with two
                     implementations: BatchQueryStrategy (paginated /Patient + $everything)
                     and DataRequirementsStrategy (DEQM spec — calls $data-requirements on
                     the measure engine, then fetches only the required resource types from
                     the CDR; falls back to $everything on any failure).
    bundle_loader.py Startup bundle loader. Called once during FastAPI lifespan. Scans
                     seed/connectathon-bundles/, waits for HAPI readiness, then loads each
                     .json file via triage_test_bundle (Measure/Library → MCS, clinical
                     resources → CDR, test-case MeasureReports → ExpectedResult table).
    validation.py    Test bundle parsing, ExpectedResult comparison, pass/fail logic.
    worker.py        Background task queue, priority ordering, job lifecycle management.
```

## Frontend Structure

```
frontend/src/
  App.js              Main app with react-router-dom v6 routing
  pages/
    JobsPage.js       Create and monitor calculation jobs
    MeasuresPage.js   Upload and view FHIR Measure bundles
    ResultsPage.js    Aggregate population summaries + patient drill-down
    SettingsPage.js   CDR connection configuration
    ValidationPage.js Upload test bundles, view pass/fail results
  components/
    ComparisonView.js Per-patient actual vs. expected population comparison panel
    PatientDetail.js  Per-patient result expansion panel
    ProgressBar.js    Job progress indicator
    Toast.js          Notification component
  api/
    client.js         Axios-based backend API client
```

Each page has a co-located CSS Module (`PageName.module.css`). The app is plain JavaScript — no TypeScript.

## Data Flow

```
User (browser)
  │  HTTP
  ▼
FastAPI (backend:8000)
  │  async httpx
  ├──► CDR (hapi-fhir-cdr or user's external FHIR server)
  │     Patient, Group resources
  │
  ├──► Measure Engine (hapi-fhir-measure)
  │     POST /fhir/$evaluate-measure
  │     Returns MeasureReport resources
  │
  └──► PostgreSQL (db)
        Job status, MeasureReports, ExpectedResults, AppConfig
```

The orchestrator fetches patients from the CDR (all patients, or group members if `group_id` set), batches them, pushes each batch to the Measure Engine via `$evaluate-measure`, and stores the resulting MeasureReports in PostgreSQL. The worker service manages job state and handles background execution.

## HAPI FHIR Configuration

Critical settings and why they are set:

| Setting | Service | Value | Reason |
|---------|---------|-------|--------|
| `hapi.fhir.cr.enabled` | measure | `true` | Enables CQL/Clinical Reasoning support required for `$evaluate-measure` |
| `hapi.fhir.client_id_strategy` | cdr | `ANY` | Accepts CMS numeric patient IDs (not just UUIDs) |
| `hapi.fhir.allow_external_references` | cdr | `true` | Required for CMS FHIR bundles with cross-resource references |
| `hapi.fhir.defer_indexing_for_codesystems_of_size` | both | `0` | Disables deferred indexing to avoid startup latency |
| `spring.jpa.properties.hibernate.search.enabled` | measure | `true` | Enables Hibernate Search / Lucene full-text indexing (required for `$data-requirements` and value-set expansion lookups) |
| `spring.jpa.properties.hibernate.search.backend.type` | measure | `lucene` | Uses embedded Lucene backend (no external search cluster needed) |

Storage: both instances use H2 file-based storage under `/data/hapi` (mounted as Docker volumes). This is appropriate for local/demo use. Production deployments should use external Postgres.

## Environment Variables

Defined in `backend/app/config.py`. All overridable via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://mct2:mct2@db:5432/mct2` | Async PostgreSQL connection string |
| `MEASURE_ENGINE_URL` | `http://hapi-fhir-measure:8080/fhir` | Measure Engine FHIR base URL |
| `DEFAULT_CDR_URL` | `http://hapi-fhir-cdr:8080/fhir` | Default CDR FHIR base URL |
| `BATCH_SIZE` | `100` | Patients per `$evaluate-measure` batch |
| `MAX_WORKERS` | `4` | Concurrent job worker threads |
| `MAX_RETRIES` | `3` | Retry attempts for failed FHIR requests |
| `HAPI_INDEX_WAIT_SECONDS` | `5` | Wait after uploading patients before evaluating (index propagation) |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ALLOWED_ORIGINS` | `"*"` | Comma-separated CORS allowed origins; `"*"` for wildcard (local dev default). Set to `https://${CADDY_HOST}` in production via `docker-compose.prod.yml`. |

## Test Infrastructure

**Unit tests** (`backend/tests/test_*.py`):
- Use pytest with pytest-asyncio
- Database: SQLite in-memory (via `aiosqlite`)
- FHIR servers: mocked with `respx` (async HTTP mocking)
- Run with: `cd backend && python -m pytest tests/ --ignore=tests/integration -v`

**Integration tests** (`backend/tests/integration/`):
- Require live infrastructure: Postgres (port 5433), HAPI CDR (port 8180), HAPI Measure (port 8181)
- Spun up via `docker-compose.test.yml`
- Run with: `./scripts/run-integration-tests.sh`
- Marked with `@pytest.mark.integration`
