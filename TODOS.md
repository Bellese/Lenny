# TODOS — MCT2

## Future Data Acquisition: Bulk Data Export Strategy
**What:** Add `BulkExportStrategy` as a second data acquisition option using FHIR Bulk Data $export.
**Why:** Batch queries work for ~4,000 patients but may not scale to larger populations. $export returns NDJSON files asynchronously and is purpose-built for bulk extraction — 10-100x faster for large populations with less CDR load.
**Cons:** Not all CDRs support $export yet. Requires async polling for export completion.
**Context:** The `DataAcquisitionStrategy` ABC is already in place (v1 ships with `BatchQueryStrategy`). This is a clean plug-in addition — implement the interface, register alongside BatchQueryStrategy, expose in settings UI.
**Depends on:** v1 strategy pattern in `fhir_client.py`.

## SMART on FHIR / OAuth2 Authentication
**What:** Add SMART on FHIR backend services auth flow for CDR connections.
**Why:** Many production FHIR servers require OAuth2. Basic Auth / static Bearer tokens (v1) won't work at organizations with mature security postures. This unlocks connectivity to the majority of production FHIR servers.
**Cons:** SMART backend services flow requires client credentials, JWT signing, token refresh — moderate complexity.
**Context:** The settings UI already has auth configuration (CDR URL + auth type). This extends it with OAuth2 client credentials fields and automatic token management. Reference: SMART Backend Services spec (hl7.org/fhir/smart-app-launch/backend-services.html).
**Depends on:** v1 Basic Auth / Bearer token auth working.

## MeasureReport Submission to CMS
**What:** Add a 'Submit' action that POSTs MeasureReports to a configurable receiving endpoint (CMS, data aggregator, quality reporting portal).
**Why:** Closes the loop from 'calculate measure' to 'report to CMS.' Without it, users must manually export and upload results.
**Cons:** Receiving endpoints vary by program (MSSP, MIPS, etc.). Auth and format requirements differ across programs.
**Context:** MeasureReports are already stored in PostgreSQL. Submission is a POST to an external endpoint with program-specific configuration. May want to support DEQM (Data Exchange for Quality Measures) IG profiles.
**Depends on:** v1 result inspection working.

## SSRF on /settings/test-connection
**What:** Add URL validation and private-IP blocking to `POST /settings/test-connection`.
**Why:** The endpoint accepts any `cdr_url` and issues an outbound HTTP request without validating the target host. An attacker (or misconfigured client) can use it to probe EC2 metadata (`http://169.254.169.254/latest/meta-data/`) or internal Docker services.
**Fix:** (1) Require `https://` scheme. (2) Resolve the hostname and reject RFC-1918 and link-local ranges before connecting. httpx does not block private IPs by default.
**Priority:** P1 — public EC2 instance, no auth. Ship before connectathon.
**Context:** Found during pre-landing review for #10. The current fix (sanitize_error on the response) improves output hygiene but does not block the outbound request itself.

## CI/CD Validation Pipeline
**What:** Add CLI/API command that runs validation and outputs structured pass/fail report for CI/CD.
**Why:** Enables automated regression detection before deploy. Required for formal CMS certification workflow.
**Pros:** Catches measure calculation regressions pre-deploy. Reuses the validation service.
**Cons:** Requires CLI harness or standalone script. Low priority until certification is imminent.
**Context:** The validation service (`services/validation.py`) and ExpectedResult/ValidationRun models provide all the backend logic. This TODO adds a CLI entry point or API endpoint that returns structured JSON/HTML for CI pipelines.
**Depends on:** ~~Validation dashboard feature (`feature/expected-results-compare` branch).~~ **Now unblocked.**

## Orchestrator Group Filtering Unit Test
**What:** Add a unit test to `tests/test_services_orchestrator.py` verifying that `run_job` with `group_id` set calls `get_group_members()` instead of `gather_patients()`.
**Why:** Deferred from plan `iridescent-kindling-whistle.md` during ship. Integration tests cover this path (12/12 pass) but explicit unit isolation is good practice.
**Priority:** P3 — low urgency, integration tests already cover it.
