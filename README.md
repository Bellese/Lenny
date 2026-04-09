# MCT2 — Measure Calculation Tool v2

A free, open-source utility for calculating FHIR-based digital quality measures (dQMs). MCT2 sits between a clinical data repository and a measure calculation engine, orchestrating the evaluation workflow so quality improvement staff can compute measures without vendor dependency.

## Quick Start

```bash
docker compose up
```

Open http://localhost:3001. A demo measure and test patients are pre-loaded — you can run your first calculation immediately.

## Architecture

MCT2 runs 5 Docker containers:

| Service | Role | Port |
|---------|------|------|
| **frontend** | React web UI | 3001 |
| **backend** | FastAPI orchestrator | 8000 |
| **db** | PostgreSQL (job tracking, results) | 5432 (internal) |
| **hapi-fhir-cdr** | Default clinical data repository | 8080 (internal) |
| **hapi-fhir-measure** | Measure calculation engine ($evaluate-measure) | 8080 (internal) |

## Requirements

- Docker Engine 24+ and Docker Compose v2+
- 16 GB RAM recommended (8 GB minimum)
- 4 CPU cores recommended
- 20 GB disk

## Usage

1. **Measures** — View loaded measures or upload new FHIR Measure bundles
2. **Jobs** — Create a calculation job: select a measure, set the measurement period, optionally filter by FHIR Group, click Calculate
3. **Results** — Inspect aggregate population summaries and drill into individual patient results
4. **Validation** — Upload a FHIR test bundle with expected population results; MCT2 runs the measure and compares actual vs. expected populations, reporting pass/fail per patient
5. **Settings** — Configure your organization's clinical data repository (CDR) connection

## Validation Pipeline

MCT2 includes a validation workflow for verifying measure logic against known test cases:

1. Upload a FHIR Bundle containing test patients and a `Parameters` resource that declares expected population membership (`initialPopulation`, `denominator`, `numerator`, etc.)
2. MCT2 runs `$evaluate-measure` against the test patients and compares results to the expected values
3. Results are displayed per patient with pass/fail status and any discrepancies highlighted

This is useful for measure developers and quality teams who need to confirm that a newly loaded measure produces correct output before running it against production data.

## FHIR Group-Based Patient Filtering

When creating a calculation job, you can optionally select a FHIR Group resource from the connected CDR. When a Group is selected, MCT2 fetches only the patients in that Group rather than all patients in the CDR. Use this to scope calculations to a specific panel, care team, or cohort.

## Connecting Your CDR

By default, MCT2 uses a bundled CDR with test data. To connect to your organization's FHIR server:

1. Go to Settings
2. Enter your CDR URL
3. Select auth type (None, Basic Auth, or Bearer Token)
4. Click "Test Connection" to verify
5. Save

## Development

```bash
# Unit tests
cd backend && python -m pytest tests/ --ignore=tests/integration -v

# Unit tests with coverage (70% floor enforced by CI)
cd backend && python -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing

# Lint
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
```

See [docs/testing.md](docs/testing.md) for the full testing strategy, integration test setup, and golden file test patterns.

## License

Apache 2.0
