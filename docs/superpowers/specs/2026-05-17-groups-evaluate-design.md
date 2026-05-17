# Experimental Groups Page: `Group/<id>/$evaluate`

**Issue:** [Bellese/Lenny#322](https://github.com/Bellese/Lenny/issues/322)
**Status:** Design approved 2026-05-17
**Spec author:** Claude (paired w/ @blakenan-bellese)

## 1. Summary

Add an experimental, admin-gated "Groups" page that lists CQL-evaluatable Groups on the connected CDR and lets users invoke `Group/<id>/$evaluate` to materialize members. Architecturally independent from the existing Measure pipeline. When the `groups_enabled` admin toggle is off, Lenny looks and behaves exactly as today — the only visible difference is the new toggle row on Settings → Developer Tools.

The bundled HAPI image (`v8.8.0-1`) does not implement this operation; the feature requires connecting Lenny to a CDR that does.

## 2. Operation reference

- Operation: `Group/<id>/$evaluate`. Defined in the CQL IG. The OperationDefinition file is named `cql-group-evaluate` but the operation's `code` is `evaluate`.
- IG: <https://build.fhir.org/ig/HL7/cql-ig/en/OperationDefinition-cql-group-evaluate.html>
- Returns a `Group` resource whose `member[].entity.reference` is populated based on the evaluation. References are typically of the form `Patient/{id}` and do not carry inline patient summary fields.
- CQL-evaluatable Groups are identified by an extension at `http://hl7.org/fhir/StructureDefinition/characteristicExpression` whose value is a `valueExpression` with `language` starting with `text/cql` (covers `text/cql-expression`, `text/cql-identifier`, etc.).
- Example Group: <https://github.com/cqframework/dqm-content-cms-2026/blob/main/input/resources/group/InlinePatientWithAgeRangeAndEncounter.json>

## 3. Architecture & boundaries

### New modules (no measure-pipeline coupling)

- `backend/app/routes/groups.py` — FastAPI router, prefix `/api/groups`
- `frontend/src/pages/GroupsPage.js` + `GroupsPage.module.css`
- `frontend/src/api/client.js` — add `getEvaluatableGroups()` and `evaluateGroup(id)`. The existing `getGroups()` client and `GET /jobs/groups` endpoint stay untouched — they belong to the measure-job flow.

### Reused shared infrastructure (allowed; not measure-specific)

- `backend/app/services/fhir_client.py` — add two new helpers (`list_groups_with_expression`, `evaluate_group_and_resolve_members`). Do **not** modify the existing `list_groups` or `get_group_members` — their contracts are pinned by the Jobs flow.
- `Depends(get_active_cdr)` — same CDR-connection pattern every route already uses.
- UI components: `ErrorBanner`, `OperationOutcomeView`, the patient-row component used elsewhere (`PatientDetail` or a thinner variant).

### Settings & feature flag

- New field `groups_enabled: bool = False` in the admin settings model.
- Toggle row added under Developer Tools in `SettingsPage.js`, matching the `validation_enabled` / `comparison_enabled` pattern. Description copy:
  > Adds a Groups tab where you can list CQL-evaluatable Groups from the current CDR and invoke `Group/<id>/$evaluate` to resolve members. Requires a CDR that supports the operation (the bundled HAPI image does not). Experimental.
- `App.js`:
  - Add `{ path: '/groups', label: 'Groups', Icon: …, kbd: 'G', feature: 'groups' }` to `ALL_NAV_ITEMS`, positioned **below `Jobs`** (matches issue: "below Runs").
  - Route `<Route path="/groups" element={<GroupsPage />} />` registered unconditionally.
  - `getAdminSettings()` consumer extended to expose `features.groups: s.groups_enabled`.
  - Keyboard handler block gains `'g' | 'G'` → `navigate('/groups')` gated on `features.groups`.

### Disabled-state behavior

- Nav item hidden when `features.groups === false` (existing filter pattern).
- `GroupsPage` checks `features.groups`; if false, renders `<Navigate to="/measures" replace />`.
- Backend endpoints return `404` when `groups_enabled=false` — keeps the API contract honest if hit directly.

### Independence verification

Unit test (`tests/test_groups_independence.py`) AST-parses `backend/app/routes/groups.py` and asserts no `import` or `from` statements name any of:
- `app.orchestrator`
- `app.routes.jobs`
- `app.routes.measures`
- `app.routes.results`
- Any module ending in `_models` that belongs to measure/job state.

Frontend equivalent: a single test that statically walks `GroupsPage.js`'s import graph and asserts the same negative set.

## 4. Backend contract

### `GET /api/groups`

Response — `200 OK`:
```json
{
  "groups": [
    {
      "id": "InlinePatientWithAgeRangeAndEncounter",
      "name": "InlinePatientWithAgeRangeAndEncounter",
      "type": "person",
      "expression_language": "text/cql-expression",
      "expression_preview": "Patient.birthDate >= Today() - 18 years and exists ([Encounter] ..."
    }
  ]
}
```

- Lists CDR Groups filtered to those with a `characteristicExpression` extension whose `valueExpression.language` starts with `text/cql`.
- `expression_preview`: first ~120 chars of the expression, for quick visual verification on the row.
- `502` if the CDR is unreachable (matches existing `/jobs/groups` error contract).
- `404` if `groups_enabled=false`.

### `POST /api/groups/{id}/evaluate`

Response — `200 OK`:
```json
{
  "group_id": "InlinePatientWithAgeRangeAndEncounter",
  "evaluated_at": "2026-05-17T14:32:01Z",
  "member_count": 3,
  "members": [
    { "id": "patient-1", "name": "Smith, John", "gender": "male", "birth_date": "1980-04-12" }
  ]
}
```

- Calls `POST {cdr}/Group/{id}/$evaluate` with no body parameters — the Group already carries its `valueExpression`.
- Parses the returned Group, extracts `member[].entity.reference` of the form `Patient/{id}`, fans out reads using the same semaphore-bounded concurrency pattern as `get_group_members` (semaphore=10, 60s client timeout).
- Members whose `Patient/{id}` read fails are still included with `name: null` and a `lookup_error` field — partial failure does not blank the whole accordion.

Error responses:

- `$evaluate` returns 4xx/5xx with an OperationOutcome → `502 { "operation_outcome": <verbatim FHIR OperationOutcome> }`.
- `$evaluate` returns 5xx without OperationOutcome → `502 { "error": "...", "status": <upstream> }`.
- Backend `httpx.TimeoutException` → `504 { "error": "..." }`.
- `404` if `groups_enabled=false`.

### Helper additions to `fhir_client.py`

- `list_groups_with_expression(cdr_url, auth_headers)` — paginates `GET /Group?_count=100`, filters/projects to the shape above. Reuses internal helpers (`_same_origin`, `sanitize_url`) but does not touch `list_groups`.
- `evaluate_group_and_resolve_members(cdr_url, group_id, auth_headers)` — invokes `$evaluate`, parses members, fans out Patient reads (semaphore=10, 60s timeout), returns the enriched list and the evaluated-at timestamp.

### Safety

`_validate_ssrf_url` is applied to the active CDR at connection-establishment time, so the new endpoints inherit SSRF protection for free. Pagination next-links go through the existing `_same_origin` check.

## 5. Frontend contract & UI

### `api/client.js` additions

```js
export function getEvaluatableGroups() {
  return request('/api/groups');
}
export function evaluateGroup(groupId) {
  return request(`/api/groups/${encodeURIComponent(groupId)}/evaluate`, { method: 'POST' });
}
```

### `GroupsPage.js` state shape (component-local, ephemeral)

```js
const [groups, setGroups] = useState([]);        // GET /api/groups result
const [loading, setLoading] = useState(true);    // initial list load
const [listError, setListError] = useState(null);
const [evalState, setEvalState] = useState({});  // { [groupId]: 'idle' | 'running' | 'complete' | 'error' }
const [results, setResults] = useState({});      // { [groupId]: { members, evaluated_at, member_count } }
const [errors, setErrors] = useState({});        // { [groupId]: OperationOutcome | string }
const [expanded, setExpanded] = useState({});    // { [groupId]: bool }
```

All state is component-local. Unmounting clears it — satisfies the ephemeral requirement. A user can evaluate Group A, expand it, then evaluate Group B without losing A's accordion; both stay expandable until navigation.

### Disabled-state guard

```js
if (features.groups === false) return <Navigate to="/measures" replace />;
```

Reads `features` from whatever context/prop App.js uses to gate the nav item.

### Layout (mirrors `JobsPage`)

- Page header: title "Groups" + subdued description: *"Showing only Groups with a CQL `valueExpression`. Other Groups on the CDR are hidden."*
- Below the description: list of group rows, same row styling as `JobsPage`.
- Empty state (zero groups): centered *"No CQL-evaluatable Groups found on this CDR."*
- List-load error: top-of-page `ErrorBanner` with the backend message.
- Header has a small refresh button that re-runs `getEvaluatableGroups()` without clearing existing per-row results.

### Row anatomy

**Collapsed:**
- Left: Group name (or id if name missing), small monospaced id chip, small language chip (`text/cql-expression`), expression preview (truncated, monospaced).
- Right: `$evaluate` button. While running: spinner + label "Evaluating…", button disabled. After complete: chevron expand/collapse toggle alongside the button; button label switches to "Re-evaluate".

**Expanded (accordion):**
- Header strip: `member_count` + `evaluated_at` (relative time).
- Members table: name | id | gender | birth_date. Rows with `lookup_error` render dimmed with the id and an inline warning icon.
- If evaluation failed: instead of a table, render `OperationOutcomeView` for FHIR errors, or a plain string error otherwise.

### `$evaluate` click flow

1. `setEvalState(s => ({ ...s, [groupId]: 'running' }))`
2. `evaluateGroup(groupId)`:
   - success → store result, transition to `complete`, auto-expand the row
   - failure → store error, transition to `error`, auto-expand the row
3. No polling, no toast — the row's own state is the indicator.

## 6. Error matrix

| Scenario | Backend | Frontend |
|---|---|---|
| No active CDR connection | `get_active_cdr` raises 400 | Top-of-page `ErrorBanner`: "No CDR connected. Configure one in Settings." |
| CDR unreachable on list | `502` | Top-of-page `ErrorBanner` |
| Zero matching Groups | `200 { "groups": [] }` | Centered empty state |
| Feature disabled but `/groups` hit directly | `404` from endpoints; `<Navigate>` from page | Redirect to `/measures` |
| `$evaluate` returns 4xx + OperationOutcome | `502 { operation_outcome: {...} }` | Row expanded, `OperationOutcomeView` |
| `$evaluate` returns 5xx without OperationOutcome | `502 { error, status }` | Row expanded, plain error |
| `$evaluate` returns Group with zero members | `200 { members: [], member_count: 0 }` | Row expanded, "0 members" subheader, no table |
| Per-member Patient read fails | Member entry with `name: null, lookup_error` | Dimmed table row with warning icon |
| Backend timeout (>60s on either CDR call) | `504` | Row expanded with timeout message |

## 7. Testing (unit only)

### Backend — `tests/test_routes_groups.py`

- `GET /api/groups` happy path: mock `list_groups_with_expression`, assert response shape and filtering.
- `GET /api/groups` when CDR unreachable: assert `502`.
- `GET /api/groups` when `groups_enabled=false`: assert `404`.
- `POST /api/groups/{id}/evaluate` happy path: mock `evaluate_group_and_resolve_members`, assert enriched response.
- `POST /api/groups/{id}/evaluate` with OperationOutcome from CDR: assert `502` + `operation_outcome` body.
- `POST /api/groups/{id}/evaluate` partial member-read failure: assert `lookup_error` entries.
- `POST /api/groups/{id}/evaluate` when `groups_enabled=false`: assert `404`.

### Backend — `tests/test_fhir_client_groups.py`

- `list_groups_with_expression` filters by `characteristicExpression` + `text/cql*` language.
- `evaluate_group_and_resolve_members` parses returned Group, fans out Patient reads, respects semaphore, returns partial failures.
- Pagination handling and SSRF rejection on next-link.

### Backend — `tests/test_groups_independence.py`

AST-walks `app/routes/groups.py`; asserts no imports from `app.orchestrator`, `app.routes.jobs`, `app.routes.measures`, `app.routes.results`, or measure/job state modules.

### Frontend — `GroupsPage.test.js`

- Renders rows from mocked `getEvaluatableGroups`.
- Clicking `$evaluate` shows spinner, then expands accordion with members.
- Error path: row expands with `OperationOutcomeView`.
- `features.groups === false` → page redirects to `/measures`.
- Re-evaluate guard: button disabled while running.
- Empty state when zero groups.
- Refresh button re-fetches list without clearing existing per-row results.

### Frontend — additions to existing tests

- `App.js` nav filter: `features.groups = false` → nav item absent; `= true` → present.
- Settings page: toggling Groups dispatches `admin-settings-changed` event with `groups_enabled`.

## 8. Acceptance traceability

| Issue acceptance criterion | Where satisfied |
|---|---|
| Default `groups_enabled=false` → app visually identical to `main` except toggle row | `App.js` nav filter; backend default; `GroupsPage` redirect; unit test on nav filter |
| `groups_enabled=true` → "Groups" sidebar tab below Runs | Insert in `ALL_NAV_ITEMS` between Jobs and Results |
| `/groups` registered unconditionally with disabled-state redirect | `<Route>` always registered; `<Navigate>` guard inside `GroupsPage` |
| Page lists only Groups with `valueExpression`; guiding text explains filter | `list_groups_with_expression` + page subtitle |
| `$evaluate` invokes `Group/<id>/$evaluate` and expands row on success | `evaluate_group_and_resolve_members` + accordion state transitions |
| OperationOutcome errors render inline on the row | `502 + operation_outcome` body → `OperationOutcomeView` |
| No persistence: refresh/navigate clears results | Component-local `useState`; no backend storage; no DB migration |
| No import path linking new feature to measure-pipeline modules | Independence unit test |

## 9. Out of scope (will not be implemented under this spec)

- Persistence of evaluation history
- Batch evaluation across Groups
- CQL authoring or Group creation inside Lenny
- Running measures against a Group
- Upgrading the bundled HAPI image to support `$evaluate`
- Streaming or progressive member disclosure
- Capability-statement pre-flight checks
