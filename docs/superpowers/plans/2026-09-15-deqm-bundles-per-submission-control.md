# DEQM Bundles-Per-Submission Operator Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator choose how many subjects' bundles ride in one DEQM `$submit-data` POST, clamped to what the server and the processing chunk allow, and remembered across jobs.

**Architecture:** The capability probe widens to carry the server's declared `bundle` maximum alongside its mode verdict. Job creation clamps the operator's request against that maximum and the chunk size, storing the raw request and the clamped result in two new `jobs` columns. The orchestrator threads the clamped value into `DeqmSubmitDataWorkflow`'s existing `group_size` constructor argument — the one PR 2 built and deliberately left unpassed. The creation form derives its default from the most recent DEQM job's *requested* value, so no settings row and no new endpoint are involved.

**Tech Stack:** Python 3.10+ (FastAPI, SQLAlchemy 2.0 async, Pydantic v2, pytest with `asyncio_mode = auto`), React 18 plain JavaScript with CSS Modules, Jest + React Testing Library, ruff.

**Spec:** `docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md` — read § The bundles-per-submission control, § Testing, and § PR 3 — The user-facing control before starting.

**Issue:** #413, PR 3 of 3. PR 1 (`e11415d`) shipped the contract; PR 2 (`fa08384`) shipped the grouping mechanics behind a constructor argument nothing sets.

## Global Constraints

- Python 3.10+ union syntax: `X | None`, never `Optional[X]` in new code. (Existing `Optional[...]` in `routes/jobs.py` stays as-is — match the file when editing its existing models, use `X | None` in new service code.)
- Type hints are required on every new function.
- ruff line-length is 120. Run `ruff check app/ tests/ && ruff format --check app/ tests/` before every commit.
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`.
- React is plain JavaScript, not TypeScript. Components are PascalCase; CSS lives in co-located `*.module.css`.
- All configuration comes from environment variables via `backend/app/config.py`. Never hardcode a URL, a credential, or `BATCH_SIZE`.
- `CHUNK` means `settings.BATCH_SIZE` (default 100) throughout.
- `requested == 0` means "no limit beyond the chunk" and resolves to `CHUNK`.
- The effective value is `min(requested or CHUNK, server_bundle_max or INF, CHUNK)`.
- The remembered value is the **requested** one, never the clamped one.
- Both new `jobs` columns are `NULL` for `direct_load` jobs.
- #414's guarantee is load-bearing and must stay true: no job may submit some subjects as STU5 and others as base.
- Do NOT modify `TODOS.md` — it is frozen.
- Do NOT run any command with `&`, with a background flag, or via a monitor. Every command runs in the foreground.

## Pre-existing state you can rely on

These already exist on `main` as of `fa08384`. Do not rebuild them:

- `DeqmSubmitDataWorkflow.__init__(..., group_size: int = 1)` storing `self._group_size = max(1, group_size)` (`workflows.py:333`).
- `DeqmSubmitDataWorkflow.submission_group_size` returning `self._group_size` under STU5 and `1` otherwise (`workflows.py:354`).
- The orchestrator's group-outer/subject-inner Phase 1 loop, which re-reads `workflow.submission_group_size` fresh for every group.
- `_submit_individually(subjects, mode)`, `_submit_group(subjects)`, `_is_payload_attributable(exc)`, `_ISOLATE_STATUS_CODES = {400, 409, 422}`.
- The `main.py` idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` list (around `main.py:230-251`).

## File Structure

| File | Responsibility in this PR |
|---|---|
| `backend/app/services/fhir_client.py` | `SubmitDataCapability` dataclass; `detect_submit_data_capability` replacing `detect_submit_data_mode`; `_bundle_max_from_definition`. |
| `backend/app/models/job.py` | Two nullable integer columns on `Job`. |
| `backend/app/main.py` | Two idempotent `ALTER TABLE` statements. |
| `backend/app/routes/jobs.py` | `JobCreate.bundles_per_submission`; `_effective_bundles_per_submission`; the probe call site; both response fields. |
| `backend/app/services/workflows.py` | `build_submission_workflow` gains `bundles_per_submission`; `_settle_mode_and_submit` splits for M6. |
| `backend/app/services/orchestrator.py` | Passes the stored value to the factory; rewrites it to 1 on a runtime downgrade. |
| `frontend/src/pages/JobsPage.js` | The numeric input, its derived default, helper text, and the clamp toast. |
| `frontend/src/pages/JobsPage.module.css` | One `.fieldHelp` class. |
| `docs/architecture.md` | Documents the control and the clamp. |

---

### Task 1: The capability probe carries the server's bundle max

**Files:**
- Modify: `backend/app/services/fhir_client.py` (`_operation_definition_matches_contract` at `:1064`, `detect_submit_data_mode` at `:1190`)
- Modify: `backend/app/routes/jobs.py:27-33` (import) and `:315` (call site)
- Test: `backend/tests/test_services_fhir_client.py` (class `TestDetectSubmitDataMode` at `:3102`)
- Test: `backend/tests/test_routes_jobs.py` (patch targets naming `detect_submit_data_mode`)

**Interfaces:**
- Produces: `SubmitDataCapability(mode: str, bundle_max: int | None)` — a frozen dataclass exported from `app.services.fhir_client`.
- Produces: `async def detect_submit_data_capability(*, mcs_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 10.0) -> SubmitDataCapability`.
- Removes: `detect_submit_data_mode`. There is no wrapper — every call site moves.

**Context:** `bundle_max` is read off the `bundle` input parameter of the OperationDefinition that matched the contract. FHIR types `OperationDefinition.parameter.max` as a string: `"*"` means unbounded, otherwise a digit string. Anything else is malformed. Malformed and `"*"` both resolve to `None` (unbounded) because an unparseable bound is not evidence of a limit — and crucially, the **mode verdict must not change** in any of these cases.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_services_fhir_client.py`, inside `class TestDetectSubmitDataMode`:

```python
    async def test_bundle_max_star_is_unbounded(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_bundle_max_digit_string_is_retained(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "5"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max == 5

    async def test_bundle_max_of_one_still_classifies_stu5(self):
        """Spec § Testing: a max:"1" server is STU5 and the max is retained for
        clamping. The declared bound governs how many bundles we send, never
        whether the contract is supported at all."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "1"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max == 1

    async def test_missing_bundle_max_is_unbounded(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_unparseable_bundle_max_is_unbounded_and_does_not_disturb_the_verdict(self):
        """An unparseable bound is not evidence of a limit, and must never cost
        a job its STU5 path."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "many"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_base_fallback_reports_no_bundle_max(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=httpx.ConnectError("boom")))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_BASE
        assert result.bundle_max is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py::TestDetectSubmitDataMode -v`
Expected: FAIL — `NameError: name 'detect_submit_data_capability' is not defined` (the import in Step 5 is what fixes it).

- [ ] **Step 3: Add the dataclass and the max parser**

In `backend/app/services/fhir_client.py`, immediately above `_operation_definition_matches_contract` (currently `:1064`):

```python
@dataclass(frozen=True)
class SubmitDataCapability:
    """What the CapabilityStatement probe learned about $submit-data.

    `mode` is the wire format verdict — SUBMIT_DATA_MODE_STU5 or
    SUBMIT_DATA_MODE_BASE — and carries exactly the meaning the probe has
    always returned. `bundle_max` is the server's declared ceiling on the
    number of `bundle` parameters one submission may carry: None means
    unbounded, which covers `max: "*"`, an absent max, and an unparseable
    one. An unparseable bound is not evidence of a limit, and must never
    cost a job its STU5 path — so it never affects `mode`.
    """

    mode: str
    bundle_max: int | None = None


def _bundle_max_from_definition(operation_definition: dict[str, Any]) -> int | None:
    """The declared ceiling on `bundle` parameters, or None for unbounded.

    FHIR types OperationDefinition.parameter.max as a string: "*" for
    unbounded, otherwise a digit string. A non-positive or non-numeric value
    is malformed; both resolve to unbounded rather than to a bound we made up.
    """
    for param in operation_definition.get("parameter", []):
        if param.get("name") != "bundle" or param.get("use") != "in":
            continue
        raw = param.get("max")
        if not isinstance(raw, str) or not raw.isdigit():
            return None
        value = int(raw)
        return value if value > 0 else None
    return None
```

- [ ] **Step 4: Convert the probe to return the capability**

In the same file, change the signature and the three `return` statements of `detect_submit_data_mode`. Rename it to `detect_submit_data_capability`, keeping every existing comment in the body:

```python
async def detect_submit_data_capability(
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> SubmitDataCapability:
```

Update its docstring's first line to:

```
    """Probe the MCS for the selected type-level $submit-data bundle contract.

    Returns a SubmitDataCapability whose `mode` is SUBMIT_DATA_MODE_STU5 only
    when the server advertises an operation whose OperationDefinition has
    `code: submit-data`, `type: true`, and a `bundle` input parameter, and
    whose `bundle_max` carries that parameter's declared ceiling for clamping.
```

Leave the rest of the docstring unchanged. Then change the match branch (currently `:1246-1247`) from:

```python
                if operation_definition is not None and _operation_definition_matches_contract(operation_definition):
                    return SUBMIT_DATA_MODE_STU5
```

to:

```python
                if operation_definition is not None and _operation_definition_matches_contract(operation_definition):
                    return SubmitDataCapability(
                        mode=SUBMIT_DATA_MODE_STU5,
                        bundle_max=_bundle_max_from_definition(operation_definition),
                    )
```

and the final line of the function from `return SUBMIT_DATA_MODE_BASE` to:

```python
    return SubmitDataCapability(mode=SUBMIT_DATA_MODE_BASE)
```

- [ ] **Step 5: Move the test import**

In `backend/tests/test_services_fhir_client.py`, change the import at `:22` from `detect_submit_data_mode,` to `detect_submit_data_capability,`. Then update all 13 existing assertions in `TestDetectSubmitDataMode` from the form:

```python
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5
```

to:

```python
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_STU5
```

The same transformation applies to every `== SUBMIT_DATA_MODE_BASE` assertion. One call at `:3185` is not an assertion (it is inside a "never raises" test) — change only the function name there.

- [ ] **Step 6: Move the production call site**

In `backend/app/routes/jobs.py`, change the import at `:30` from `detect_submit_data_mode,` to `detect_submit_data_capability,`, then change the call at `:313-319` from:

```python
    submit_data_mode: str | None = None
    if body.workflow == "deqm_submit_data":
        submit_data_mode = await detect_submit_data_mode(
            mcs_url=mcs.mcs_url,
            auth_headers=mcs_auth_headers,
            timeout=preflight_timeout,
        )
```

to:

```python
    submit_data_mode: str | None = None
    submit_data_capability: SubmitDataCapability | None = None
    if body.workflow == "deqm_submit_data":
        submit_data_capability = await detect_submit_data_capability(
            mcs_url=mcs.mcs_url,
            auth_headers=mcs_auth_headers,
            timeout=preflight_timeout,
        )
        submit_data_mode = submit_data_capability.mode
```

Add `SubmitDataCapability,` to the same import block. `submit_data_capability` is unused until Task 3 — that is deliberate, and ruff will not complain about an assigned-and-read local.

- [ ] **Step 7: Update the route tests' patch targets**

In `backend/tests/test_routes_jobs.py`, find every occurrence of `detect_submit_data_mode` (patch targets and any direct reference) and rename it to `detect_submit_data_capability`. Any patch whose `return_value` is a bare mode string must become `SubmitDataCapability(mode=...)` — import it from `app.services.fhir_client`. Run `grep -n detect_submit_data tests/test_routes_jobs.py` first to enumerate them.

- [ ] **Step 8: Run the full backend unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS, with no remaining reference to `detect_submit_data_mode`. Confirm with `grep -rn detect_submit_data_mode app/ tests/` returning nothing.

- [ ] **Step 9: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/fhir_client.py backend/app/routes/jobs.py backend/tests/test_services_fhir_client.py backend/tests/test_routes_jobs.py
git commit -m "feat: probe returns the server's declared bundle max alongside the mode"
```

---

### Task 2: The two job columns

**Files:**
- Modify: `backend/app/models/job.py` (after `submit_data_mode` at `:97`)
- Modify: `backend/app/main.py` (the `ALTER TABLE` list, after the `submit_data_mode` line at `:242`)
- Modify: `backend/app/routes/jobs.py` (`JobResponse` at `:91`, `_job_to_response` at `:140`)
- Test: `backend/tests/test_routes_jobs.py`

**Interfaces:**
- Produces: `Job.bundles_per_submission: int | None` and `Job.bundles_per_submission_requested: int | None`.
- Produces: the same two keys in every job API response.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_routes_jobs.py` (match the file's existing client fixture style — read a neighbouring test first):

```python
async def test_job_response_carries_both_bundle_columns_as_null_by_default(client):
    """A direct_load job contributes no bundles-per-submission value at all:
    both columns are NULL, and the response says so rather than inventing a 1."""
    resp = await client.post("/api/jobs", json=_valid_job_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["bundles_per_submission"] is None
    assert body["bundles_per_submission_requested"] is None
```

If `_valid_job_body()` does not exist in that file, build the body inline from whatever an adjacent creation test uses.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py::test_job_response_carries_both_bundle_columns_as_null_by_default -v`
Expected: FAIL with `KeyError: 'bundles_per_submission'`.

- [ ] **Step 3: Add the model columns**

In `backend/app/models/job.py`, directly after the `submit_data_mode` column:

```python
    # Issue #413 PR 3. Two columns, not one, because the remembered preference
    # must survive a clamp: `_requested` is what the operator asked for and is
    # what the creation form reads back, while `bundles_per_submission` is the
    # clamped value the job actually ran under. Storing only the clamped value
    # would let one job against a `bundle max: "1"` server ratchet the operator's
    # preference down to 1 permanently, with nothing to ever raise it back.
    #
    # Both NULL for direct_load, which has no submission-grouping concept.
    bundles_per_submission: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bundles_per_submission_requested: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Add the idempotent migration**

In `backend/app/main.py`, directly after the `submit_data_mode` entry in the `ALTER TABLE` list:

```python
            # Issue #413 PR 3. NULL on existing rows is correct: every job
            # created before this column existed ran one subject per POST, and
            # NULL renders as "not applicable" rather than as a number nobody chose.
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bundles_per_submission INTEGER",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bundles_per_submission_requested INTEGER",
```

- [ ] **Step 5: Add the response fields**

In `backend/app/routes/jobs.py`, add to `JobResponse` after `submit_data_mode`:

```python
    bundles_per_submission: Optional[int] = None
    bundles_per_submission_requested: Optional[int] = None
```

and to the dict returned by `_job_to_response`, after `"submit_data_mode": job.submit_data_mode,`:

```python
        "bundles_per_submission": job.bundles_per_submission,
        "bundles_per_submission_requested": job.bundles_per_submission_requested,
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py -v`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/models/job.py backend/app/main.py backend/app/routes/jobs.py backend/tests/test_routes_jobs.py
git commit -m "feat: add requested and effective bundles-per-submission columns to jobs"
```

---

### Task 3: Accept and clamp the operator's request

**Files:**
- Modify: `backend/app/routes/jobs.py` (`JobCreate` at `:58`, the creation body at `:308-342`)
- Test: `backend/tests/test_routes_jobs.py`

**Interfaces:**
- Consumes: `SubmitDataCapability` and `detect_submit_data_capability` (Task 1); `Job.bundles_per_submission*` (Task 2).
- Produces: `def _effective_bundles_per_submission(requested: int | None, bundle_max: int | None, chunk: int) -> int` in `app/routes/jobs.py`.
- Produces: `JobCreate.bundles_per_submission: int | None`.

**Context:** `requested is None` means the caller said nothing and gets the conservative default of 1 — not the chunk. `requested == 0` is an explicit "no limit beyond the chunk" and resolves to `chunk`. These are different inputs and must not be collapsed; `or` on an integer would silently merge `0` and `None`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_routes_jobs.py`:

```python
@pytest.mark.parametrize(
    "requested,bundle_max,chunk,expected",
    [
        (None, None, 100, 1),      # said nothing -> conservative default
        (0, None, 100, 100),       # explicit "unlimited" -> the chunk
        (5, None, 100, 5),         # under every ceiling -> honoured
        (500, None, 100, 100),     # above the chunk -> clamped to the chunk
        (50, 1, 100, 1),           # server says max 1 -> clamped to 1
        (50, 10, 100, 10),         # server ceiling bites before the chunk
        (0, 10, 100, 10),          # "unlimited" still respects the server
        (5, 10, 3, 3),             # chunk is the tightest ceiling
    ],
)
def test_effective_bundles_per_submission(requested, bundle_max, chunk, expected):
    assert _effective_bundles_per_submission(requested, bundle_max, chunk) == expected


def test_zero_and_none_are_not_the_same_request():
    """`or` on an integer would merge them. 0 is an explicit 'every subject in
    the chunk'; None is 'the caller said nothing' and must stay conservative."""
    assert _effective_bundles_per_submission(0, None, 100) == 100
    assert _effective_bundles_per_submission(None, None, 100) == 1


async def test_negative_bundles_per_submission_is_rejected(client):
    body = {**_valid_job_body(), "workflow": "deqm_submit_data", "bundles_per_submission": -1}
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 422


async def test_non_integer_bundles_per_submission_is_rejected(client):
    body = {**_valid_job_body(), "workflow": "deqm_submit_data", "bundles_per_submission": "lots"}
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 422


async def test_a_later_job_does_not_rewrite_an_earlier_jobs_record(client):
    """Each job's row is a snapshot of what THAT job did. With the preference
    living on the job rows rather than in a settings table, this is structural
    rather than enforced — the test is what stops a future refactor from
    reintroducing a shared mutable default that back-writes history."""
    with patch(
        "app.routes.jobs.detect_submit_data_capability",
        AsyncMock(return_value=SubmitDataCapability(mode="stu5", bundle_max=None)),
    ):
        first = await client.post(
            "/api/jobs", json={**_valid_job_body(), "workflow": "deqm_submit_data", "bundles_per_submission": 5}
        )
        await client.post(
            "/api/jobs", json={**_valid_job_body(), "workflow": "deqm_submit_data", "bundles_per_submission": 40}
        )
    reread = await client.get(f"/api/jobs/{first.json()['id']}")
    assert reread.json()["bundles_per_submission"] == 5
    assert reread.json()["bundles_per_submission_requested"] == 5


async def test_direct_load_job_stores_neither_value(client):
    body = {**_valid_job_body(), "workflow": "direct_load", "bundles_per_submission": 20}
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 200
    assert resp.json()["bundles_per_submission"] is None
    assert resp.json()["bundles_per_submission_requested"] is None
```

Import `_effective_bundles_per_submission` from `app.routes.jobs` at the top of the test file.

For the clamp-is-logged case, add (adapting the existing DEQM creation test's patching of `detect_submit_data_capability`):

```python
async def test_a_clamped_request_is_logged_with_its_reason(client, caplog):
    """A silently reduced group size is unexplainable in the field. The log
    line is the only place the operator's 50 and the server's 1 appear together."""
    with patch(
        "app.routes.jobs.detect_submit_data_capability",
        AsyncMock(return_value=SubmitDataCapability(mode="stu5", bundle_max=1)),
    ):
        body = {**_valid_job_body(), "workflow": "deqm_submit_data", "bundles_per_submission": 50}
        with caplog.at_level(logging.WARNING):
            resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 200
    assert resp.json()["bundles_per_submission"] == 1
    assert resp.json()["bundles_per_submission_requested"] == 50
    assert any("bundles per submission" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py -k bundles -v`
Expected: FAIL — `ImportError: cannot import name '_effective_bundles_per_submission'`.

- [ ] **Step 3: Add the clamp helper**

In `backend/app/routes/jobs.py`, near the other module-level helpers (below `_job_to_response`):

```python
def _effective_bundles_per_submission(requested: int | None, bundle_max: int | None, chunk: int) -> int:
    """Resolve the operator's request against every ceiling that applies.

    Spec: `min(requested or CHUNK, server_bundle_max or INF, CHUNK)`.

    `requested is None` (the caller said nothing) and `requested == 0` (an
    explicit "every subject in the chunk") are DIFFERENT inputs: None keeps the
    conservative default of 1, 0 opens up to the chunk. A plain `or` would
    merge them, because 0 is falsy — which would silently turn every unspecified
    request into a 100-subject POST.
    """
    if requested is None:
        candidate = 1
    elif requested == 0:
        candidate = chunk
    else:
        candidate = requested
    ceilings = [candidate, chunk]
    if bundle_max is not None:
        ceilings.append(bundle_max)
    return max(1, min(ceilings))
```

- [ ] **Step 4: Add the request field**

In `JobCreate`, after `workflow: str = "direct_load"`:

```python
    # Issue #413 PR 3. >= 0; 0 means "every subject in a processing batch".
    # None means the caller said nothing and takes the conservative default.
    bundles_per_submission: Optional[int] = None

    @field_validator("bundles_per_submission")
    @classmethod
    def validate_bundles_per_submission(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("bundles_per_submission must be zero or greater")
        return v
```

Pydantic v2 rejects a non-integer for an `Optional[int]` field on its own, producing the 422 the test expects — the validator only has to cover the negative case.

- [ ] **Step 5: Clamp and store at creation**

In `create_job`, directly after the probe block from Task 1 Step 6, add:

```python
    bundles_requested: int | None = None
    bundles_effective: int | None = None
    if body.workflow == "deqm_submit_data":
        bundles_requested = body.bundles_per_submission
        bundles_effective = _effective_bundles_per_submission(
            bundles_requested,
            submit_data_capability.bundle_max if submit_data_capability else None,
            settings.BATCH_SIZE,
        )
        if bundles_requested is not None and bundles_requested != 0 and bundles_effective < bundles_requested:
            logger.warning(
                "Clamped bundles per submission from %s to %s",
                bundles_requested,
                bundles_effective,
                extra={
                    "requested": bundles_requested,
                    "effective": bundles_effective,
                    "server_bundle_max": submit_data_capability.bundle_max if submit_data_capability else None,
                    "batch_size": settings.BATCH_SIZE,
                },
            )
```

Then add to the `Job(...)` constructor, after `submit_data_mode=submit_data_mode,`:

```python
        bundles_per_submission=bundles_effective,
        bundles_per_submission_requested=bundles_requested,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py -v`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/routes/jobs.py backend/tests/test_routes_jobs.py
git commit -m "feat: accept and clamp bundles_per_submission at job creation"
```

---

### Task 4: Thread the clamped value into the workflow

**Files:**
- Modify: `backend/app/services/workflows.py` (`build_submission_workflow` at `:621`)
- Modify: `backend/app/services/orchestrator.py` (the factory call at `:231`)
- Test: `backend/tests/test_services_workflows.py`
- Test: `backend/tests/test_services_orchestrator.py`

**Interfaces:**
- Consumes: `Job.bundles_per_submission` (Task 2).
- Produces: `build_submission_workflow(..., bundles_per_submission: int | None = None)`, forwarding it to `DeqmSubmitDataWorkflow(group_size=...)`.

**Context:** `DeqmSubmitDataWorkflow.__init__` already accepts `group_size: int = 1` and already applies `max(1, group_size)`. This task only connects the wire. A `None` here means a legacy row created before Task 2's columns existed; it must resolve to 1.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_services_workflows.py`:

```python
class TestGroupSizeThreading:
    async def test_factory_passes_the_stored_value_to_the_workflow(self):
        with patch(
            "app.services.workflows.get_measure_canonical",
            AsyncMock(return_value="http://example.org/Measure/CMS999"),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="CMS999",
                mcs_url="http://mcs",
                mcs_auth_headers=None,
                submit_data_mode=SUBMIT_DATA_MODE_STU5,
                period_start="2025-01-01",
                period_end="2025-12-31",
                bundles_per_submission=20,
            )
        assert wf.submission_group_size == 20

    async def test_a_legacy_null_resolves_to_one(self):
        """Rows created before the column existed read as None. One subject per
        POST is what those jobs actually did, so that is what None must mean."""
        with patch(
            "app.services.workflows.get_measure_canonical",
            AsyncMock(return_value="http://example.org/Measure/CMS999"),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="CMS999",
                mcs_url="http://mcs",
                mcs_auth_headers=None,
                submit_data_mode=SUBMIT_DATA_MODE_STU5,
                period_start="2025-01-01",
                period_end="2025-12-31",
                bundles_per_submission=None,
            )
        assert wf.submission_group_size == 1

    async def test_direct_load_ignores_the_value_entirely(self):
        wf = await build_submission_workflow(
            workflow="direct_load",
            job_id=1,
            measure_id="CMS999",
            mcs_url="http://mcs",
            mcs_auth_headers=None,
            submit_data_mode=None,
            period_start="2025-01-01",
            period_end="2025-12-31",
            bundles_per_submission=50,
        )
        assert wf.submission_group_size == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestGroupSizeThreading -v`
Expected: FAIL — `TypeError: build_submission_workflow() got an unexpected keyword argument 'bundles_per_submission'`.

- [ ] **Step 3: Add the parameter**

In `backend/app/services/workflows.py`, add to `build_submission_workflow`'s signature after `period_end: str,`:

```python
    bundles_per_submission: int | None = None,
```

and pass it in the `DeqmSubmitDataWorkflow(...)` construction, after `mode=resolved_mode,`:

```python
            # None is a row created before #413 PR 3 added the column; those
            # jobs submitted one subject per POST, so that is what None means.
            group_size=bundles_per_submission or 1,
```

- [ ] **Step 4: Pass it from the orchestrator**

In `backend/app/services/orchestrator.py`, the `build_submission_workflow(...)` call at `:231` gains, after `period_end=job_period_end,`:

```python
            bundles_per_submission=job_bundles_per_submission,
```

Find where the sibling `job_*` locals are read off the `Job` row (search upward for `job_submit_data_mode =`) and add alongside them:

```python
        job_bundles_per_submission = job.bundles_per_submission
```

Match the exact local-variable style used by its neighbours.

- [ ] **Step 5: Add the orchestrator test**

Add to `backend/tests/test_services_orchestrator.py`, in the style of the existing factory-call assertions:

```python
async def test_the_jobs_stored_group_size_reaches_the_factory():
    """The column is inert unless the orchestrator actually forwards it."""
    with patch("app.services.orchestrator.build_submission_workflow", AsyncMock()) as factory:
        await _run_a_minimal_job(bundles_per_submission=7)
    assert factory.await_args.kwargs["bundles_per_submission"] == 7
```

Read the neighbouring orchestrator tests first and reuse whatever job-setup helper they use instead of inventing `_run_a_minimal_job` — if no such helper exists, extend the closest existing test's fixture rather than writing a new harness.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py tests/test_services_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/app/services/orchestrator.py backend/tests/test_services_workflows.py backend/tests/test_services_orchestrator.py
git commit -m "feat: thread the job's clamped group size into the DEQM workflow"
```

---

### Task 5: Narrow the pioneer's lock hold (finding M6)

**Files:**
- Modify: `backend/app/services/workflows.py` (`submit_prepared` at `:496`, `_settle_mode_and_submit` at `:551`)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Produces: `_Settlement(action: str, error: Exception | None)` — a module-private frozen dataclass in `workflows.py`.
- Produces: `async def _settle_mode(self, subjects: list[PreparedSubject]) -> _Settlement` and `async def _apply_settlement(self, settlement: _Settlement, subjects: list[PreparedSubject]) -> list[SubjectOutcome]`.
- Removes: `_settle_mode_and_submit`.

**Context — why this matters now:** `submit_prepared` holds `self._mode_lock` across the whole of `_settle_mode_and_submit`, whose downgrade and isolation paths each call `_submit_individually`, which POSTs once per subject *sequentially*. At group size 1 that is one or two round trips. Task 3 is what makes larger sizes reachable, so a pioneer group of 100 would hold the barrier across up to 101 round trips while the other three concurrent chunks block on it.

**The invariant you must not break:** `_mode` and `_downgraded` are written inside the lock, and `_mode_settled.set()` happens before the lock is released. Waiters therefore observe a fully decided mode. Only the *re-sends* move out. Keep `.set()` in the `finally`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_services_workflows.py`:

```python
class TestPioneerReleasesTheBarrierEarly:
    async def test_the_barrier_opens_after_the_pioneers_first_post_not_its_last(self):
        """Finding M6. The pioneer's re-sends must happen outside _mode_lock:
        a downgrading group of N otherwise blocks every other chunk for N
        sequential round trips, which is only reachable once operators can
        select N (#413 PR 3)."""
        wf = _stu5_workflow(group_size=3)
        posts: list[str] = []
        barrier_open_after: list[int] = []

        async def _post(parameters, mode):
            posts.append(mode)
            if len(posts) == 1:
                raise FhirOperationError(
                    operation="submit-data", url="http://mcs", status_code=404, outcome=None, latency_ms=1
                )
            # By the time a re-send runs, a waiter must already be admissible.
            if not wf._mode_lock.locked():
                barrier_open_after.append(len(posts))

        wf._post = AsyncMock(side_effect=_post)
        subjects = [_prepared(f"p{i}") for i in range(3)]
        outcomes = await wf.submit_prepared(subjects)

        assert len(outcomes) == 3
        assert posts[0] == SUBMIT_DATA_MODE_STU5
        assert posts[1:] == [SUBMIT_DATA_MODE_BASE] * 3
        # The lock was already free on the FIRST re-send, i.e. post #2.
        assert barrier_open_after and barrier_open_after[0] == 2

    async def test_the_mode_is_already_decided_when_the_barrier_opens(self):
        """#414 must survive the narrowing: self._mode and self._downgraded are
        written inside the lock, before the event is set, so no POST can ever
        observe an open barrier next to a half-decided mode."""
        wf = _stu5_workflow(group_size=2)
        calls: list[int] = []

        async def _post(parameters, mode):
            calls.append(len(calls))
            if len(calls) == 1:
                # The pioneer POST, still inside the lock, still undecided.
                raise FhirOperationError(
                    operation="submit-data", url="http://mcs", status_code=404, outcome=None, latency_ms=1
                )
            # Every later POST is a re-send running outside the lock. If the
            # barrier is open, the decision must already be visible and final.
            assert wf._mode_settled.is_set()
            assert wf.mode == SUBMIT_DATA_MODE_BASE
            assert wf.downgraded is True
            assert mode == SUBMIT_DATA_MODE_BASE

        wf._post = AsyncMock(side_effect=_post)
        outcomes = await wf.submit_prepared([_prepared("a"), _prepared("b")])

        assert len(calls) == 3  # one pioneer POST, then one re-send per subject
        assert len(outcomes) == 2
        assert all(o.error is None for o in outcomes)
        assert wf.mode == SUBMIT_DATA_MODE_BASE
        assert wf.downgraded is True
```

Reuse the module's existing helpers for building a workflow and a `PreparedSubject` — read the neighbouring `TestPioneerGroup` class and use exactly the helper names it uses, replacing `_stu5_workflow` / `_prepared` above with them.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestPioneerReleasesTheBarrierEarly -v`
Expected: FAIL on `barrier_open_after[0] == 2` — the list is empty, because today the lock is still held during every re-send.

- [ ] **Step 3: Add the settlement record**

In `backend/app/services/workflows.py`, beside the other module-private dataclasses:

```python
@dataclass(frozen=True)
class _Settlement:
    """What the pioneer's single POST decided, so the re-sends can happen
    outside the mode lock (finding M6).

    `action` is one of:
      "done"         — the group POST succeeded; every subject is good.
      "resend-base"  — a capability signal; the job downgraded and the group
                       must be re-sent one subject at a time in base mode.
      "isolate-stu5" — a payload rejection on a group of more than one; re-send
                       each subject alone under the settled STU5 mode.
      "fail"         — one verdict for the whole group; `error` carries it.
    """

    action: str
    error: Exception | None = None
```

- [ ] **Step 4: Split the method**

Replace `_settle_mode_and_submit` entirely with the two methods below. Every comment in the original body is load-bearing history — carry them across verbatim into `_settle_mode`.

```python
    async def _settle_mode(self, subjects: list[PreparedSubject]) -> _Settlement:
        """Decide the job's wire format with ONE POST. Runs under
        self._mode_lock with self._mode_settled unset, so it is the single
        point where the mode is decided (#414).

        Returns the decision rather than acting on it: the follow-up re-sends
        are N sequential POSTs, and holding the barrier across them would stall
        every other chunk for the whole group (finding M6). Writes to self._mode
        and self._downgraded happen HERE, inside the lock and before the caller
        sets the event, so a waiter can never observe a half-decided mode.
        """
        parameters = build_stu5_parameters([SubjectBundle(s.measure_report, s.resources) for s in subjects])
        try:
            await self._post(parameters, SUBMIT_DATA_MODE_STU5)
        except FhirOperationError as exc:
            # A mis-probed capability stamps Job.submit_data_mode="stu5" for a
            # server that doesn't actually implement the type-level $submit-data
            # bundle contract. Rather than fail every patient in the job,
            # downgrade to base mode and re-send this group individually.
            # Because this runs before the mode is settled, nobody has been
            # submitted under STU5 yet, so the downgrade cannot strand anyone in
            # the other format.
            #
            # A bare status is only trusted when it is a statement about the
            # server (_DOWNGRADE_STATUS_CODES). A 400 is ambiguous, so it
            # downgrades only when its OperationOutcome says the operation is
            # missing — otherwise it is a payload rejection and belongs to the
            # subjects, with the server's explanation preserved (#414).
            capability_signal = exc.status_code in _DOWNGRADE_STATUS_CODES or (
                exc.status_code == 400 and _outcome_reports_unsupported_operation(exc)
            )
            if capability_signal:
                logger.warning(
                    "STU5 $submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                    exc.status_code,
                    self._job_id,
                    extra={
                        "job_id": self._job_id,
                        "patient_id": subjects[0].patient_id,
                        "subject_count": len(subjects),
                        "status_code": exc.status_code,
                    },
                )
                self._mode = SUBMIT_DATA_MODE_BASE
                # Read by the orchestrator to persist Job.submit_data_mode, so
                # the Jobs badge reports the mode actually used rather than the
                # probe's verdict.
                self._downgraded = True
                # Capability first, isolation second, never both: base-fallback
                # has no multi-bundle form, so this is a re-send, not a retry.
                return _Settlement("resend-base")
            if len(subjects) > 1 and _is_payload_attributable(exc):
                return _Settlement("isolate-stu5")
            return _Settlement("fail", exc)
        except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
            return _Settlement("fail", exc)
        return _Settlement("done")

    async def _apply_settlement(
        self, settlement: _Settlement, subjects: list[PreparedSubject]
    ) -> list[SubjectOutcome]:
        """Carry out what _settle_mode decided, OUTSIDE the mode lock.

        Every branch returns exactly one outcome per subject — losing one here
        would silently drop a patient from the job's counters.
        """
        if settlement.action == "resend-base":
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
        if settlement.action == "isolate-stu5":
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_STU5)
        if settlement.action == "fail":
            return [
                SubjectOutcome(
                    patient_id=s.patient_id,
                    gather=s.gather,
                    error=TransferPhaseError("submit", settlement.error),
                )
                for s in subjects
            ]
        return [SubjectOutcome(patient_id=s.patient_id, gather=s.gather) for s in subjects]
```

- [ ] **Step 5: Restructure the caller**

In `submit_prepared`, replace the barrier block (currently `:506-516`) with:

```python
        if not self._mode_settled.is_set():
            settlement: _Settlement | None = None
            async with self._mode_lock:
                if not self._mode_settled.is_set():
                    try:
                        settlement = await self._settle_mode(subjects)
                    finally:
                        # In a finally: a pioneer group that fails outright must
                        # still release every group waiting on its verdict.
                        self._mode_settled.set()
            # Deliberately outside the `async with`: the re-sends are N
            # sequential POSTs, and holding the barrier across them would stall
            # every other chunk for the whole group (finding M6). The mode is
            # already decided and published at this point, so nothing a waiter
            # does can race it.
            if settlement is not None:
                return await self._apply_settlement(settlement, subjects)
            # Settled by the pioneer while we queued on the lock; fall through
            # and submit under whatever it decided.
```

Leave the two lines after the block (`if self._mode != SUBMIT_DATA_MODE_STU5: ...` / `return await self._submit_group(subjects)`) exactly as they are.

- [ ] **Step 6: Run the workflow suite**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS, including every pre-existing `TestPioneerGroup` and #414 settlement test untouched. If any pre-existing test fails, the narrowing changed observable behavior it should not have — fix the code, not the test.

- [ ] **Step 7: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "fix: release the mode barrier after the pioneer's first POST (M6)"
```

---

### Task 6: A runtime downgrade rewrites the stored group size

**Files:**
- Modify: `backend/app/services/orchestrator.py` (the settled-mode persistence block at `:998-1010`)
- Test: `backend/tests/test_services_orchestrator.py`

**Interfaces:**
- Consumes: `Job.bundles_per_submission` (Task 2), `workflow.mode` / `workflow.downgraded` (pre-existing).

**Context:** `submission_group_size` already collapses to 1 once the mode is not STU5, so the *behavior* is already correct. What is wrong without this task is the *record*: a job created at 20 that downgraded would keep claiming 20 while having submitted one subject per call. The existing block already rewrites `Job.submit_data_mode` in the same session for exactly this reason.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_services_orchestrator.py`, beside the existing `test_batch_persists_runtime_downgrade_to_job_submit_data_mode`:

```python
async def test_a_runtime_downgrade_rewrites_the_stored_group_size_to_one():
    """base-fallback has no multi-bundle form, so a downgraded job submitted one
    subject per call. Leaving the column at 20 would make the record claim
    something that never happened."""
    job = await _job_with(workflow="deqm_submit_data", submit_data_mode="stu5", bundles_per_submission=20)
    workflow = _downgraded_workflow()
    await _run_batch(job, workflow)
    refreshed = await _reload(job)
    assert refreshed.submit_data_mode == "base-fallback"
    assert refreshed.bundles_per_submission == 1
    assert refreshed.bundles_per_submission_requested == 20  # the request is NOT rewritten


async def test_a_job_that_does_not_downgrade_keeps_its_group_size():
    job = await _job_with(workflow="deqm_submit_data", submit_data_mode="stu5", bundles_per_submission=20)
    workflow = _stu5_workflow_that_settles()
    await _run_batch(job, workflow)
    assert (await _reload(job)).bundles_per_submission == 20
```

Read `test_batch_persists_runtime_downgrade_to_job_submit_data_mode` first and reuse its exact setup helpers; replace the `_job_with` / `_run_batch` / `_reload` placeholders above with whatever that test actually uses.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -k downgrade -v`
Expected: FAIL — `assert 20 == 1`.

- [ ] **Step 3: Extend the persistence block**

In `backend/app/services/orchestrator.py`, inside the `if settled_mode and job.submit_data_mode != settled_mode:` branch, after `job.submit_data_mode = settled_mode`:

```python
                        # base-fallback has no multi-bundle form, so a
                        # downgraded job submitted one subject per call no
                        # matter what was chosen. The requested column is
                        # deliberately NOT touched: it records what the operator
                        # asked for, and the creation form reads it back as the
                        # remembered preference.
                        if settled_mode != SUBMIT_DATA_MODE_STU5 and job.bundles_per_submission not in (None, 1):
                            job.bundles_per_submission = 1
```

Confirm `SUBMIT_DATA_MODE_STU5` is imported in `orchestrator.py`; if not, add it to the existing `from app.services.fhir_client import (...)` block.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/orchestrator.py backend/tests/test_services_orchestrator.py
git commit -m "fix: a downgraded job records the group size it actually used"
```

---

### Task 7: The job-creation form control

**Files:**
- Modify: `frontend/src/pages/JobsPage.js` (`formData` at `:58`, `handleCreateJob` at `:162`, the workflow field at `:509-516`)
- Modify: `frontend/src/pages/JobsPage.module.css` (add `.fieldHelp` beside `.labelHint` at `:430`)
- Test: `frontend/src/pages/JobsPage.workflow.test.js`

**Interfaces:**
- Consumes: `bundles_per_submission` and `bundles_per_submission_requested` on every job in `GET /api/jobs` (Task 2); `bundles_per_submission` on `POST /api/jobs` (Task 3).

**Context:** `GET /api/jobs` returns every job, newest first, unpaginated, and `JobsPage` already holds that list in its `jobs` state. The default is therefore derived from state already in memory — no new endpoint, no extra fetch. The helper text names the *rule*, not the number: `BATCH_SIZE` is a backend environment value the frontend has never been given, and a hardcoded `100` would go quietly wrong the first time it is retuned.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/pages/JobsPage.workflow.test.js`, inside the existing describe block:

```javascript
  test('the bundles input appears only on the DEQM branch', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    expect(screen.queryByLabelText(/Bundles per submission/i)).not.toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect(await screen.findByLabelText(/Bundles per submission/i)).toBeInTheDocument();
  });

  test('it defaults to 1 when no DEQM job exists', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('1');
  });

  test('it defaults to the most recent DEQM job\'s REQUESTED value, not its clamped one', async () => {
    // The whole point of storing two columns: a job clamped from 50 to 1 must
    // still offer 50, or one run against a max:1 server would ratchet the
    // operator's preference down permanently.
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [
        { ...BASE_JOB, id: 2, workflow: 'deqm_submit_data', bundles_per_submission: 1, bundles_per_submission_requested: 50 },
        { ...BASE_JOB, id: 1, workflow: 'deqm_submit_data', bundles_per_submission: 5, bundles_per_submission_requested: 5 },
      ],
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('50');
  });

  test('direct_load jobs never contribute a default', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [
        { ...BASE_JOB, id: 2, workflow: 'direct_load', bundles_per_submission: null, bundles_per_submission_requested: null },
        { ...BASE_JOB, id: 1, workflow: 'deqm_submit_data', bundles_per_submission: 8, bundles_per_submission_requested: 8 },
      ],
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('8');
  });

  test('0 is accepted and sent', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const input = await screen.findByLabelText(/Bundles per submission/i);
    await userEvent.clear(input);
    await userEvent.type(input, '0');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    await waitFor(() =>
      expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ bundles_per_submission: 0 }))
    );
  });

  test('direct_load sends no bundles value at all', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    const sent = api.createJob.mock.calls[0][0];
    expect(sent.bundles_per_submission).toBeUndefined();
  });

  test('the helper text states the rule without hardcoding the batch size', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const help = await screen.findByText(/0 submits every subject in a processing batch/i);
    expect(help).toBeInTheDocument();
    expect(help.textContent).not.toMatch(/\b100\b/);
  });

  test('a clamped creation reports the value the server actually accepted', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    api.createJob = jest.fn().mockResolvedValue({
      ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'stu5',
      bundles_per_submission: 1, bundles_per_submission_requested: 50,
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const input = await screen.findByLabelText(/Bundles per submission/i);
    await userEvent.clear(input);
    await userEvent.type(input, '50');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    expect(await screen.findByText(/reduced .* to 1/i)).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx jest src/pages/JobsPage.workflow.test.js`
Expected: FAIL — `Unable to find a label with the text of: /Bundles per submission/i`.

- [ ] **Step 3: Add the derived default**

In `frontend/src/pages/JobsPage.js`, add `bundles_per_submission: '1'` to the `formData` initial state at `:58`, then add a memo beside the other derived values (after the `useConnection()` line at `:65`):

```javascript
  // The remembered preference. GET /api/jobs already returns every job newest
  // first, and this page already holds that list — so the default costs no
  // endpoint and no second fetch. It reads the REQUESTED column, never the
  // clamped one: a job whose 50 was reduced to 1 by a max:1 server must still
  // offer 50, or one run would ratchet the preference down permanently.
  const rememberedBundles = useMemo(() => {
    const lastDeqm = jobs.find(
      j => j.workflow === 'deqm_submit_data' && j.bundles_per_submission_requested !== null
        && j.bundles_per_submission_requested !== undefined
    );
    return lastDeqm ? String(lastDeqm.bundles_per_submission_requested) : '1';
  }, [jobs]);
```

Add `useMemo` to the React import if it is not already there.

Then seed the field whenever the modal opens. The "New calculation" button at `:283` uses an inline handler with no named function, so change it from:

```javascript
        <button className={styles.btnPrimary} onClick={() => setShowModal(true)}>
```

to:

```javascript
        <button
          className={styles.btnPrimary}
          onClick={() => {
            // Seed on open, not on render: the operator may edit the field, and
            // re-deriving it underneath them would discard what they typed.
            setFormData(p => ({ ...p, bundles_per_submission: rememberedBundles }));
            setShowModal(true);
          }}
        >
```

If any other call site opens the same modal (`grep -n "setShowModal(true)" src/pages/JobsPage.js`), give it the same seeding.

- [ ] **Step 4: Render the control**

In `JobsPage.js`, directly after the closing `</div>` of the workflow-select field (`:516`):

```javascript
              {formData.workflow === 'deqm_submit_data' && (
                <div className={styles.field}>
                  <label className={styles.label} htmlFor="bundles-input">Bundles per submission</label>
                  <input
                    id="bundles-input"
                    type="number"
                    min="0"
                    step="1"
                    className={styles.input}
                    value={formData.bundles_per_submission}
                    onChange={e => setFormData(p => ({ ...p, bundles_per_submission: e.target.value }))}
                  />
                  <span className={styles.fieldHelp}>
                    How many subjects&rsquo; bundles ride in one $submit-data call.
                    0 submits every subject in a processing batch; larger values are reduced to the batch size.
                  </span>
                </div>
              )}
```

- [ ] **Step 5: Send it and report a clamp**

In `handleCreateJob`, add to the `createJob({...})` argument after `workflow: formData.workflow,`:

```javascript
        ...(formData.workflow === 'deqm_submit_data'
          ? { bundles_per_submission: Number(formData.bundles_per_submission) }
          : {}),
```

and after the existing `base-fallback` warning toast:

```javascript
      const requested = created?.bundles_per_submission_requested;
      const effective = created?.bundles_per_submission;
      if (requested > 0 && effective > 0 && effective < requested) {
        toast.warning(`Bundles per submission reduced from ${requested} to ${effective} — the server or the batch size is lower.`);
      }
```

- [ ] **Step 6: Add the help style**

In `frontend/src/pages/JobsPage.module.css`, after the `.labelHint` block:

```css
.fieldHelp {
  font-size: 11px;
  line-height: 1.45;
  color: var(--text-dim);
}
```

- [ ] **Step 7: Run the frontend tests**

Run: `cd frontend && npx jest src/pages/JobsPage.workflow.test.js`
Expected: PASS, including the six pre-existing tests in that file.

- [ ] **Step 8: Build and commit**

```bash
cd frontend && CI=true npm test -- --watchAll=false && npm run build
git add frontend/src/pages/JobsPage.js frontend/src/pages/JobsPage.module.css frontend/src/pages/JobsPage.workflow.test.js
git commit -m "feat: bundles-per-submission control on the DEQM job form"
```

---

### Task 8: Documentation

**Files:**
- Modify: `docs/architecture.md` (the DEQM submission section PR 2 extended)
- Modify: `HANDOFF.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Locate the section**

Run: `grep -n "submit_data_mode\|grouping\|submission group" docs/architecture.md` and read the surrounding 40 lines so the new text matches the file's voice and depth.

- [ ] **Step 2: Document the control**

Extend that section with:

```markdown
**Bundles per submission.** A DEQM job carries an operator-chosen group size:
how many subjects' bundles ride in one type-level `$submit-data` POST. The
request is clamped at job creation to `min(requested or BATCH_SIZE,
server bundle max, BATCH_SIZE)`, where the server's maximum comes from the
`bundle` input parameter of the OperationDefinition the capability probe
matched. Any clamp that reduces the request is logged with its reason.

Two columns record the outcome: `jobs.bundles_per_submission_requested` is what
the operator asked for and is what the creation form reads back as the
remembered preference, while `jobs.bundles_per_submission` is the clamped value
the job ran under. Keeping them apart is what stops a single job against a
`max: "1"` server from ratcheting the preference down permanently. Both are
NULL for `direct_load`, which has no submission-grouping concept.

A job that downgrades to `base-fallback` at runtime submits one subject per
call — base mode has no multi-bundle envelope — and its effective value is
rewritten to 1 alongside `submit_data_mode`, so the record reports what the job
actually did.
```

- [ ] **Step 3: Update the handoff note**

In `HANDOFF.md`, update the header commit/date and replace the "#413 PR 3 remaining" item with a line stating that PR 3 is implemented, naming this plan and the branch.

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md HANDOFF.md
git commit -m "docs: describe the bundles-per-submission control and its clamp"
```

---

## Final verification (run before opening the PR)

Per `CLAUDE.md`'s mandatory pre-push checklist — every step, no exceptions:

- [ ] **Lint:** `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`
- [ ] **Unit:** `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
- [ ] **Frontend:** `cd frontend && CI=true npm test -- --watchAll=false && npm run build`
- [ ] **CI-equivalent integration** (the `USE_PREBAKED=1 REQUIRE_PREBAKED=1` prefix is NOT optional — without it the script silently falls back to vanilla HAPI images with no FHIR Groups, and the run is not CI-equivalent no matter what the `--ignore` flags say):

```bash
USE_PREBAKED=1 REQUIRE_PREBAKED=1 ./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py \
  --ignore=tests/integration/test_groups_dropdown.py \
  --ignore=tests/integration/test_full_jobs_pipeline.py \
  --ignore=tests/integration/test_factory_reset.py
```

- [ ] **Full workflow** — this PR touches `orchestrator.py` and `workflows.py`, so the decision tree requires it:

```bash
./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py
```

- [ ] **The DEQM integration file** — `test_deqm_submit_data_workflow.py:187` asserts the probe records `base-fallback` against bundled HAPI `v8.8.0-1`. Task 1 changes that probe's return type; confirm the assertion still reads the mode correctly:

```bash
USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_deqm_submit_data_workflow.py
```

- [ ] **The ship-or-not gate:** if any of the above did not pass, do not push. Say what is blocking in the PR description instead.

## Notes for the reviewer

- **Real-server coverage is unchanged and still absent.** Bundled HAPI `v8.8.0-1` does not implement the type-level operation, so every STU5 path — including every group size above 1 — remains fixture-verified only. A HAPI 8.10.x bump is the follow-up that would genuinely exercise it.
- **The default group size in production stays 1** unless an operator changes it, so a deploy of this PR is behavior-neutral until someone uses the control.
- **Deviation from the spec's literal wording, approved during brainstorming:** the helper text states the ceiling as a rule rather than naming `BATCH_SIZE`, because the frontend has never been given that value and exposing it means the endpoint this design deliberately removed.
