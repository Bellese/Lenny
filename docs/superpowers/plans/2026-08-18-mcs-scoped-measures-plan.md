# Implementation Plan — MCS-scoped measures list (issue #396)

Spec: https://github.com/Bellese/Lenny/issues/396

Branch `feature/396-mcs-scoped-measures`, stacked on `fix/mcs-auth-wiring` (PR #393).
PR #393 already added `auth_headers` to `evaluate_measure`, `wipe_patient_data`, and
`resolve_evaluated_resource`. This plan follows that convention exactly rather than
inventing a second one.

## Global Constraints

- Python 3.10+, `X | None` unions, type hints required. Ruff clean.
- React is plain JavaScript, not TypeScript. Co-located CSS Modules.
- All config via environment variables (`backend/app/config.py`). Never hardcode URLs.
- No Alembic. Schema changes go in `_run_schema_migrations()` (`backend/app/main.py:85`)
  as idempotent raw SQL, and must be safe to re-run on every boot including production.
- `base_url` on the three measure functions is REQUIRED, not defaulted to
  `settings.MEASURE_ENGINE_URL`. A default is how the current bug hides.
- Connection identity keys on `id`, never `name`. Renaming a connection must not read
  as a switch.
- Never fall back to the local engine when the active MCS is unreachable.
- Follow the `auth_headers: dict[str, str] | None = None` parameter convention already
  established by PR #393 in `fhir_client.py`.
- Conventional commits (`feat:`, `fix:`, `test:`).

## Task 1 — Backend (agent: backend)

Owns: `backend/app/models/`, `backend/app/main.py`, `backend/app/dependencies.py`,
`backend/app/services/fhir_client.py`, `backend/app/routes/`, `backend/tests/`.

Do NOT touch anything under `frontend/`.

### 1a. Data model

- Move `is_read_only` from `CDRConfig` to `ConnectionConfigMixin`
  (`backend/app/models/connection_base.py:46`):
  `Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")`.
  Remove the now-duplicate declaration from `CDRConfig`.
- Add an idempotent `ALTER TABLE mcs_configs ADD COLUMN IF NOT EXISTS is_read_only ...`
  to `_run_schema_migrations()` in `backend/app/main.py:85`, guarded the same way the
  existing statements there are. Fresh DBs are covered by `create_all`.
- Add `is_read_only` to the MCS request and response Pydantic schemas.
  `connection_factory._cfg_to_response` reflects over `response_schema.model_fields`,
  so no factory change is needed.
- Add `is_read_only` to `ConnectionContext` for the MCS kind and populate it in
  `get_active_mcs` (`backend/app/dependencies.py:99`), including the no-active-row
  fallback branch at `:107-117` (fallback is writable: `is_read_only=False`).

### 1b. Service layer (`backend/app/services/fhir_client.py`)

Change these signatures. `base_url` is required and positional-or-keyword:

```python
async def list_measures(base_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 30.0) -> dict[str, Any]
async def upload_measure_bundle(bundle_json: dict[str, Any], base_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 60.0) -> dict[str, Any]
async def delete_measure(measure_id: str, base_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 30.0) -> None
```

- Thread `base_url` and `auth_headers` into `_remap_valueset_ids_for_hapi`
  (`fhir_client.py:860`). It queries `settings.MEASURE_ENGINE_URL` directly at `:876`;
  leaving that would remap ValueSet ids against the wrong server during upload.
- New helper:
  ```python
  async def measure_exists(measure_id: str, base_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 30.0) -> bool
  ```
  Implemented as `GET {base_url}/Measure?_id={measure_id}&_summary=count`, returning
  `bundle.get("total", 0) > 0`. Propagate transport errors to the caller; do not
  swallow them into `False`.

### 1c. Routes

All four take `mcs: ConnectionContext = Depends(get_active_mcs)` and resolve auth via
the existing `_build_auth_headers(mcs.auth_type, mcs.auth_credentials)`, passing
`mcs.request_timeout_seconds` as the timeout.

| Route | File | Change |
|---|---|---|
| `GET /measures` | `routes/measures.py:19` | Read from `mcs.mcs_url`. Add an `mcs` block `{id, name, url}` to the response alongside `measures` and `total`. On upstream failure keep the 502 but put the MCS name in `diagnostics`. No fallback. |
| `POST /measures/upload` | `routes/measures.py:57` | Target `mcs.mcs_url`. Return 403 OperationOutcome when `mcs.is_read_only`, checked before reading the file body. |
| `DELETE /measures/{id}` | `routes/measures.py` | Target `mcs.mcs_url`. Return 403 OperationOutcome when `mcs.is_read_only`. |
| `POST /jobs` | `routes/jobs.py:175` | Call `measure_exists` against the active MCS before inserting the Job row. Miss → 400 OperationOutcome naming both the measure id and `mcs.name`. Transport failure → 502 naming the MCS. |

Also add `id` and `is_read_only` to the `measure_engine` block in
`backend/app/routes/health.py:82` (all three branches — connected, disconnected,
exception — so the frontend always has the id).

### 1d. Backend tests

- `list_measures` / `upload_measure_bundle` / `delete_measure` hit the passed
  `base_url` and never `settings.MEASURE_ENGINE_URL`. This is the regression guard
  for the whole bug — assert on the URL the mocked client received.
- `_remap_valueset_ids_for_hapi` queries the passed base.
- `measure_exists` returns True on `total > 0`, False on `total: 0`, and propagates
  transport errors.
- Read-only MCS returns 403 on upload and on delete.
- `POST /jobs`: measure present → 201; measure absent → 400; MCS unreachable → 502.
- `GET /measures` includes the `mcs` block.
- `/health` `measure_engine` block includes `id`.

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` and
`python3 -m pytest tests/ --ignore=tests/integration -q`. Both must pass.

## Task 2 — Frontend (agent: frontend)

Owns: everything under `frontend/src/`. Do NOT touch anything under `backend/`.

Build against this contract, which Task 1 implements in parallel:

- `GET /measures` → `{ measures: [...], total: N, mcs: { id, name, url } }`
- `GET /health` → `{ ..., measure_engine: { status, name, id, is_read_only, error_details? }, cdr: {...} }`
- `POST /measures/upload` and `DELETE /measures/{id}` → 403 when the MCS is read-only

### 2a. ConnectionContext

Create `frontend/src/contexts/ConnectionContext.js`, following the shape of the
existing `SearchContext.js`. Value:

```js
{ cdr: {id, name, state}, mcs: {id, name, state, isReadOnly}, refresh() }
```

The provider is the health-poll logic lifted out of `App.js` — the `chips` state
(`App.js:33-37`), the `failureCounts` ref (`:38`), `checkHealth` (`:63-98`), and the
visibility-aware 30s interval effect (`:100-123`) move into the provider essentially
as-is. Keep `FAILURE_DEBOUNCE` and `HEALTH_KINDS` semantics identical. `refresh()`
exposes an immediate `checkHealth()`.

`App.js` renders the provider and `HealthChipGroup` becomes a consumer instead of
receiving chip props. Do not change the chips' visual behavior.

### 2b. Change detection and pages

- `MeasuresPage` and `JobsPage`: add `mcs.id` to the `loadMeasures` effect dependency
  array so activating a different MCS refetches.
- `MeasuresPage` (`pages/MeasuresPage.js`):
  - Subtitle becomes `{N} measures on {mcs.name}` (currently `{N} measures`).
  - Replace the bare error state at `:163-168` with `ErrorBanner`
    (`components/ErrorBanner.js`, props `{title, message, issues, errorDetails}`),
    title naming the MCS, rendering upstream OperationOutcome issues via the
    `OperationOutcomeView` it already delegates to. Table stays empty. Add a Retry
    button that re-runs `loadMeasures`.
  - Disable Upload and Delete when `mcs.isReadOnly`, with a title tooltip naming the
    connection.
- `JobsPage` (`pages/JobsPage.js`): the effect at `:91-95` defaults
  `formData.measure_id` only when empty. It must ALSO reset when the current
  selection is absent from the newly loaded list, or switching MCS leaves a stale id
  that `POST /jobs` now rejects.
- `components/ConnectionSection.js:32`: set `showReadOnlyBadge: true` for the `mcs`
  kind.
- `components/ConnectionSection.js`: after a successful activate, call the context's
  `refresh()` so the UI updates immediately instead of waiting up to 30s.
- `ConnectionModal.js`: add `showReadOnly: true` to the MCS spec. The checkbox
  (`:222-229`) and payload wiring (`:130-132`) are already generic over `spec`.

### 2c. Frontend tests

- `MeasuresPage` renders the MCS name in the subtitle.
- `MeasuresPage` renders the error state naming the MCS and shows no measures.
- `MeasuresPage` disables Upload when the MCS is read-only.
- `JobsPage` clears a stale `measure_id` when the measure list no longer contains it.
- `ConnectionContext` provider exposes mcs id/name from the health payload.

Run: `cd frontend && CI=true npm test -- --watchAll=false` and `npm run build`.
Both must pass.

## Task 3 — Integration test (controller, after Tasks 1 and 2)

New file `backend/tests/integration/test_mcs_scoped_measures.py`: with two MCS
connections configured, activating one and then the other returns different measure
sets from `GET /measures`. `hapi-fhir-measure` (:8080) holds the connectathon
measures; `hapi-fhir-cdr` (:8081) holds none, so a second MCS connection pointed at
:8081 gives a real two-server test with no new infrastructure.

Per CLAUDE.md this is a NEW integration file: the CI-equivalent suite uses `--ignore`
flags and will silently skip it, so it must be run locally before push.

## Out of scope

- Caching measure lists per MCS.
- Migrating historical Jobs or Results — they already snapshot `mcs_url`/`mcs_name`.
- CDR connection behavior beyond relocating `is_read_only` to the shared mixin.
