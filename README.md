# Lenny — Measure Calculation Tool

A free, open-source utility for calculating FHIR-based digital quality measures (dQMs). Lenny sits between a clinical data repository and a measure calculation engine, orchestrating the evaluation workflow so quality improvement staff can compute measures without vendor dependency.

## Quick Start

```bash
cp .env.example .env
docker compose up
```

Open http://localhost:3001. The 7 active connectathon measures and their test patients are pre-loaded via the prebaked HAPI images — ready to run calculations immediately.

> **Configuring your own CDR?** See [Connecting Your CDR](#connecting-your-cdr) for the one extra setup step (set `CDR_FERNET_KEY` so saved credentials encrypt at rest).

> **Want vanilla upstream HAPI instead?** Comment out `HAPI_CDR_IMAGE`, `HAPI_MEASURE_IMAGE`, and `COMPOSE_FILE` in `.env`. The seed loader will populate connectathon data at startup (~10–15 min first run).

## Architecture

Lenny runs 5 Docker containers:

| Service | Role | Port |
|---------|------|------|
| **frontend** | React web UI | 3001 |
| **backend** | FastAPI orchestrator | 8000 |
| **db** | PostgreSQL (job tracking, results) | 5432 (internal) |
| **hapi-fhir-cdr** | Default clinical data repository | 8080 (internal) |
| **hapi-fhir-measure** | Measure calculation engine ($evaluate-measure) | 8080 (internal) |

Local dev (per `.env.example`) and CI use `docker-compose.prebaked.yml` (HAPI images with QI-Core / US-Core IGs and connectathon bundles baked in for fast cold-start). Production runs vanilla `hapiproject/hapi:v8.8.0-1` with bundles loaded by the `seed` service into a persistent H2 volume. See [docs/architecture.md](docs/architecture.md) for the full service map, data flow, HAPI configuration, and environment variables.

## Requirements

- Docker Engine 24+ and Docker Compose v2+
- 16 GB RAM recommended (8 GB minimum)
- 4 CPU cores recommended
- 20 GB disk

## Usage

1. **Measures** — View loaded measures or upload new FHIR Measure bundles
2. **Jobs** — Create a calculation job: select a measure, set the measurement period, optionally filter by FHIR Group, click Calculate
3. **Results** — Inspect aggregate population summaries and drill into individual patient results
4. **Validation** — Upload a FHIR test bundle with expected population results; Lenny runs the measure and compares actual vs. expected populations, reporting pass/fail per patient *(hidden by default; enable via Settings → Admin → Features → Validation toggle)*
5. **Settings** — Manage clinical data repository (CDR) and measure calculation server (MCS) connections. Add multiple of each, switch the active one, test connectivity per connection (with a deeper "Verify with sample evaluate" probe on the active MCS). The active CDR provides patient/clinical data; the active MCS runs `$evaluate-measure`. Useful at connectathons for swapping between your own server and a reference server.

## Validation Pipeline

> **Note:** The Validation tab is hidden by default. Enable it via Settings → Admin → Features → Validation toggle.

Lenny includes a validation workflow for verifying measure logic against known test cases:

1. Upload a FHIR Bundle containing test patients and a `Parameters` resource that declares expected population membership (`initialPopulation`, `denominator`, `numerator`, etc.)
2. Lenny runs `$evaluate-measure` against the test patients and compares results to the expected values
3. Results are displayed per patient with pass/fail status and any discrepancies highlighted

This is useful for measure developers and quality teams who need to confirm that a newly loaded measure produces correct output before running it against production data.

## FHIR Group-Based Patient Filtering

When creating a calculation job, you can optionally select a FHIR Group resource from the connected CDR. When a Group is selected, Lenny fetches only the patients in that Group rather than all patients in the CDR. Use this to scope calculations to a specific panel, care team, or cohort.

## Connecting Your CDR

By default, Lenny uses a bundled CDR with test data. To connect to your organization's FHIR server:

1. Go to Settings
2. Enter your CDR URL
3. Select auth type (None, Basic Auth, or Bearer Token)
4. Click "Test Connection" to verify
5. Save

Auth credentials (passwords, bearer tokens) are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256). **Set `CDR_FERNET_KEY` in `.env` before saving a custom CDR — generate one with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.** See `.env.example` and `docs/architecture.md` for the production SSM/Docker-secrets pipeline.

## Connectathon Measures

Lenny ships with the 7 active measures targeted for the MADiE May 2026 Connectathon, pre-loaded into the bundled CDR for immediate testing. Per-measure pass/fail status, removed-bundle rationale (5 measures removed 2026-05-06, issue #278), and resource baselines (Patient: 319, Measure: 8 [7 connectathon + 1 seed], Library: 18, ValueSet: 78) are tracked in [docs/connectathon-measures-status.md](docs/connectathon-measures-status.md). The nightly **Connectathon Measures** GitHub Actions workflow runs the source-of-truth suite (golden + connectathon-measures + full-workflow tests) against pre-baked HAPI images and surfaces drift.

## Development

The backend pins to **Python 3.10** to match CI. Use a project-local venv:

```bash
# One-time setup
cd backend
python3.10 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-test.txt

# Day-to-day
source backend/.venv/bin/activate

# Unit tests
cd backend && python -m pytest tests/ --ignore=tests/integration -v

# Unit tests with coverage (70% floor enforced by CI)
cd backend && python -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing

# Integration tests (CI-equivalent — same ignore flags pr-checks.yml uses)
./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py

# Lint
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
```

See [docs/testing.md](docs/testing.md) for the full testing strategy, the three-job nightly workflow (Bundle Loader Test, Connectathon Eval, Full Workflow), and golden file test patterns. See [CLAUDE.md](CLAUDE.md) for the mandatory pre-push checklist and the HAPI async-indexing troubleshooting reference.

## License

Apache 2.0
