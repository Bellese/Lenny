# Changelog

All notable changes to this project will be documented in this file.

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
