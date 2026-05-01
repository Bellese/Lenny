# Changelog

All notable changes to this project will be documented in this file.

## [0.0.12.0] - 2026-05-01

### Added
- **Auto-select patient group when measure is chosen (issue #228)** — Choosing a measure in the "Start Calculation" modal now automatically pre-fills the Patient Group field with the group whose CMS number matches the selected measure (e.g. selecting `CMS122FHIRDiabetes...` auto-selects `CMS122-cohort`). Uses `extractCmsId` as the join key. Manual group selection still works; the field clears if no match is found.
- **FHIR Groups synthesized for all 12 connectathon measures (issue #232)** — Seeding the local stack now produces one FHIR `Group` resource per measure bundle on the CDR, so the Patient Group dropdown is populated on a fresh install without manual setup. CMS1017's curated `artifact-testArtifact` extension is preserved; the other 11 groups are synthesized from each bundle's Patient members.

### Fixed
- **Population counts no longer show 0 when running jobs against a patient group** — The reindex probe that gates CQL evaluation was inadvertently polling a pre-baked phantom patient from the HAPI measure engine Docker image instead of the patients just pushed to the engine. The probe now targets only patients with Encounters from the current batch, so CQL sees correctly indexed data. Batches with no Encounter-bearing patients fall back to a timed sleep rather than probing the wrong patient.

## [0.0.11.0] - 2026-04-29

### Fixed
- **Measure bundle upload no longer fails with HTTP 422 (HAPI-0902)** — Uploading a QI-Core 6 measure bundle now succeeds even when the measure engine already holds a ValueSet under a different resource ID than what the bundle contains. The upload service now queries HAPI by canonical URL before posting and rewrites conflicting IDs in-place, turning a failed create into a clean update.
- **Backend log no longer raises `KeyError` on measure upload** — Using `filename` as a structured log key collided with Python's reserved `LogRecord.filename` field. Fixed by switching to a format-string log call.

## [0.0.10.0] - 2026-04-28

### Added
- **Fernet encryption for CDR auth credentials at rest (issue #219)** — Basic passwords, Bearer tokens, and SMART `client_secret` values are now encrypted in `cdr_configs.auth_credentials` using Fernet (AES-128-CBC + HMAC-SHA256). Each value is stored as a `{"v": 1, "ct": "<token>"}` envelope; the TypeDecorator decrypts transparently on read so no call-site changes are required.
- **`credential_crypto` module** — `EncryptedJSON` SQLAlchemy `TypeDecorator`, lazy Fernet singleton loader (reads `/run/secrets/cdr_fernet_key` first, falls back to `CDR_FERNET_KEY` env var which is immediately `os.environ.pop`'d to prevent subprocess leakage), `self_check()` startup probe.
- **`CDR_FERNET_KEY` in SSM/Docker-secrets pipeline** — `bootstrap-aws.sh` provisions `/leonard/prod/CDR_FERNET_KEY` as a SecureString (generated via Python, never visible as a process arg). `fetch-prod-secrets.sh` fetches it alongside `POSTGRES_PASSWORD`. `deploy-prod.sh` writes it to `/run/leonard/CDR_FERNET_KEY` (mode 0600). `docker-compose.prod.yml` mounts it as a Docker secret. `.env.example` documents the local dev generation command.
- **`cdr_id` FK on `Job`** — replaces the per-job `cdr_auth_credentials` plaintext snapshot. The orchestrator now reads live credentials from `cdr_configs` via the FK, so mid-job CDR credential rotations propagate naturally.
- **Audit logging on CDR connection changes** — `POST`/`PUT`/`DELETE` to `/connections` emit a structured `INFO` log with `event: cdr_credentials_changed`, `action`, `cdr_id`, and `cdr_name`. Credential values are never logged.
- **409 guard on CDR connection delete** — `DELETE /connections/{id}` returns 409 if any `queued` or `running` jobs reference the connection, preventing orphaned in-flight jobs.

### Changed
- **Inline migration** — startup adds `cdr_id INTEGER REFERENCES cdr_configs(id) ON DELETE SET NULL` to `jobs`, backfills from matching `cdr_url + name`, then drops `cdr_auth_credentials`. Guarded by `IF NOT EXISTS`/`IF EXISTS` — idempotent on concurrent restarts.
- **Credential encryption backfill** — on Postgres, any `cdr_configs.auth_credentials` rows lacking the `{v, ct}` envelope are re-saved through the TypeDecorator at startup (uses `flag_modified` to force SQLAlchemy dirty-check).
- **Orchestrator live-lookup** — `_get_cdr_auth_headers` now joins `cdr_configs` via `job.cdr_id` instead of reading the stale job snapshot. Raises `RuntimeError` with a clear message if `cdr_id` is NULL or the CDR row no longer exists.

### Fixed
- **Plaintext CDR credentials at rest** — closes the HIGH finding from the 2026-04-28 security audit. Credentials are no longer recoverable from a DB backup, read replica, or ops console query.

## [0.0.9.0] - 2026-04-28

### Added
- **Structured FHIR error surfacing (PR-2 — $gather + bundle upload + MCS OperationOutcome, issues #75 #76)** — three previously-silent failure classes now surface actionable context to the user.
- **MCS `OperationOutcome` preservation (#76)** — when the Measure Calculation Server returns a non-2xx response or a 200-with-`OperationOutcome` body, `evaluate_measure` raises `FhirOperationError` carrying the full parsed OO. The orchestrator persists the server-returned OO (not a synthetic one) in `measure_report` via a FHIR `Extension`, along with `error_details` (status_code, url, latency_ms, raw_outcome). Per-patient error rows show the MCS OO in the `PatientDetail` drawer alongside existing results.
- **Partial CDR gather surfaced (#75 AT-2)** — when `DataRequirementsStrategy` fails to fetch one or more resource types from the CDR but succeeds on others, `gather_patient_data` now returns a `GatherResult` with `failed_types: list[FailedResourceFetch]`. The orchestrator continues to evaluate with available data (does NOT skip the patient), annotates the `MeasureResult` with `error_phase="gather_partial"` and `error_details` listing failed/succeeded types. The ResultsPage shows an amber **Partial data** badge for these rows so attendees know results may be incomplete.
- **Full CDR gather failure surfaced (#75 AT-1)** — when gather/push raises an exception (CDR unreachable, 401, etc.), the patient is recorded with `error_phase="gather"`, `error_details` with upstream status_code/url/latency, and is skipped in the evaluate phase.
- **Bundle upload partial failures surfaced (#75 AT-3/AT-4)** — `push_resources` now parses the HAPI batch-response Bundle and returns a `BundleUploadResult` with per-entry success/failure breakdown. A 200-OK-with-OperationOutcome body (transaction-level HAPI rejection) is treated as a failure. The ValidationPage upload result shows a per-entry failure list. `BundleUpload.error_details JSONB` column added.
- **`ValidationResult.error_details JSONB` column** — `evaluate_and_compare` catches `FhirOperationError` and persists the MCS OO in `error_details`.
- **New dataclasses** — `GatherResult`, `FailedResourceFetch`, `BundleUploadResult`, `BundleEntryResult` in `fhir_client.py`.

### Changed
- **`PatientDetail` drawer** — when `measure_report.resourceType === "OperationOutcome"`, renders an `OperationOutcomeView` above the raw JSON toggle. Closes #76.
- **ResultsPage patient table** — tri-state: `error` rows show a red **Error** badge with error phase; `gather_partial` rows show an amber **Partial data** badge.
- **ValidationPage upload result** — per-entry failure list when `error_details.failed_entries` is non-empty.
- **`_error_measure_report`** — embeds MCS-returned OO via FHIR Extension instead of synthesizing from `str(exc)`. Deep-copies upstream dict to prevent cross-patient mutation. `populations["error_message"]` still written for back-compat.

### Fixed
- **MCS OO discarded on 4xx/5xx** — `evaluate_measure` now parses and preserves the server-returned OperationOutcome. Closes #76.
- **Partial gather silent pass** — patients with partial CDR data now annotated with `gather_partial` phase instead of evaluating against missing resources. Closes #75.
- **HAPI 200-with-OperationOutcome body** — transaction-level HAPI bundle rejection returning HTTP 200 with OO body now raises `FhirOperationError`.

## [0.0.8.1] - 2026-04-27

### Added
- **Structured FHIR error surfacing (PR-1 — connection/auth, issue #74)** — when a CDR connection test fails, the backend now returns a structured `OperationOutcome` with `error_details` containing the HTTP status code, probed URL, latency, and a user-facing hint (e.g. "Authentication failed. Re-check your bearer token or username/password."). Network errors (unreachable host, TLS/SSL, timeout) include network-layer hints. SSRF attempts return HTTP 400 instead of 502.
- **`fhir_errors` shared module** (`backend/app/services/fhir_errors.py`) — `FhirOperationOutcome`, `FhirOperationError`, `build_error_envelope`, `redact_outcome`, `sanitize_url`, and `HINT_BY_STATUS` hint map. Foundation for PR-2 ($gather and MCS error surfacing).
- **DB schema extensions** — `measure_results.error_details JSONB`, `measure_results.error_phase VARCHAR(32)`, and a unique index on `(job_id, patient_id)` (with dedup pre-flight) added via `_run_schema_migrations`. Populated by PR-2; nullable additive in this PR.
- **`parseFhirError` helper** (`frontend/src/api/fhirError.js`) — parses `detail.issue[]` and `detail.error_details` from API error bodies into `{issues, errorDetails}`. Now consumed by `client.js`; ready for `OperationOutcomeView` in the next PR.
- **Integration tests** (`backend/tests/integration/test_connection_errors.py`) — bearer-token 401, unreachable-URL 502, and success `response_time_ms` scenarios against a live HAPI CDR.

### Changed
- **`/settings/test-connection` response** — success responses now include `response_time_ms` (int) and a sanitized `url` field. Error responses carry the full `error_details` envelope. Closes #74.

## [0.0.8.0] - 2026-04-27

### Changed
- **Bundle-loader CI test now runs against a 2-bundle subset** — `bundle-loader-test` exercises `load_connectathon_bundles()` against CMS122 and CMS124 instead of all 12 connectathon bundles, cutting vanilla-HAPI CI wall-clock from >90 min toward ≤60 min. Structural tests (file presence, SHA256) still cover all 12 bundles. All 12 continue to load nightly via the bake job. Closes #202.

## [0.0.7.0] - 2026-04-27

### Changed
- **Measure IDs now display as short CMS numbers** — the Measures table, job creation form, patient group dropdown, and Results page all show `CMS122` instead of the full raw HAPI ID (`CMS122FHIRDiabetesAssessGT9Pct`). Handles both `CMS{n}FHIR...` and `CMSFHIR{n}...` ID patterns.
- **"FHIR" suffix stripped from measure names** — display names like `"Breast Cancer ScreeningFHIR"` now show as `"Breast Cancer Screening"` across all pages.
- **Job creation dropdown shows formatted labels** — measure options now display as `CMS71 — Anticoagulation Therapy Prescribed at Discharge` instead of raw FHIR IDs. Patient group options show the same format with patient count appended.

## [0.0.6.8] - 2026-04-25

### Added
- **Year picker for reporting period** — the job creation form now defaults to the current calendar year (Jan 1 – Dec 31) with a dropdown showing the last 5 years, so you can say "2026" instead of typing both dates. A "Enter custom dates" toggle is available for non-calendar-year ranges; clicking "← Back to year select" restores the year dropdown. Closes #79.
- **Frontend component tests** — React Testing Library test suite for `PeriodPicker` covering year dropdown, custom date toggle, and `onChange` wiring (10 tests).

## [0.0.7.7] - 2026-04-24

### Fixed
- **Validation runs no longer fail with 409 CONFLICT on first evaluation** — concurrent `$evaluate-measure` calls to HAPI now include a warmup burst that serially evaluates one patient per measure before the concurrent batch. First concurrent batch against a fresh measure engine triggers a race during SearchParameter indexing; some concurrent requests hit 409 CONFLICT affecting ~20-30% of patients. Warmup avoids the race by completing indexing in single-threaded context before concurrent calls start. First run after bundle upload now succeeds; retry workarounds no longer necessary. Fixes #156.

## [0.0.6.7] - 2026-04-22

### Fixed
- **Validation runs no longer fail for EXM FHIR4 measures** — `_resolve_measure_id` now handles relative FHIR references (`Measure/{id}`) in addition to canonical URLs. EXM test bundles store `MeasureReport.measure` as a relative reference, which previously couldn't be resolved because HAPI was queried via `?url=` (which only matches canonical URLs). Fixes #108.

### Removed
- **Obsolete EXM FHIR4 measure bundles** — removed 9 old bundles (EXM104, EXM105, EXM108, EXM124, EXM125, EXM130, EXM165, EXM506, EXM529) that were being auto-loaded on backend startup, causing duplicate measures in production. Kept older CMS placeholder versions (v0.3-v0.5) pending QI-Core 6 dQM v1.0.000 bundles from MADiE (issue #115).

## [0.0.6.6] - 2026-04-22

### Fixed
- **ValueSet compose patch now applies on production bundle upload** (`triage_test_bundle`), not only in test fixtures. MADiE bundles with ValueSets that have sub-ValueSet compose references or bare CodeSystem includes (no explicit codes) are now rewritten to use direct code lists from `expansion.contains` before being POSTed to the HAPI measure engine — preventing all-zero CQL evaluation results. (#99)

## [0.0.6.5] - 2026-04-22

### Changed
- **Measures page UI improvements**: measure display names no longer show the trailing "FHIR" suffix for cleaner presentation, and a new "Measure ID" column displays the measure identifier (e.g., "CMS122") for easier identification and reference.

### Security
- **Upload endpoint hardening** — two unauthenticated upload endpoints now protected against abuse:
  - `POST /measures/upload`: 100 MB size cap (413 OperationOutcome); 10 req/min per-IP rate limit (429 OperationOutcome)
  - `POST /validation/upload-bundle`: same size cap and rate limit; filename sanitization strips null bytes, control characters, and path-traversal sequences; filenames truncated to 255 chars with extension preserved
  - `Caddyfile`: `request_body { max_size 100MB }` on the API vhost for belt-and-suspenders OOM prevention; `header_up X-Forwarded-For {remote_host}` prevents clients from spoofing the rate-limit key
  - `backend/app/limiter.py`: shared slowapi `Limiter` with `X-Forwarded-For`-aware key function (Caddy proxy architecture)
  - `backend/app/config.py`: `MAX_UPLOAD_SIZE` constant shared across both endpoints

## [0.0.6.3] - 2026-04-21

### Added
- Automatic lazy measure loading: validation runs now detect missing measures on the HAPI engine and attempt to reload them from seed bundles automatically before failing. If manual recovery is needed, a recovery script is available: `./scripts/reload-validation-bundles.sh`.
- Comprehensive validation failure recovery guide (`docs/validation-fixes.md`) documenting three complementary recovery strategies for when expected results exist but measure resources are lost.

### Fixed
- **Validation error messages** now provide clear, actionable guidance instead of raw HAPI errors. When the measure engine is unavailable or measures are missing, users see a user-friendly message directing them to the Validation page or manual recovery steps.
- **Bundle upload error handling** — when measures fail to push to HAPI during bundle upload, the system returns a descriptive error instead of a generic failure, helping users diagnose connection issues or measure engine availability problems.

## [0.0.6.2] - 2026-04-21

### Changed
- `docs/architecture.md` and `docs/testing.md` refreshed to match the current repo: HAPI FHIR bumped to v8.8.0-1, `backend/app/dependencies.py` and `ConnectionModal.js` added to the structure maps, integration test file list expanded, PR gate corrected to reflect that `test_connectathon_measures.py` and `test_full_workflow.py` run nightly (not on PRs), and `STRICT_STU6=0` noted as the current CI default during rollout.

### Removed
- `docs/validation-findings-2026-03-27.md` — superseded by `docs/connectathon-measures-status.md`.
- `docs/workflow-proposal.md` — early draft, superseded by `docs/workflow.md`.

## [0.0.6.1] - 2026-04-21

### Added
- `docs/connectathon-measures-status.md` — full status reference for all 12 MADiE connectathon measures, including per-measure pass/fail/skip counts, golden test exclusion rationale for CMS1017/CMS1218, infrastructure bugs fixed in sessions 11–13, remaining failure classes, and next steps.

## [0.0.6.0] - 2026-04-20

### Fixed
- **Golden measure tests now pass reliably against HAPI v8.6.0** — resolved two test failures
  affecting CMS122 ("Betty-Bertha-*" patients showing `denominator-exclusion=0`) and all EXM
  DBCG connectathon bundles. Root causes: HAPI ignores pre-computed ValueSet expansions and
  always re-expands via compose; patient data must be loaded on the measure server (not just
  the CDR) for `$evaluate-measure` to resolve it. Fixes include ValueSet compose patching,
  dual-server patient loading, and post-load `$reindex` with indexed-resource polling.
- **Duplicate ValueSet guard** — before loading each golden bundle's ValueSets, the test
  fixture checks which canonical URLs are already in HAPI and skips any duplicates. Prevents
  "Multiple ValueSets resolved" CQL evaluation errors when bundles share ValueSet URLs.
- **All-bundle reindex wait** — the fixture now collects a probe encounter from every bundle
  and waits until all probes are indexed before running tests, preventing a race where later
  bundles' encounters were not indexed when tests started.
- EXM bundles (DBCG connectathon era, CQL 1.3 syntax) marked `xfail` — HAPI v8.6.0's CQL
  engine no longer supports the `timezone` keyword and old `DateTime()` signatures used in
  these pre-2021 bundles.
- Improved test robustness: `$reindex` failures now emit warnings, HAPI response parsing is
  guarded against non-JSON bodies, nested `expansion.contains` entries are fully flattened,
  and ELM decoding errors are caught rather than propagating as unhandled exceptions.

## [0.0.5.0] - 2026-04-19

### Added
- QI-Core STU6 (v6.0.0) support: HAPI FHIR now installs the QI-Core, US Core 6.1.0, and CQL
  implementation guides on startup via `hapi.fhir.implementationguides.*` env vars in both
  production and test Docker Compose configs.
- 11 of 12 connectathon bundles replaced with QICore6 versions from the cqframework public
  repository (CMS2, CMS71, CMS122, CMS124, CMS125, CMS130, CMS165, CMS506 added; CMS816,
  CMS1017, CMS1218 retained; CMS529 pending MADiE access).
- `seed/connectathon-bundles/manifest.json` with SHA-256 pins, canonical URLs, expected
  test-case counts, 2026 measurement period, and per-measure strictness flag.
- Integration smoke test (`tests/integration/test_smart_load.py`): manifest-driven bundle
  load verification including CDR CapabilityStatement + QI-Core IG assertions.
- Per-test-case integration test (`tests/integration/test_connectathon_measures.py`):
  parametrized by MADiE `ExpectedResult` row; uses `STRICT_STU6` env var for CI gating.
- Connectathon rehearsal script (`scripts/connectathon-rehearsal.sh`): cold-start demo
  workflow with health polling, measure inventory, and a 12-row pass/fail table.

### Fixed
- `wipe_patient_data` now includes `Medication` and `Task` resource types found in QICore6
  connectathon bundles, preventing leftover data from contaminating subsequent evaluations.
- `_KNOWN_CLINICAL_TYPES` in `validation.py` updated to include `Medication` and `Task`,
  eliminating spurious unknown-type log warnings when processing QICore6 bundles.
- Rehearsal script `jq` calls use `-r` flag to prevent quoted string IDs in URLs.
- `bundle_loader.py` now skips `manifest.json` when globbing bundle files, preventing it
  from being loaded as a FHIR bundle.
- Measure push switched from transaction to batch bundle to avoid HAPI-2001 (`Patient ref
  unknown`) when clinical subjects are absent from the measure engine.
- `docker-compose.test.yml` `server_address` corrected from Docker-internal hostname to
  `localhost` so HAPI pagination links resolve from the CI host.
- CMS1218 `expected_test_cases` corrected from 75 to 69 (6 duplicate patient refs in the
  MADiE bundle produce 69 unique DB rows via `ON CONFLICT DO UPDATE`).
- `test_cdr_qicore_implementation_guide_resource` marked skip: HAPI loads the QI-Core IG
  for profile validation but does not persist it as a queryable FHIR resource.
- Added Lucene `io.refresh_interval=100ms` and `reuse_cached_search_results_millis=0` to
  the measure engine test config to eliminate indexing race conditions in integration tests.

### Changed
- Test HAPI FHIR bumped from `v7.4.0` to `v8.6.0-1` in `docker-compose.test.yml`, aligning
  with production and enabling QI-Core STU6 evaluation.
- `STRICT_STU6=0` soft default added to CI workflow for one-week rollout; flip to `1` once
  all 12 connectathon measures pass evaluation.
- Per-patient `$evaluate-measure` tests (`test_connectathon_measures.py`, ~548 cases,
  15-20 min) moved to a dedicated nightly workflow (`connectathon-measures.yml`) with a
  60-minute timeout and manual `STRICT_STU6` override. PR gate now runs in ≤ 20 minutes.

## [0.0.4.0] - 2026-04-19

### Fixed
- Bundle upload now forwards auth headers (Basic or Bearer) when pushing clinical data
  to an external CDR. `push_resources` now accepts an optional `auth_headers` parameter
  that is merged into the POST request, matching the auth behavior already present in
  `run_validation`. External CDRs requiring authentication would previously receive a 401.

## [0.0.3.0] - 2026-04-19

### Added
- **$data-requirements strategy**: Lenny now uses the DEQM-compliant `$data-requirements`
  endpoint to fetch only the clinical resources a measure actually needs, replacing the
  broad `$everything` call. Falls back to `$everything` automatically if the measure engine
  does not support `$data-requirements` or returns an empty list. codeFilter.valueSet entries
  are translated to `code:in={valueSetUrl}` CDR search parameters, and per-resource-type
  failures are isolated so one failing type does not abort all others.
- **Startup bundle loader**: 12 connectathon bundles (9 DBCG FHIR4 + 3 QICore 2025 Hospital Harm)
  load automatically on backend startup. Measures, patients, and expected results are available
  immediately without manual upload. Clinical data now always loads to the active CDR regardless
  of whether it is the default or an external CDR.
- **Comparison view on Results page**: A new comparison table shows each patient's actual
  vs. expected population results side-by-side, including match/mismatch indicators.
  Requires expected results from loaded test bundles.
- **`GET /jobs/{id}/comparison` endpoint**: Returns per-patient comparison data against
  stored expected results for a job.
- **Golden integration test fixtures**: 12 end-to-end regression fixtures under
  `tests/integration/golden/` (EXM104–529, CMS816/1017/1218) assert that each seed bundle
  evaluates to correct population counts after a full HAPI startup.

### Changed
- Orchestrator uses `DataRequirementsStrategy` by default instead of `BatchQueryStrategy`.
- `_fetch_by_requirements` now follows FHIR pagination (`link.next`) for resource type queries,
  preventing silent truncation at 100 resources for patients with large datasets.

### Fixed
- HAPI FHIR 8.6 measure engine now uses Hibernate Search Lucene backend, fixing ValueSet
  expansion failures that caused all-zero population counts.
- Golden integration tests use FHIR batch bundles instead of transaction bundles to avoid
  HAPI reference validation failures when test fixtures contain partial resource graphs.

## [0.0.2.2] - 2026-04-10

### Added
- Multi-CDR connection management: full CRUD for CDR connections with support for
  none/basic/bearer/SMART on FHIR auth, read-only flag, and a default Local CDR row.
  CDR credentials are stamped on each job at creation. Closes #6.

## [0.0.2.1] - 2026-04-10

### Fixed
- Bundle upload path collision: two concurrent uploads of the same filename within the
  same second no longer overwrite each other. A `uuid4` hex token is now embedded in
  the saved filename (`{timestamp}-{uuid4}-{basename}`), guaranteeing unique paths
  regardless of upload timing. Closes #63.

## [0.0.2.0] - 2026-04-09

### Added
- CI/CD gate: new `pr-checks.yml` GitHub Actions workflow runs unit tests (70% coverage
  floor), ruff lint, integration tests, and frontend build on every pull request.
- Deploy workflow now enforces the 70% coverage floor via `--cov-fail-under=70`.
- Golden file integration test pattern: drop a `bundle.json` in `tests/integration/golden/`
  and the measure engine evaluates it automatically on every integration run.
- `docs/testing.md` documents the full testing strategy — coverage targets, test layers,
  golden test format, and CI job reference.
- `backend/requirements-test.txt` separates test-only dependencies from runtime so
  production images stay lean.
- `backend/ruff.toml` enforces consistent import ordering and code style across all
  backend Python files.

### Changed
- CLAUDE.md updated with coverage and lint commands and a reference to `docs/testing.md`.

### Fixed
- Ruff auto-corrected import ordering and formatting across all backend `app/` and
  `tests/` files. No logic changes — cosmetic only.

## [0.0.1.1] - 2026-04-09

### Fixed
- CDR status dot in header now correctly shows green on page load when CDR is connected.
  Previously always showed red because `App.js` read `health.cdr_connected` (a field that
  does not exist) instead of `health.cdr.status`.
- CDR status indicator now propagates the API's three-state value (`connected`,
  `disconnected`, `unknown`) instead of collapsing all non-connected states to `disconnected`.
- System Status section on Settings page now refreshes immediately after a successful
  connection test instead of waiting for the next 30-second poll.

## [0.0.1.0] - 2026-04-09

### Security
- Restrict CORS to explicit origins in production via `ALLOWED_ORIGINS` env var
  (`docker-compose.prod.yml` sets it to `https://${CADDY_HOST}` at deploy time)
- `allow_credentials` is now disabled when origins is wildcard, which is invalid
  per the CORS spec and was previously a misconfiguration in local dev
- Startup warning logged when wildcard CORS is active so accidental production
  deployments are visible in logs

### Added
- `ALLOWED_ORIGINS` environment variable in `backend/app/config.py`; defaults to
  `"*"` so `docker compose up` requires no env changes
- `parse_allowed_origins()` helper in `config.py` — shared by `main.py` and tests
- 8 CORS behavior tests in `backend/tests/test_cors.py` covering wildcard, allowed
  origin, rejected origin, preflight, multi-origin list, empty origins, and negative
  cases

### Fixed
- CORS origin parser now strips trailing slashes to prevent silent mismatches
  (e.g. `https://example.com/` vs `https://example.com`)
- `allow_credentials` guard handles empty origin list correctly
