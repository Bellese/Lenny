# Project Launch Pad — A dQM Starting Point

(now known as Lenny)

## Executive Summary

### The Problem

CMS is mandating the transition to FHIR-based digital quality measures (dQMs) across all quality reporting programs. Healthcare organizations — particularly Accountable Care Organizations (ACOs) participating in the Medicare Shared Savings Program (MSSP) — must calculate these measures to meet compliance requirements and drive quality improvement.

Most organizations lack the technical infrastructure to perform these calculations in-house. As a result, they pay vendors between $300 and $4,000 per provider annually for measure computation services. For a typical ACO with 500 to 4,000 providers, this translates to $1-3 million in recurring annual costs. Five ACOs independently confirmed this cost burden during our discovery process, with one organization reporting $250,000 in fees paid to a single vendor for FHIR-based measure computation alone.

The open-source landscape offers low-level CQL execution engines and one prototype tool (the original MCT from the Clinical Quality Framework), but nothing production-ready or accessible enough for non-technical quality improvement staff to operate independently.

### The Solution

Lenny is a free, open-source utility that enables healthcare organizations to calculate digital quality measures without vendor dependency. It sits between an organization's clinical data repository (any FHIR-compliant server) and a measure calculation engine, orchestrating the entire evaluation workflow through a simple web interface designed for quality improvement staff — not software engineers.

Lenny ships ready to use: a single `docker compose up` command starts the application with a pre-loaded demo measure and synthetic patient data, allowing users to run their first calculation within minutes of installation. When ready for production, staff configure a connection to their organization's real clinical data repository through the web UI and begin calculating measures against live patient data.

Key capabilities include uploading FHIR Measure bundles, triggering measure calculations with live progress tracking, inspecting aggregate population results (initial population, numerator, denominator, exclusions, performance rate), and drilling into individual patient results to understand why each patient was included or excluded — with clinical data translated into plain language rather than raw FHIR JSON.

### Architecture

Lenny is deployed as five Docker containers managed by a single `docker compose` command:

- **Web Interface** (React) — Administration, job management, and result inspection  
- **Backend API** (Python/FastAPI) — Orchestrates the measure calculation workflow, manages jobs, and serves results  
- **Database** (PostgreSQL) — Tracks calculation jobs, stores configuration, and caches measure results  
- **Clinical Data Repository** (HAPI FHIR) — Bundled default data source with synthetic patients; replaced by the organization's own FHIR server in production  
- **Measure Calculation Engine** (HAPI FHIR) — Hosts FHIR Measure resources and CQL libraries, performs the actual measure evaluation via the $evaluate-measure operation

The backend uses a pluggable data acquisition architecture, enabling future support for multiple strategies to retrieve patient data (batch queries, FHIR Bulk Data Export, custom approaches) as organizational needs evolve.

### Value Proposition

|  | Current State | With Lenny |
| :---- | :---- | :---- |
| **Annual cost** | $1-3M in vendor fees | $0 (open source) |
| **Setup time** | Weeks of vendor onboarding | Minutes (single command) |
| **Vendor dependency** | Locked into proprietary platforms | Full organizational control |
| **Transparency** | Black-box calculation | Full patient-level result inspection |
| **Flexibility** | Vendor's supported measures only | Any FHIR-based dQM |

Lenny eliminates the most expensive component of the vendor relationship — the actual measure computation and result inspection — while complementing existing investments in data ingestion, EHR integration, and measure submission workflows. It is designed for the organizations that need it most: those just beginning their transition to digital quality measures who face significant financial, technical, and staffing constraints.

**Requirements:** Docker Engine 24+, 16 GB RAM recommended, 4 CPU cores, 20 GB disk.

**Repository:** [https://github.com/Bellese/mct2](https://github.com/Bellese/mct2)