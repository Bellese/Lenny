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
2. **Jobs** — Create a calculation job: select a measure, set the measurement period, click Calculate
3. **Results** — Inspect aggregate population summaries and drill into individual patient results
4. **Settings** — Configure your organization's clinical data repository (CDR) connection

## Connecting Your CDR

By default, MCT2 uses a bundled CDR with test data. To connect to your organization's FHIR server:

1. Go to Settings
2. Enter your CDR URL
3. Select auth type (None, Basic Auth, or Bearer Token)
4. Click "Test Connection" to verify
5. Save

## License

Apache 2.0
