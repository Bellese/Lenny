# Changelog

All notable changes to this project will be documented in this file.

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
