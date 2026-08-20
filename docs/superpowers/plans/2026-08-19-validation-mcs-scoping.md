# Validation Pipeline MCS Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every measure-engine interaction in the validation pipeline target the MCS connection the user selected, instead of silently defaulting to Lenny's own container.

**Architecture:** Three entry points (`triage_test_bundle`, `run_validation`, `_reload_measures_from_seed_bundles`) resolve the target once into a frozen `McsTarget` value object and thread it down as a required parameter. No helper resolves a target itself, and no call relies on a defaulted `target_url`. `validation_runs` snapshots its MCS the way `Job` does, so a queued run executes against the server it was created for.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy 2.x (async), httpx, pytest + pytest-asyncio, Postgres (prod/integration) and SQLite (unit).

**Spec:** `docs/superpowers/specs/2026-08-19-validation-mcs-scoping-design.md`

> **Known defect in this plan's code samples — read before copying them.** The four
> `ExpectedResult(...)` literals below (lines ~1170, ~1362, ~1666, ~1707) omit
> `source_bundle`. That column is `NOT NULL` with no server default, so each one raises
> `IntegrityError` on commit. Add `source_bundle="<something>.json"` when you copy them.
> Three separate tasks hit this independently and fixed it the same way; the literals are
> left as written because this plan is a historical record of what was planned, not the
> shipped code.

## Global Constraints

- Python 3.10+; use `X | None`, never `Optional[X]`, in new code.
- Type hints required on every new function.
- No hardcoded URLs or credentials — everything through `backend/app/config.py`.
- Lint gate: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` must be clean before every commit.
- Unit tests run from `backend/`: `python3 -m pytest tests/ --ignore=tests/integration -q`.
- A fresh worktree has no venv. Prepend the main checkout's: `export PATH="/Users/bill/dev/bellese/mct2/backend/.venv/bin:$PATH"`.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).
- End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
  ```
- `settings.MEASURE_ENGINE_URL` is permitted ONLY as a legacy-NULL fallback (`run.mcs_url or settings.MEASURE_ENGINE_URL`). Never as a direct target.
- `tests/test_mcs_scoping_inventory.py` pins per-file env-var read counts. Any task that changes a count MUST update `EXPECTED_READS` in the same commit or the suite fails.
- Do NOT modify `TODOS.md` — frozen per CLAUDE.md.

---

### Task 1: `McsTarget` value object

**Files:**
- Modify: `backend/app/services/fhir_client.py` (add dataclass near the top, after the logger/constants block)
- Modify: `backend/app/dependencies.py` (add the bridge function)
- Test: `backend/tests/test_services_fhir_client.py`, `backend/tests/test_dependencies.py`

**Interfaces:**
- Consumes: `ConnectionContext` from `app/dependencies.py` (existing; already carries `mcs_url`, `auth_type`, `auth_credentials`, `is_read_only`, `wipe_before_job`).
- Produces:
  - `fhir_client.McsTarget(url: str, auth_headers: dict[str, str], is_read_only: bool, wipe_before_job: bool)` — frozen dataclass.
  - `dependencies.mcs_target_from_context(ctx: ConnectionContext) -> McsTarget` — async; resolves credentials via `_build_auth_headers`.

`McsTarget` lives in `fhir_client`, not `dependencies`: `dependencies` imports `fhir_client` for `_build_auth_headers`, so the reverse import would be a cycle.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_services_fhir_client.py`, append:

```python
# ---------------------------------------------------------------------------
# McsTarget (issue #397 slice 3)
# ---------------------------------------------------------------------------


def test_mcs_target_is_frozen():
    """Immutable on purpose: it is threaded through ~16 call sites, and a helper
    mutating the shared target would silently re-point every later call."""
    from dataclasses import FrozenInstanceError

    from app.services.fhir_client import McsTarget

    t = McsTarget(url="https://mcs.example.org/fhir", auth_headers={}, is_read_only=False, wipe_before_job=False)
    with pytest.raises(FrozenInstanceError):
        t.url = "https://elsewhere.example.org/fhir"  # type: ignore[misc]


def test_mcs_target_requires_every_field():
    """No defaults. A default target is exactly how issue #397 stayed invisible."""
    from app.services.fhir_client import McsTarget

    with pytest.raises(TypeError):
        McsTarget(url="https://mcs.example.org/fhir")  # type: ignore[call-arg]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && export PATH="/Users/bill/dev/bellese/mct2/backend/.venv/bin:$PATH"
python3 -m pytest tests/test_services_fhir_client.py -k mcs_target -v
```
Expected: FAIL with `ImportError: cannot import name 'McsTarget'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/fhir_client.py`, after the `_MAX_CONSECUTIVE_FAILURES` constant:

```python
@dataclass(frozen=True)
class McsTarget:
    """Which measure-calculation server a pipeline is working against (issue #397).

    Carries RESOLVED auth headers rather than raw credentials so no helper deep in
    the validation pipeline re-derives them, and carries the two connection flags so
    no helper re-queries the database.

    Frozen because it is threaded through roughly sixteen call sites: a helper
    mutating a shared target would silently re-point every call after it.

    Every field is required. The absence of a default is the point — the #397 bug
    class is precisely a target that defaults to something the caller did not mean.
    """

    url: str
    auth_headers: dict[str, str]
    is_read_only: bool
    wipe_before_job: bool
```

`dataclass` is already imported at the top of the file (`from dataclasses import dataclass, field`).

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && python3 -m pytest tests/test_services_fhir_client.py -k mcs_target -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing test for the bridge**

In `backend/tests/test_dependencies.py`, append:

```python
@pytest.mark.asyncio
async def test_mcs_target_from_context_resolves_credentials(test_session):
    """The bridge turns a ConnectionContext into a pipeline-ready target.

    Credentials are resolved once here so no validation helper re-derives them.
    """
    from app.dependencies import get_active_mcs, mcs_target_from_context
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-397"},
        is_active=True,
        name="Remote MCS",
        is_default=False,
        wipe_before_job=True,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_mcs(session=test_session)
    target = await mcs_target_from_context(ctx)

    assert target.url == "https://mcs.example.org/fhir"
    assert target.auth_headers == {"Authorization": "Bearer tok-397"}
    assert target.is_read_only is False
    assert target.wipe_before_job is True
```

- [ ] **Step 6: Run it to verify it fails**

```bash
cd backend && python3 -m pytest tests/test_dependencies.py -k mcs_target_from_context -v
```
Expected: FAIL with `ImportError: cannot import name 'mcs_target_from_context'`.

- [ ] **Step 7: Implement the bridge**

In `backend/app/dependencies.py`, after `resolve_job_mcs_auth_headers`:

```python
async def mcs_target_from_context(ctx: ConnectionContext) -> McsTarget:
    """Build a pipeline-ready `McsTarget` from an active-connection context.

    Lives here rather than on `McsTarget` itself because it needs
    `_build_auth_headers`, and `fhir_client` (where `McsTarget` is defined) must not
    import this module — `dependencies` already imports `fhir_client`, so the
    reverse direction would be an import cycle.
    """
    from app.services.fhir_client import McsTarget

    return McsTarget(
        url=ctx.mcs_url,
        auth_headers=await _build_auth_headers(ctx.auth_type, ctx.auth_credentials),
        is_read_only=ctx.is_read_only,
        wipe_before_job=ctx.wipe_before_job,
    )
```

The local `McsTarget` import above is sufficient — do not add a module-level one.
Keep `_build_auth_headers` as a LOCAL import inside each function that needs it
(controller ruling R2): `dependencies` importing `fhir_client` at module scope is
what the local imports exist to avoid, and consistency between the two functions
matters more than saving a line.

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd backend && python3 -m pytest tests/test_dependencies.py tests/test_services_fhir_client.py -q
```
Expected: PASS, no failures.

- [ ] **Step 9: Lint and commit**

```bash
cd backend && ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/services/fhir_client.py backend/app/dependencies.py backend/tests/test_services_fhir_client.py backend/tests/test_dependencies.py
git commit -m "$(cat <<'EOF'
feat(mcs): add McsTarget value object for pipeline scoping (#397)

A frozen dataclass naming which measure engine a pipeline works against, carrying
resolved auth headers plus the is_read_only and wipe_before_job flags so no helper
deep in the validation pipeline re-derives credentials or re-queries the database.

Every field is required. The absence of a default is the point: #397 is a bug class
whose entire mechanism is a target defaulting to something the caller did not mean.

Lives in fhir_client, not next to ConnectionContext, because dependencies imports
fhir_client and the reverse would be a cycle.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 2: Generalize MCS credential resolution

**Files:**
- Modify: `backend/app/dependencies.py:28-72` (the `resolve_job_mcs_auth_headers` body)
- Test: `backend/tests/test_dependencies.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `dependencies.resolve_mcs_auth_headers(session, *, mcs_id: int | None, mcs_url: str | None, mcs_auth_type: str | None, owner_label: str) -> dict[str, str]`. `resolve_job_mcs_auth_headers(session, job_id)` remains as a thin wrapper so slice 2's callers (`routes/jobs.py`, `routes/results.py`, `orchestrator._get_mcs_auth_headers`) are untouched.

Why: the existing helper does `session.get(Job, job_id)` and cannot serve a `ValidationRun`. Copying it for validation would duplicate the URL-drift guard that decides whether to hand a config's credentials to a snapshotted host — the exact rot ADR-013 warned about.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_dependencies.py`, append:

```python
@pytest.mark.asyncio
async def test_resolve_mcs_auth_headers_works_without_a_job_row(test_session):
    """The generalised form takes snapshot FIELDS, not a Job id (issue #397).

    ValidationRun needs the same URL-drift guard, and it is not a Job. Duplicating
    the guard for validation is what this generalisation avoids.
    """
    from app.dependencies import resolve_mcs_auth_headers
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-vr"},
        is_active=True,
        name="Remote MCS",
        is_default=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    headers = await resolve_mcs_auth_headers(
        test_session,
        mcs_id=cfg.id,
        mcs_url="https://mcs.example.org/fhir",
        mcs_auth_type="bearer",
        owner_label="validation run 7",
    )
    assert headers == {"Authorization": "Bearer tok-vr"}


@pytest.mark.asyncio
async def test_resolve_mcs_auth_headers_refuses_on_url_drift(test_session):
    """Credentials are never sent to a host the snapshot does not name."""
    from app.dependencies import resolve_mcs_auth_headers
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://moved.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-vr"},
        is_active=True,
        name="Moved MCS",
        is_default=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    with pytest.raises(RuntimeError, match="different server"):
        await resolve_mcs_auth_headers(
            test_session,
            mcs_id=cfg.id,
            mcs_url="https://original.example.org/fhir",
            mcs_auth_type="bearer",
            owner_label="validation run 7",
        )


@pytest.mark.asyncio
async def test_resolve_mcs_auth_headers_deleted_config_with_auth_raises(test_session):
    """A NULL mcs_id plus an auth-bearing snapshot means credentials are gone."""
    from app.dependencies import resolve_mcs_auth_headers

    with pytest.raises(RuntimeError, match="deleted after"):
        await resolve_mcs_auth_headers(
            test_session,
            mcs_id=None,
            mcs_url="https://mcs.example.org/fhir",
            mcs_auth_type="bearer",
            owner_label="validation run 7",
        )


@pytest.mark.asyncio
async def test_resolve_mcs_auth_headers_no_config_no_auth_returns_empty(test_session):
    """An unauthenticated target legitimately needs no headers."""
    from app.dependencies import resolve_mcs_auth_headers

    headers = await resolve_mcs_auth_headers(
        test_session, mcs_id=None, mcs_url=None, mcs_auth_type=None, owner_label="validation run 7"
    )
    assert headers == {}
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python3 -m pytest tests/test_dependencies.py -k resolve_mcs_auth_headers -v
```
Expected: FAIL with `ImportError: cannot import name 'resolve_mcs_auth_headers'`.

- [ ] **Step 3: Implement the generalisation**

Replace the body of `resolve_job_mcs_auth_headers` in `backend/app/dependencies.py` with a wrapper, and add the generalised function above it:

```python
async def resolve_mcs_auth_headers(
    session: AsyncSession,
    *,
    mcs_id: int | None,
    mcs_url: str | None,
    mcs_auth_type: str | None,
    owner_label: str,
) -> dict[str, str]:
    """Resolve MCS credentials from the live config for any snapshot-carrying row.

    Takes the three snapshot FIELDS rather than a row id, so both `Job` and
    `ValidationRun` use one implementation (issue #397 slice 3). `owner_label`
    (e.g. "job 12", "validation run 4") only shapes the error messages.

    `mcs_id` is ON DELETE SET NULL on both tables, so a NULL id means EITHER "this
    row never had an MCS config" OR "the config was deleted after creation". The
    snapshotted `mcs_auth_type` is what tells them apart: without that check, a row
    whose connection was deleted would run unauthenticated against the
    still-snapshotted URL.
    """
    from app.services.fhir_client import _build_auth_headers

    if mcs_id is None:
        if not mcs_auth_type or mcs_auth_type == "none":
            return {}
        raise RuntimeError(
            f"{owner_label} has no mcs_id — MCS config was deleted after creation. "
            "Cannot fetch auth credentials."
        )
    cfg = await session.get(MCSConfig, mcs_id)
    if cfg is None:
        # Defensive: unreachable under the ON DELETE SET NULL FK, but a database
        # without the constraint enforced would land here.
        raise RuntimeError(f"MCS config {mcs_id} referenced by {owner_label} no longer exists.")
    if mcs_url and cfg.mcs_url != mcs_url:
        # The URL comes from the snapshot but credentials are read live, so a config
        # repointed at a different host would hand the new host's token to the old one.
        raise RuntimeError(
            f"MCS config {mcs_id} now points at a different server than {owner_label} "
            "was created against. Refusing to send its credentials to the snapshotted URL."
        )
    return await _build_auth_headers(cfg.auth_type, cfg.auth_credentials)


async def resolve_job_mcs_auth_headers(session: AsyncSession, job_id: int) -> dict[str, str]:
    """Job-shaped wrapper over `resolve_mcs_auth_headers`.

    Kept so slice 2's call sites (routes/jobs.py, routes/results.py,
    orchestrator._get_mcs_auth_headers) are unchanged.
    """
    from app.models.job import Job

    job = await session.get(Job, job_id)
    if job is None:
        return {}
    return await resolve_mcs_auth_headers(
        session,
        mcs_id=job.mcs_id,
        mcs_url=job.mcs_url,
        mcs_auth_type=job.mcs_auth_type,
        owner_label=f"Job {job_id}",
    )
```

- [ ] **Step 4: Run the new AND the inherited tests**

```bash
cd backend && python3 -m pytest tests/test_dependencies.py tests/test_services_orchestrator.py tests/test_routes_jobs.py tests/test_routes_results.py -q
```
Expected: PASS. The pre-existing `Job` tests passing unchanged is the proof the wrapper preserved slice 2's behavior — if any fail, the generalisation changed semantics and must be fixed, not the tests.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/dependencies.py backend/tests/test_dependencies.py
git commit -m "$(cat <<'EOF'
refactor(deps): generalise MCS credential resolution beyond Job (#397)

resolve_job_mcs_auth_headers did session.get(Job, job_id), so it could not serve a
ValidationRun. It now delegates to resolve_mcs_auth_headers, which takes the three
snapshot fields instead of a row id.

The point is that the URL-drift guard — which decides whether to hand a config's
credentials to a snapshotted host — stays in exactly one place. A second copy for
validation is the rot ADR-013 called out.

The pre-existing Job tests pass unchanged, which is the evidence the wrapper
preserved slice 2's behavior.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 3: Thread the target through the terminology helpers

**Files:**
- Modify: `backend/app/services/validation.py:558-609` (`_find_existing_valueset_id`, `_find_existing_codesystem_id`, `_delete_existing_valueset`)
- Modify: `backend/app/services/validation.py:611-690` (`_prepare_measure_support_resources` — signature and the four call sites at 635, 648, 661, 673)
- Modify: `backend/tests/test_mcs_scoping_inventory.py` (lower `app/services/validation.py` 9 → 6)
- Test: `backend/tests/test_services_validation.py`

**Interfaces:**
- Consumes: `McsTarget` (Task 1).
- Produces:
  - `_find_existing_valueset_id(url: str, client: httpx.AsyncClient, *, mcs: McsTarget) -> str | None` — the old `target_url` kwarg is REMOVED, not supplemented.
  - `_find_existing_codesystem_id(url: str, version: str | None, client: httpx.AsyncClient, *, mcs: McsTarget) -> str | None`
  - `_delete_existing_valueset(existing_id: str, client: httpx.AsyncClient, *, mcs: McsTarget) -> None`
  - `_prepare_measure_support_resources(resources, bundle_json, *, mcs: McsTarget) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_services_validation.py`, append:

```python
# ---------------------------------------------------------------------------
# Terminology helpers follow the given MCS (issue #397 slice 3)
# ---------------------------------------------------------------------------


def _mcs(url: str = "https://mcs.example.org/fhir", headers: dict | None = None):
    from app.services.fhir_client import McsTarget

    return McsTarget(
        url=url,
        auth_headers=headers if headers is not None else {"Authorization": "Bearer tok-397"},
        is_read_only=False,
        wipe_before_job=False,
    )


@pytest.mark.asyncio
async def test_find_existing_valueset_id_uses_the_given_mcs():
    from app.services.validation import _find_existing_valueset_id

    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"entry": []}, request=httpx.Request("GET", url))

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)

    await _find_existing_valueset_id("http://vs.example/1", client, mcs=_mcs())

    assert seen["url"] == "https://mcs.example.org/fhir/ValueSet"
    assert seen["headers"]["Authorization"] == "Bearer tok-397"


@pytest.mark.asyncio
async def test_find_existing_codesystem_id_uses_the_given_mcs():
    from app.services.validation import _find_existing_codesystem_id

    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"entry": []}, request=httpx.Request("GET", url))

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)

    await _find_existing_codesystem_id("http://cs.example/1", "1.0.0", client, mcs=_mcs())

    assert seen["url"] == "https://mcs.example.org/fhir/CodeSystem"
    assert seen["headers"]["Authorization"] == "Bearer tok-397"


@pytest.mark.asyncio
async def test_delete_existing_valueset_uses_the_given_mcs():
    """Destructive: this deletes a ValueSet, so on a shared engine it removes
    another participant's terminology. It must never guess its target."""
    from app.services.validation import _delete_existing_valueset

    seen: dict[str, object] = {}

    async def _delete(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={}, request=httpx.Request("DELETE", url))

    client = AsyncMock()
    client.delete = AsyncMock(side_effect=_delete)

    await _delete_existing_valueset("vs-1", client, mcs=_mcs())

    assert seen["url"] == "https://mcs.example.org/fhir/ValueSet/vs-1"
    assert seen["headers"]["Authorization"] == "Bearer tok-397"


@pytest.mark.asyncio
async def test_terminology_helpers_require_an_mcs():
    """No default target — the #397 mechanism is a target the caller did not choose."""
    from app.services.validation import (
        _delete_existing_valueset,
        _find_existing_codesystem_id,
        _find_existing_valueset_id,
    )

    client = AsyncMock()
    with pytest.raises(TypeError):
        await _find_existing_valueset_id("http://vs.example/1", client)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await _find_existing_codesystem_id("http://cs.example/1", None, client)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await _delete_existing_valueset("vs-1", client)  # type: ignore[call-arg]
```

Ensure `import httpx` and `from unittest.mock import AsyncMock` are present at the top of `test_services_validation.py`; add them if missing.

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -k "given_mcs or require_an_mcs" -v
```
Expected: FAIL — `TypeError: ... unexpected keyword argument 'mcs'`.

- [ ] **Step 3: Implement — the three helpers**

Replace `backend/app/services/validation.py` lines 558-609 with:

```python
async def _find_existing_valueset_id(
    url: str,
    client: httpx.AsyncClient,
    *,
    mcs: McsTarget,
) -> str | None:
    """Return the existing ValueSet resource ID for a canonical URL on `mcs`."""
    resp = await client.get(
        f"{mcs.url}/ValueSet",
        params={"url": url, "_count": 1, "_elements": "id,url"},
        headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json", **mcs.auth_headers},
    )
    resp.raise_for_status()
    entries = resp.json().get("entry", [])
    if not entries:
        return None
    return entries[0].get("resource", {}).get("id")


async def _find_existing_codesystem_id(
    url: str,
    version: str | None,
    client: httpx.AsyncClient,
    *,
    mcs: McsTarget,
) -> str | None:
    """Return the existing CodeSystem resource ID for a canonical URL/version on `mcs`."""
    params: dict[str, str | int] = {"url": url, "_count": 50, "_elements": "id,url,version"}
    if version:
        params["version"] = version
    resp = await client.get(
        f"{mcs.url}/CodeSystem",
        params=params,
        headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json", **mcs.auth_headers},
    )
    resp.raise_for_status()
    entries = resp.json().get("entry", [])
    for entry in entries:
        resource = entry.get("resource", {})
        resource_version = resource.get("version")
        if (version and resource_version == version) or (not version and not resource_version):
            return resource.get("id")
    return None


async def _delete_existing_valueset(existing_id: str, client: httpx.AsyncClient, *, mcs: McsTarget) -> None:
    """Delete a stale ValueSet on `mcs` so HAPI rebuilds terminology from patched compose.

    DESTRUCTIVE against whatever server `mcs` names: on a shared engine this removes
    another participant's terminology, which is why the target is required (#397).
    """
    resp = await client.delete(f"{mcs.url}/ValueSet/{existing_id}", headers=mcs.auth_headers)
    # 409 = referential integrity: other resources (Measure/Library) already reference this
    # ValueSet and HAPI won't delete it.  Treat as a no-op — push_resources will PUT the same
    # content to the existing ID and HAPI will accept it with a 200 update (issue #359).
    if resp.status_code not in {200, 204, 404, 409}:
        resp.raise_for_status()
```

Add `McsTarget` to the `from app.services.fhir_client import (...)` block at the top of `validation.py`.

- [ ] **Step 4: Implement — thread through `_prepare_measure_support_resources`**

Change its signature (line 611) to:

```python
async def _prepare_measure_support_resources(
    resources: list[dict[str, Any]],
    bundle_json: dict[str, Any],
    *,
    mcs: McsTarget,
) -> list[dict[str, Any]]:
```

Then update the four call sites inside it:
- line ~635: `if await _find_existing_valueset_id(url, client, mcs=mcs):`
- line ~648: add `mcs=mcs` to the `_find_existing_codesystem_id(...)` call
- line ~661: `existing_id = await _find_existing_valueset_id(resource["url"], client, mcs=mcs)`
- line ~673: `await _delete_existing_valueset(existing_id, client, mcs=mcs)`

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -q
```
Expected: the four NEW tests PASS. Pre-existing tests that call
`_prepare_measure_support_resources` DIRECTLY will fail with a missing `mcs` — update
each to pass `mcs=_mcs()`.

EXPECTED INTERIM BREAKAGE, do not "fix" it here: tests that reach that helper
INDIRECTLY through `triage_test_bundle` (the `TestTriageTestBundle` group, ~11 tests)
will also fail, because `triage_test_bundle` does not thread `mcs` until Task 5.
Leave them red. Adding a default to the signature, or constructing a stopgap
`McsTarget` inside `triage_test_bundle`, would reintroduce the exact env-var-default
bug class this change removes — and would collide with Task 5's edits to those same
lines. The suite goes green at Task 8 Step 5.

- [ ] **Step 6: Update the inventory guard**

In `backend/tests/test_mcs_scoping_inventory.py`, change `"app/services/validation.py"` from `9` to `6` and add to the comment block:

```
# CLEARED BY SLICE 3 (task 3): validation.py 9 -> 6. The three terminology helpers
# (_find_existing_valueset_id, _find_existing_codesystem_id, _delete_existing_valueset)
# now take a required McsTarget.
```

- [ ] **Step 7: Verify the guard and lint**

```bash
cd backend && python3 -m pytest tests/test_mcs_scoping_inventory.py -q
ruff format app/ tests/ && ruff check app/ tests/
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/validation.py backend/tests/test_services_validation.py backend/tests/test_mcs_scoping_inventory.py
git commit -m "$(cat <<'EOF'
fix(validation): scope the terminology helpers to a given MCS (#397)

_find_existing_valueset_id, _find_existing_codesystem_id and
_delete_existing_valueset read settings.MEASURE_ENGINE_URL, so they searched and
DELETED terminology on Lenny's local container regardless of the connection in use.
_delete_existing_valueset is destructive: on a shared engine it removes another
participant's ValueSets.

All three now take a required McsTarget and send its credentials.
_find_existing_valueset_id's partial target_url kwarg is removed rather than kept,
so there is one way to say where to look.

Inventory guard: validation.py 9 -> 6.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 4: Thread the target through the measure-resolution helpers

**Files:**
- Modify: `backend/app/services/validation.py:698-740` (`_assert_no_canonical_url_clash`)
- Modify: `backend/app/services/validation.py:973-1012` (`_resolve_measure_id`)
- Modify: `backend/tests/test_mcs_scoping_inventory.py` (validation.py 6 → 3)
- Test: `backend/tests/test_services_validation.py`

**Interfaces:**
- Consumes: `McsTarget` (Task 1), `_mcs()` test helper (Task 3).
- Produces:
  - `_assert_no_canonical_url_clash(measures: list[dict[str, Any]], *, mcs: McsTarget) -> None`
  - `_resolve_measure_id(measure_url: str, *, mcs: McsTarget) -> str | None`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_validation.py`:

```python
@pytest.mark.asyncio
async def test_assert_no_canonical_url_clash_probes_the_given_mcs():
    """The clash probe must ask the server the bundle is being pushed to.

    Probing a different server means it either misses a real clash or invents one.
    """
    from app.services.validation import _assert_no_canonical_url_clash

    seen: list[str] = []

    async def _get(url, **kwargs):
        seen.append(url)
        return httpx.Response(200, json={"entry": []}, request=httpx.Request("GET", url))

    with patch("app.services.validation.httpx.AsyncClient") as mock_httpx:
        ctx = AsyncMock()
        ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await _assert_no_canonical_url_clash(
            [{"resourceType": "Measure", "url": "http://cms.gov/M1", "id": "m1"}], mcs=_mcs()
        )

    assert seen == ["https://mcs.example.org/fhir/Measure"]


@pytest.mark.asyncio
async def test_resolve_measure_id_uses_the_given_mcs_by_relative_ref():
    from app.services.validation import _resolve_measure_id

    seen: list[str] = []

    async def _get(url, **kwargs):
        seen.append(url)
        return httpx.Response(200, json={"id": "m1"}, request=httpx.Request("GET", url))

    with patch("app.services.validation.httpx.AsyncClient") as mock_httpx:
        ctx = AsyncMock()
        ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _resolve_measure_id("Measure/m1", mcs=_mcs())

    assert result == "m1"
    assert seen == ["https://mcs.example.org/fhir/Measure/m1"]


@pytest.mark.asyncio
async def test_resolve_measure_id_uses_the_given_mcs_by_canonical_url():
    from app.services.validation import _resolve_measure_id

    seen: list[str] = []

    async def _get(url, **kwargs):
        seen.append(url)
        return httpx.Response(
            200,
            json={"entry": [{"resource": {"id": "m1"}}]},
            request=httpx.Request("GET", url),
        )

    with patch("app.services.validation.httpx.AsyncClient") as mock_httpx:
        ctx = AsyncMock()
        ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _resolve_measure_id("http://cms.gov/M1", mcs=_mcs())

    assert result == "m1"
    assert seen == ["https://mcs.example.org/fhir/Measure"]


@pytest.mark.asyncio
async def test_measure_resolution_helpers_require_an_mcs():
    from app.services.validation import _assert_no_canonical_url_clash, _resolve_measure_id

    with pytest.raises(TypeError):
        await _assert_no_canonical_url_clash([])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await _resolve_measure_id("Measure/m1")  # type: ignore[call-arg]
```

Ensure `from unittest.mock import AsyncMock, patch` is imported in the test module.

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -k "given_mcs or require_an_mcs" -v
```
Expected: FAIL — unexpected keyword argument `mcs`.

- [ ] **Step 3: Implement**

In `_assert_no_canonical_url_clash`, change the signature to:

```python
async def _assert_no_canonical_url_clash(measures: list[dict[str, Any]], *, mcs: McsTarget) -> None:
```

and the probe call to:

```python
                resp = await client.get(
                    f"{mcs.url}/Measure",
                    params={"url": canonical_url, "_elements": "id,url", "_count": "2"},
                    headers=mcs.auth_headers,
                )
```

In `_resolve_measure_id`, change the signature to:

```python
async def _resolve_measure_id(measure_url: str, *, mcs: McsTarget) -> str | None:
```

(note: return type becomes `str | None` per the Global Constraints — `Optional` is not used in new code), then:

- the relative-reference branch: `resp = await client.get(f"{mcs.url}/Measure/{parts[1]}", headers=mcs.auth_headers)`
- the canonical-URL branch: `headers = {"Cache-Control": "no-cache", "Accept": "application/fhir+json", **mcs.auth_headers}` and `resp = await client.get(f"{mcs.url}/Measure", params=params, headers=headers)`

- [ ] **Step 4: Update the two internal call sites**

- line ~778 in `triage_test_bundle`: `await _assert_no_canonical_url_clash(primary, mcs=mcs)`
- line ~1062 in `_reload_measures_from_seed_bundles`: `await _assert_no_canonical_url_clash(primary, mcs=mcs)`
- lines ~1143 and ~1171 in `run_validation`: `hapi_id = await _resolve_measure_id(measure_url, mcs=mcs)`

These reference an `mcs` variable that Tasks 5 and 7 introduce. If this task is executed before those, the module will not import — that is expected and correct: Tasks 3-8 form one coherent change. Run the full suite only at the end of Task 8. To keep this task independently committable, do Steps 1-3 and this step together, then proceed directly to Task 5.

- [ ] **Step 5: Update the inventory guard**

`"app/services/validation.py"`: `6` → `3`, and extend the comment block:

```
# CLEARED BY SLICE 3 (task 4): validation.py 6 -> 3. _assert_no_canonical_url_clash
# and _resolve_measure_id now take a required McsTarget. The remaining 3 are the
# valueset-expansion call (878), the wipe (1239), and the patient-name lookup (1359),
# cleared in tasks 7 and 8.
```

- [ ] **Step 6: Commit**

```bash
cd backend && ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/services/validation.py backend/tests/test_services_validation.py backend/tests/test_mcs_scoping_inventory.py
git commit -m "$(cat <<'EOF'
fix(validation): scope measure resolution and clash probe to a given MCS (#397)

_assert_no_canonical_url_clash probed the local engine for canonical-URL clashes
while the bundle was pushed elsewhere, so it either missed a real clash or invented
one. _resolve_measure_id looked up measure ids on the local engine for a run
targeting a different server.

Both now take a required McsTarget. _resolve_measure_id's return type moves from
Optional[str] to str | None per project convention.

Inventory guard: validation.py 6 -> 3.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 5: `triage_test_bundle` resolves and threads the target

**Files:**
- Modify: `backend/app/services/validation.py:746-800` (signature, read-only refusal, explicit push targets at 776/779, valueset expansion at 878)
- Modify: `backend/app/services/validation.py:1032-1070` (`_reload_measures_from_seed_bundles` — takes and forwards the target, explicit pushes at 1060/1063)
- Modify: `backend/app/services/validation.py:930` (upload caller)
- Modify: `backend/app/services/bundle_loader.py:88` (seed caller)
- Modify: `backend/tests/test_mcs_scoping_inventory.py` (validation.py 3 → 2)
- Test: `backend/tests/test_services_validation.py`, `backend/tests/test_services_bundle_loader.py`

**Interfaces:**
- Consumes: `McsTarget` (Task 1), `mcs_target_from_context` (Task 1), helpers from Tasks 3-4.
- Produces:
  - `triage_test_bundle(bundle_json, filename, session, *, mcs: McsTarget, progress_fn=None) -> dict[str, Any]`
  - `_reload_measures_from_seed_bundles(*, mcs: McsTarget) -> dict[str, int]`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_triage_test_bundle_refuses_a_read_only_mcs(test_session):
    """A validation upload must write Measures/Libraries/ValueSets, so a read-only
    target cannot work. Refuse before writing anything rather than partway through."""
    from app.services.fhir_client import McsTarget
    from app.services.validation import triage_test_bundle

    read_only = McsTarget(
        url="https://shared.example.org/fhir", auth_headers={}, is_read_only=True, wipe_before_job=False
    )
    bundle = {"resourceType": "Bundle", "entry": []}

    with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
        with pytest.raises(ValueError, match="read-only"):
            await triage_test_bundle(bundle, "b.json", test_session, mcs=read_only)

    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_test_bundle_pushes_measures_to_the_given_mcs(test_session):
    """Every measure-engine write names its target explicitly.

    These push_resources calls previously passed no target_url and silently used the
    env-var default — invisible to the AST inventory guard.
    """
    from app.services.validation import triage_test_bundle

    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": "m1", "url": "http://cms.gov/M1"}},
            {"resource": {"resourceType": "Library", "id": "l1"}},
        ],
    }

    with (
        patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push,
        patch("app.services.validation._assert_no_canonical_url_clash", new_callable=AsyncMock),
        patch("app.services.validation._prepare_measure_support_resources", new_callable=AsyncMock, return_value=[]),
        patch("app.services.validation.wait_for_valueset_expansion", return_value=[]),
    ):
        await triage_test_bundle(bundle, "b.json", test_session, mcs=_mcs())

    assert mock_push.await_count >= 1
    for call in mock_push.await_args_list:
        assert call.kwargs.get("target_url") == "https://mcs.example.org/fhir", call.kwargs
        assert call.kwargs.get("auth_headers") == {"Authorization": "Bearer tok-397"}


@pytest.mark.asyncio
async def test_triage_test_bundle_requires_an_mcs(test_session):
    from app.services.validation import triage_test_bundle

    with pytest.raises(TypeError):
        await triage_test_bundle({"resourceType": "Bundle", "entry": []}, "b.json", test_session)  # type: ignore[call-arg]
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -k triage_test_bundle -v
```
Expected: FAIL — unexpected keyword `mcs`.

- [ ] **Step 3: Implement the signature and the refusal**

```python
async def triage_test_bundle(
    bundle_json: dict[str, Any],
    filename: str,
    session: AsyncSession,
    *,
    mcs: McsTarget,
    progress_fn: Callable[[str, int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
```

As the FIRST statement in the body, before any network call or DB write:

```python
    # A test bundle upload writes Measures, Libraries and ValueSets to the measure
    # engine, so a read-only target cannot work. Refuse up front rather than partway
    # through, having already written some resources (issue #397). Mirrors the CDR
    # read-only check further down this same function.
    if mcs.is_read_only:
        raise ValueError(
            f"Cannot upload measures: the active measure engine connection is configured "
            f"as read-only. Switch to a writable connection to load test bundles."
        )
```

- [ ] **Step 4: Make the two measure-engine pushes explicit**

Lines ~776 and ~779 become:

```python
            support_resources = await _prepare_measure_support_resources(secondary, bundle_json, mcs=mcs)
            if support_resources:
                await push_resources(support_resources, target_url=mcs.url, auth_headers=mcs.auth_headers)
            if primary:
                await _assert_no_canonical_url_clash(primary, mcs=mcs)
                await push_resources(primary, target_url=mcs.url, auth_headers=mcs.auth_headers)
```

- [ ] **Step 5: Scope the valueset expansion**

Line ~878 becomes:

```python
    expanded = await asyncio.to_thread(wait_for_valueset_expansion, mcs.url, valueset_urls)
```

If `wait_for_valueset_expansion` takes no auth argument, leave it URL-only and add this comment above the call:

```python
    # URL only: wait_for_valueset_expansion is a synchronous polling helper with no
    # auth parameter. Against an authenticated remote MCS its polls will 401 and it
    # will report "not expanded" rather than failing — acceptable because the caller
    # treats a non-expansion as a warning, not an error. Threading auth through it is
    # follow-up work, tracked in the PR body, not silently assumed done.
```

- [ ] **Step 6: Thread through `_reload_measures_from_seed_bundles`**

```python
async def _reload_measures_from_seed_bundles(*, mcs: McsTarget) -> dict[str, int]:
```

and inside it, lines ~1060/1062/1063:

```python
                    support_resources = await _prepare_measure_support_resources(secondary, bundle_json, mcs=mcs)
                    if support_resources:
                        await push_resources(support_resources, target_url=mcs.url, auth_headers=mcs.auth_headers)
                    if primary:
                        await _assert_no_canonical_url_clash(primary, mcs=mcs)
                        await push_resources(primary, target_url=mcs.url, auth_headers=mcs.auth_headers)
```

- [ ] **Step 7: Update the two external callers**

`backend/app/services/validation.py:930` (the upload path) — resolve the active MCS from the session it already has:

```python
            from app.dependencies import get_active_mcs, mcs_target_from_context

            mcs = await mcs_target_from_context(await get_active_mcs(session=session))
            summary = await triage_test_bundle(bundle_json, upload.filename, session, mcs=mcs, progress_fn=_on_progress)
```

`backend/app/services/bundle_loader.py:88` (the boot seed path) — seeding targets Lenny's OWN engine by intent, so build the target explicitly from settings rather than inheriting a default:

```python
                # Seeding deliberately targets Lenny's own measure engine: this runs at
                # boot, before any MCS connection row is guaranteed to exist. Building
                # the target explicitly from settings makes that intent visible instead
                # of relying on a defaulted parameter (issue #397).
                seed_mcs = McsTarget(
                    url=settings.MEASURE_ENGINE_URL,
                    auth_headers={},
                    is_read_only=False,
                    wipe_before_job=False,
                )
                summary = await triage_test_bundle(bundle_json, bundle_path.name, session, mcs=seed_mcs)
```

Add `from app.services.fhir_client import McsTarget` to `bundle_loader.py`'s imports.

- [ ] **Step 8: Run tests**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py tests/test_services_bundle_loader.py -q
```
Expected: PASS. Existing callers of `triage_test_bundle` in tests need `mcs=_mcs()` added; do not add a parameter default.

- [ ] **Step 9: Update the inventory guard, lint, commit**

`"app/services/validation.py"`: `3` → `2`. `bundle_loader.py` stays at `1` (its `_wait_for_hapi` boot probe is legitimate) plus gains one for the explicit seed target — verify the actual count by running the guard and set the number to what it reports, then record the reason in the comment block.

```bash
cd backend && python3 -m pytest tests/test_mcs_scoping_inventory.py -q
ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/services/validation.py backend/app/services/bundle_loader.py backend/tests/
git commit -m "$(cat <<'EOF'
fix(validation): triage_test_bundle resolves and threads its MCS target (#397)

The bundle-upload entry point now resolves the active MCS once and threads an
McsTarget down, and its two measure-engine pushes name target_url explicitly. Those
pushes previously passed no target and relied on push_resources' back-compat
default — an unscoped write invisible to the AST inventory guard.

Refuses a read-only target up front. An upload writes Measures, Libraries and
ValueSets, so it cannot work read-only, and failing before the first write is better
than failing halfway with resources already pushed.

The boot seed path in bundle_loader now builds its target explicitly from settings.
Seeding SHOULD hit Lenny's own engine; the change makes that intent visible rather
than accidental.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 6: `validation_runs` MCS snapshot and migration

**Files:**
- Modify: `backend/app/models/validation.py:66-86` (`ValidationRun`)
- Modify: `backend/app/main.py` (migrations, in the `jobs` ALTER list region)
- Modify: `backend/app/routes/validation.py:200-206` (populate the snapshot)
- Test: `backend/tests/test_routes_validation.py`, `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ValidationRun.mcs_id`, `.mcs_url`, `.mcs_name`, `.mcs_auth_type`, `.mcs_wipe_before_job`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_routes_validation.py`:

```python
@pytest.mark.asyncio
async def test_start_validation_snapshots_the_active_mcs(client, test_session):
    """A validation result is a correctness claim; it needs provenance (issue #397).

    Without the snapshot, switching connections makes last week's "33/33 passed"
    describe a server nobody can identify.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.models.validation import ExpectedResult, ValidationRun

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-vr"},
        is_active=True,
        wipe_before_job=True,
    )
    test_session.add(cfg)
    test_session.add(
        ExpectedResult(
            measure_url="http://cms.gov/M1",
            patient_ref="p1",
            expected_populations={"initial-population": 1},
            period_start="2024-01-01",
            period_end="2024-12-31",
        )
    )
    await test_session.commit()
    await test_session.refresh(cfg)

    resp = await client.post("/validation/runs", json={})
    assert resp.status_code in (200, 201), resp.text
    run_id = resp.json()["id"]

    run = (await test_session.execute(sa_select(ValidationRun).where(ValidationRun.id == run_id))).scalar_one()
    assert run.mcs_url == "https://mcs.example.org/fhir"
    assert run.mcs_id == cfg.id
    assert run.mcs_name == "Remote MCS"
    assert run.mcs_auth_type == "bearer"
    assert run.mcs_wipe_before_job is True
```

Confirm the route path and payload against `backend/app/routes/validation.py` before running; adjust the URL if it differs.

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python3 -m pytest tests/test_routes_validation.py -k snapshots_the_active_mcs -v
```
Expected: FAIL — `AttributeError: 'ValidationRun' object has no attribute 'mcs_url'`.

- [ ] **Step 3: Add the columns**

In `backend/app/models/validation.py`, inside `ValidationRun` after `delete_requested`:

```python
    # MCS connection snapshot — populated at run creation from the active MCS
    # (issue #397). A validation result is a claim about correctness; without this,
    # the claim has no recorded provenance. Mirrors Job's snapshot fields.
    mcs_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("mcs_configs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mcs_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    mcs_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Required because mcs_id is ON DELETE SET NULL: once the config is gone the id
    # is NULL, so mcs_id alone cannot distinguish "never had MCS auth" from
    # "credentials are unrecoverable". Without it a run whose connection was deleted
    # would execute unauthenticated against the snapshotted mcs_url.
    mcs_auth_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    mcs_wipe_before_job: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
```

Ensure `ForeignKey` and `Integer` are imported in that module.

- [ ] **Step 4: Add the migration**

In `backend/app/main.py`, in the same idempotent-ALTER list that carries the `jobs` columns:

```python
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_id INTEGER REFERENCES mcs_configs(id) ON DELETE SET NULL",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_url VARCHAR(1024)",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_name VARCHAR(512)",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_auth_type VARCHAR(32)",
            # Issue #397. FALSE on existing rows = scoped wipe, the safe default.
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_wipe_before_job BOOLEAN NOT NULL DEFAULT FALSE",
```

No backfill `UPDATE` is needed: NULL `mcs_url` on legacy rows is handled by the fallback in Task 7, and `FALSE` is the correct and safe default for the wipe flag. Do NOT add a backfill — an unguarded one re-asserts on every restart (the #401 lesson).

- [ ] **Step 5: Populate the snapshot in the route**

`backend/app/routes/validation.py`, replacing the `ValidationRun(...)` construction:

```python
    mcs = await get_active_mcs(session=session)
    run = ValidationRun(
        status=ValidationStatus.queued,
        measure_urls=body.measure_urls if body else None,
        mcs_id=mcs.id if mcs.id else None,
        mcs_url=mcs.mcs_url,
        mcs_name=mcs.name,
        mcs_auth_type=mcs.auth_type.value if mcs.auth_type else None,
        mcs_wipe_before_job=mcs.wipe_before_job,
    )
```

Add `from app.dependencies import get_active_mcs` to that module's imports.

- [ ] **Step 6: Run tests**

```bash
cd backend && python3 -m pytest tests/test_routes_validation.py tests/test_migrations.py -q
```
Expected: PASS.

- [ ] **Step 7: Verify the DDL against real Postgres**

The unit suite runs on SQLite and never executes these ALTERs. Verify syntax in a throwaway database (the dev stack's Postgres container name may differ — check `docker compose ps`):

```bash
docker exec mct2-db-1 psql -U mct2 -d postgres -q -c "CREATE DATABASE ddlprobe397;"
docker exec mct2-db-1 psql -U mct2 -d ddlprobe397 -q -c "CREATE TABLE mcs_configs (id serial primary key);" -c "CREATE TABLE validation_runs (id serial primary key);"
docker exec mct2-db-1 psql -U mct2 -d ddlprobe397 -v ON_ERROR_STOP=1 -c "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_id INTEGER REFERENCES mcs_configs(id) ON DELETE SET NULL; ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_url VARCHAR(1024); ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_name VARCHAR(512); ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_auth_type VARCHAR(32); ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_wipe_before_job BOOLEAN NOT NULL DEFAULT FALSE;"
docker exec mct2-db-1 psql -U mct2 -d ddlprobe397 -c "\d validation_runs"
docker exec mct2-db-1 psql -U mct2 -d postgres -q -c "DROP DATABASE ddlprobe397;"
```
Expected: all five columns present, no error. Then drop the probe database.

- [ ] **Step 8: Lint and commit**

```bash
cd backend && ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/models/validation.py backend/app/main.py backend/app/routes/validation.py backend/tests/
git commit -m "$(cat <<'EOF'
feat(validation): snapshot the MCS connection on validation_runs (#397)

A validation result is a claim about correctness, and the claim had no record of
which server produced it: switch connections and last week's "33/33 passed"
describes a server nobody can identify.

Adds mcs_id/mcs_url/mcs_name/mcs_auth_type/mcs_wipe_before_job, mirroring Job.
mcs_auth_type is load-bearing, not decorative: mcs_id is ON DELETE SET NULL, so it
is the only thing distinguishing "never had a config" from "config was deleted" —
and without it a run whose connection was deleted would execute unauthenticated
against the snapshotted URL.

No backfill UPDATE: NULL mcs_url is handled by the legacy fallback and FALSE is the
safe default for the wipe flag. An unguarded backfill re-asserts on every restart,
which is the #401 lesson.

DDL verified against a real Postgres in a throwaway database; the unit suite runs
on SQLite and never executes these ALTERs.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 7: `run_validation` resolves from the snapshot and threads the target

**Files:**
- Modify: `backend/app/services/validation.py:1085-1200` (resolve target from snapshot, read-only refusal)
- Modify: `backend/app/services/validation.py:1254, 1298, 1345, 1359` (explicit push/evaluate/lookup targets)
- Modify: `backend/tests/test_mcs_scoping_inventory.py` (validation.py 2 → 1)
- Test: `backend/tests/test_services_validation.py`

**Interfaces:**
- Consumes: `McsTarget` (Task 1), `resolve_mcs_auth_headers` (Task 2), snapshot columns (Task 6).
- Produces: no new public surface.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_run_validation_targets_the_snapshotted_mcs(test_session, session_factory):
    """Every measure-engine call in one run must hit the SAME server.

    This is the anti-divergence guard. Pushing to one server and evaluating on
    another produces confidently wrong pass/fail numbers rather than an error, which
    is ADR-011's bug. A missed call site is exactly how that returns.
    """
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.models.validation import ExpectedResult, ValidationRun, ValidationStatus
    from app.services.validation import run_validation

    cfg = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    run = ValidationRun(
        status=ValidationStatus.queued,
        mcs_id=cfg.id,
        mcs_url=cfg.mcs_url,
        mcs_name=cfg.name,
        mcs_auth_type="none",
        mcs_wipe_before_job=False,
    )
    test_session.add(run)
    test_session.add(
        ExpectedResult(
            measure_url="http://cms.gov/M1",
            patient_ref="p1",
            expected_populations={"initial-population": 1},
            period_start="2024-01-01",
            period_end="2024-12-31",
        )
    )
    await test_session.commit()
    await test_session.refresh(run)

    targets: list[str] = []

    async def _record_push(resources, **kwargs):
        targets.append(kwargs.get("target_url"))
        from app.services.fhir_client import BundleUploadResult

        return BundleUploadResult()

    async def _record_eval(measure_id, patient_id, ps, pe, **kwargs):
        targets.append(kwargs.get("measure_engine_url"))
        return {"resourceType": "MeasureReport", "group": []}

    with (
        patch("app.services.validation.async_session", make_session_patcher(test_session)),
        patch("app.services.validation.push_resources", side_effect=_record_push),
        patch("app.services.validation.evaluate_measure", side_effect=_record_eval),
        patch("app.services.validation.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.validation._resolve_measure_id", new_callable=AsyncMock, return_value="m1"),
    ):
        await run_validation(run.id)

    assert targets, "no measure-engine calls were recorded"
    assert set(targets) == {"https://mcs.example.org/fhir"}, targets


@pytest.mark.asyncio
async def test_run_validation_refuses_a_read_only_mcs(test_session, session_factory):
    """Read-only is read LIVE, not from the snapshot: it answers "may I write now"."""
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.models.validation import ValidationRun, ValidationStatus
    from app.services.validation import run_validation

    cfg = MCSConfig(
        name="Protected MCS",
        mcs_url="https://shared.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_read_only=True,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    run = ValidationRun(
        status=ValidationStatus.queued,
        mcs_id=cfg.id,
        mcs_url=cfg.mcs_url,
        mcs_auth_type="none",
        mcs_wipe_before_job=False,
    )
    test_session.add(run)
    await test_session.commit()
    await test_session.refresh(run)

    with (
        patch("app.services.validation.async_session", make_session_patcher(test_session)),
        patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push,
        patch("app.services.validation.wipe_patients_by_id", new_callable=AsyncMock) as mock_wipe,
    ):
        await run_validation(run.id)

    mock_push.assert_not_awaited()
    mock_wipe.assert_not_awaited()
    await test_session.refresh(run)
    assert run.status == ValidationStatus.failed
    assert "read-only" in (run.error_message or "").lower()


@pytest.mark.asyncio
async def test_run_validation_legacy_null_mcs_url_falls_back(test_session, session_factory):
    """Runs created before the snapshot column must remain runnable."""
    from app.config import settings as app_settings
    from app.models.validation import ValidationRun, ValidationStatus
    from app.services.validation import run_validation

    run = ValidationRun(status=ValidationStatus.queued, mcs_url=None, mcs_auth_type=None)
    test_session.add(run)
    await test_session.commit()
    await test_session.refresh(run)

    targets: list[str] = []

    async def _record_push(resources, **kwargs):
        targets.append(kwargs.get("target_url"))
        from app.services.fhir_client import BundleUploadResult

        return BundleUploadResult()

    with (
        patch("app.services.validation.async_session", make_session_patcher(test_session)),
        patch("app.services.validation.push_resources", side_effect=_record_push),
        patch("app.services.validation.wipe_patients_by_id", new_callable=AsyncMock),
    ):
        await run_validation(run.id)

    for t in targets:
        assert t == app_settings.MEASURE_ENGINE_URL
```

Add a `make_session_patcher` helper to this test module if absent, copying the shape from `tests/test_admin_factory_reset.py`.

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -k run_validation -v
```
Expected: FAIL.

- [ ] **Step 3: Resolve the target inside `run_validation`**

After the existing status-transition block (right after `run.status = ValidationStatus.running` and its commit), add:

```python
    # Resolve the target ONCE from the run's snapshot, not from whatever connection
    # is active now: a run that sat in the queue while the user switched connections
    # must still execute against the server it was created for (issue #397).
    async with async_session() as session:
        run_row = await session.get(ValidationRun, validation_run_id)
        if run_row is None:
            return
        mcs_url = run_row.mcs_url or settings.MEASURE_ENGINE_URL
        snapshot_mcs_id = run_row.mcs_id
        snapshot_auth_type = run_row.mcs_auth_type
        wipe_before = bool(run_row.mcs_wipe_before_job)
        try:
            mcs_auth_headers = await resolve_mcs_auth_headers(
                session,
                mcs_id=snapshot_mcs_id,
                mcs_url=run_row.mcs_url,
                mcs_auth_type=snapshot_auth_type,
                owner_label=f"validation run {validation_run_id}",
            )
        except RuntimeError as exc:
            await _fail_validation_run(validation_run_id, str(exc))
            return

        # is_read_only is read LIVE, not from the snapshot: it answers "may I write to
        # this server NOW". Snapshotting it would let a run write to a server the user
        # has since marked protected.
        live_read_only = False
        if snapshot_mcs_id is not None:
            cfg = await session.get(MCSConfig, snapshot_mcs_id)
            live_read_only = bool(cfg.is_read_only) if cfg else False

    if live_read_only:
        await _fail_validation_run(
            validation_run_id,
            "The measure engine connection for this run is configured as read-only. "
            "A validation run must write measures and patient data, so it cannot proceed. "
            "Switch to a writable connection and start a new run.",
        )
        return

    mcs = McsTarget(
        url=mcs_url,
        auth_headers=mcs_auth_headers,
        is_read_only=live_read_only,
        wipe_before_job=wipe_before,
    )
```

Add a small helper next to `run_validation` if one does not already exist:

```python
async def _fail_validation_run(validation_run_id: int, message: str) -> None:
    """Mark a run failed with a diagnosis, so it never sits in `running` forever."""
    async with async_session() as session:
        run = await session.get(ValidationRun, validation_run_id)
        if run:
            run.status = ValidationStatus.failed
            run.error_message = message
            run.completed_at = datetime.now(timezone.utc)
            await session.commit()
```

Import `MCSConfig`, `McsTarget`, and `resolve_mcs_auth_headers` in `validation.py`.

- [ ] **Step 4: Make the remaining calls explicit**

- line ~1254 (`gather_and_push` closure, captures `mcs`):
  ```python
                        await push_resources(resources, target_url=mcs.url, auth_headers=mcs.auth_headers)
  ```
- line ~1298 (warm-up evaluate):
  ```python
                        await evaluate_measure(
                            info["hapi_id"],
                            warmup_er.patient_ref,
                            info["period_start"],
                            info["period_end"],
                            measure_engine_url=mcs.url,
                            auth_headers=mcs.auth_headers,
                        )
  ```
- line ~1345 (`evaluate_and_compare` closure):
  ```python
                    report = await evaluate_measure(
                        info["hapi_id"],
                        er.patient_ref,
                        info["period_start"],
                        info["period_end"],
                        measure_engine_url=mcs.url,
                        auth_headers=mcs.auth_headers,
                    )
  ```
- line ~1359 (patient-name lookup):
  ```python
                                resp = await http_client.get(f"{mcs.url}/{ref_str}", headers=mcs.auth_headers)
  ```
- the two `_resolve_measure_id(measure_url)` calls (~1143, ~1171): add `mcs=mcs`.
- the `_reload_measures_from_seed_bundles()` call inside `run_validation` (if present): add `mcs=mcs`.

- [ ] **Step 5: Run tests**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -q
```
Expected: PASS.

- [ ] **Step 6: Update the inventory guard, lint, commit**

`"app/services/validation.py"`: stays at `2` — this task NETS ZERO. It removes the
patient-name lookup at 1359 but ADDS one read: the legacy-NULL fallback
`run_row.mcs_url or settings.MEASURE_ENGINE_URL` in the new resolve block. Record
that in the comment block, because a count that does not move is exactly where a
reader assumes nothing happened. Task 8 takes it to 1.

```bash
cd backend && python3 -m pytest tests/test_mcs_scoping_inventory.py -q
ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/services/validation.py backend/tests/
git commit -m "$(cat <<'EOF'
fix(validation): run_validation targets its snapshotted MCS throughout (#397)

Resolves the target once from the run's snapshot — not the currently-active
connection, so a run that sat in the queue while the user switched connections still
executes against the server it was created for — and threads it through every
measure-engine call: pushes, both evaluates, the measure-id lookups and the
patient-name lookup.

The push and evaluate calls previously named no target at all and relied on
back-compat defaults. That mattered more than it looks: scoping the writes without
the evaluation would push test data to one server and grade against another,
producing confidently wrong pass/fail numbers instead of an error. A test asserts
every recorded call hit the same URL.

Refuses a read-only connection, read LIVE rather than from the snapshot, because it
answers "may I write to this server now".

Inventory guard: validation.py 2 -> 1 (legacy-NULL fallback only).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 8: Scope the validation wipe

**Files:**
- Modify: `backend/app/services/validation.py:1239`
- Modify: `backend/tests/test_mcs_scoping_inventory.py` (validation.py 2 → 1)
- Test: `backend/tests/test_services_validation.py`

**Interfaces:**
- Consumes: `wipe_patients_by_id` and `wipe_patient_data` from `fhir_client` (both exist), `mcs` from Task 7.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_run_validation_scoped_wipe_by_default(test_session, session_factory):
    """The second copy of the #392 bug. Unfiltered here too until now.

    #392 scoped run_job's wipe and never touched this path. Re-pointing it at the
    active MCS without scoping would have deleted every participant's data on a
    shared server.
    """
    from app.models.validation import ExpectedResult, ValidationRun, ValidationStatus
    from app.services.validation import run_validation

    run = ValidationRun(
        status=ValidationStatus.queued,
        mcs_url="https://mcs.example.org/fhir",
        mcs_auth_type=None,
        mcs_wipe_before_job=False,
    )
    test_session.add(run)
    for ref in ("p1", "p2"):
        test_session.add(
            ExpectedResult(
                measure_url="http://cms.gov/M1",
                patient_ref=ref,
                expected_populations={"initial-population": 1},
                period_start="2024-01-01",
                period_end="2024-12-31",
            )
        )
    await test_session.commit()
    await test_session.refresh(run)

    with (
        patch("app.services.validation.async_session", make_session_patcher(test_session)),
        patch("app.services.validation.push_resources", new_callable=AsyncMock),
        patch("app.services.validation.evaluate_measure", new_callable=AsyncMock, return_value={"group": []}),
        patch("app.services.validation._resolve_measure_id", new_callable=AsyncMock, return_value="m1"),
        patch("app.services.validation.wipe_patient_data", new_callable=AsyncMock) as mock_full,
        patch("app.services.validation.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped,
    ):
        await run_validation(run.id)

    mock_full.assert_not_awaited()
    mock_scoped.assert_awaited_once()
    kwargs = mock_scoped.await_args.kwargs
    assert kwargs["base_url"] == "https://mcs.example.org/fhir"
    assert sorted(kwargs["patient_ids"]) == ["p1", "p2"]


@pytest.mark.asyncio
async def test_run_validation_full_wipe_when_connection_opted_in(test_session, session_factory):
    from app.models.validation import ExpectedResult, ValidationRun, ValidationStatus
    from app.services.validation import run_validation

    run = ValidationRun(
        status=ValidationStatus.queued,
        mcs_url="https://mcs.example.org/fhir",
        mcs_auth_type=None,
        mcs_wipe_before_job=True,
    )
    test_session.add(run)
    test_session.add(
        ExpectedResult(
            measure_url="http://cms.gov/M1",
            patient_ref="p1",
            expected_populations={"initial-population": 1},
            period_start="2024-01-01",
            period_end="2024-12-31",
        )
    )
    await test_session.commit()
    await test_session.refresh(run)

    with (
        patch("app.services.validation.async_session", make_session_patcher(test_session)),
        patch("app.services.validation.push_resources", new_callable=AsyncMock),
        patch("app.services.validation.evaluate_measure", new_callable=AsyncMock, return_value={"group": []}),
        patch("app.services.validation._resolve_measure_id", new_callable=AsyncMock, return_value="m1"),
        patch("app.services.validation.wipe_patient_data", new_callable=AsyncMock) as mock_full,
        patch("app.services.validation.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped,
    ):
        await run_validation(run.id)

    mock_scoped.assert_not_awaited()
    mock_full.assert_awaited_once()
    assert mock_full.await_args.kwargs["base_url"] == "https://mcs.example.org/fhir"
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -k "scoped_wipe_by_default or full_wipe_when_connection" -v
```
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace line ~1239 with:

```python
            # Clear the prior run's data off the target. Scoped by default: this is a
            # second copy of the #392 hazard — #392 scoped run_job's wipe and never
            # touched the validation path, so an unfiltered delete here would remove
            # every participant's patients on a shared engine.
            #
            # Correctness is preserved for the same reason as #392: evaluation is
            # per-subject, so patients this run never evaluates cannot affect its
            # numbers. See ADR-012.
            validation_patient_ids = sorted({er.patient_ref for er in resolved_expected_results if er.patient_ref})
            if mcs.wipe_before_job:
                logger.warning(
                    "Full patient-data wipe starting — deletes ALL patients on the target MCS",
                    extra={"run_id": validation_run_id, "mcs_url": sanitize_url(mcs.url), "scope": "all-patients"},
                )
                await wipe_patient_data(base_url=mcs.url, strict=False, auth_headers=mcs.auth_headers)
            else:
                logger.info(
                    "Scoped patient-data wipe starting — deletes only this run's patients",
                    extra={
                        "run_id": validation_run_id,
                        "mcs_url": sanitize_url(mcs.url),
                        "scope": "run-patients",
                        "patient_count": len(validation_patient_ids),
                    },
                )
                await wipe_patients_by_id(
                    base_url=mcs.url, patient_ids=validation_patient_ids, auth_headers=mcs.auth_headers
                )
```

Import `wipe_patients_by_id` and `sanitize_url` in `validation.py` if not already present. Confirm `resolved_expected_results` is in scope at this point; if the wipe currently sits above its assignment, move the wipe below it — the scoped wipe needs the ids.

- [ ] **Step 4: Run tests**

```bash
cd backend && python3 -m pytest tests/test_services_validation.py -q
```
Expected: PASS.

- [ ] **Step 4b: Update the inventory guard**

`"app/services/validation.py"`: `2` → `1`. The only remaining read is the
legacy-NULL fallback introduced in Task 7. Record that as the reason.

- [ ] **Step 5: Full unit suite — the first end-to-end check of Tasks 3-8**

```bash
cd backend && python3 -m pytest tests/ --ignore=tests/integration -q
```
Expected: PASS. Tasks 3-8 form one coherent change; this is where the module first imports cleanly with every signature aligned.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff format app/ tests/ && ruff check app/ tests/
git add backend/app/services/validation.py backend/tests/
git commit -m "$(cat <<'EOF'
fix(validation): scope the pre-run patient wipe (#397, second copy of #392)

run_validation did an unfiltered wipe_patient_data. #392 fixed exactly this in
run_job and never touched the validation path, so the hazard survived here —
harmless only because it hit the local container. Re-pointing it at the active MCS
without scoping would have activated #392's bug on the validation path.

Scoped by default to the run's own patient refs, full sweep only where the
connection opted in via wipe_before_job. Correctness holds for ADR-012's reason:
evaluation is per-subject, so patients the run never evaluates cannot affect its
numbers. The full-wipe branch logs at WARNING with the target.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 9: Teach the inventory guard about defaulted-parameter reliance

**Files:**
- Modify: `backend/tests/test_mcs_scoping_inventory.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EXPECTED_UNSCOPED_CALLS: dict[str, int]` plus a second test.

This closes a hole shipped in #402: the guard counts AST attribute accesses only, so it could not see the seven `push_resources`/`evaluate_measure` calls that relied on a default. Its docstring claimed to pin the bug class; that was too strong.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_mcs_scoping_inventory.py`:

```python
# Calls that reach a measure engine but take their target from a DEFAULTED parameter
# rather than naming it. Invisible to the attribute-read counter above, which is how
# seven unscoped sites in validation.py hid behind a guard that reported "clean".
_TARGET_KWARG = {
    "push_resources": "target_url",
    "evaluate_measure": "measure_engine_url",
    "resolve_evaluated_resource": "base_url",
}

# Remaining allowed omissions, per file. push_resources' CDR calls are NOT counted:
# they pass target_url explicitly, which is the whole point.
EXPECTED_UNSCOPED_CALLS: dict[str, int] = {}


def _count_unscoped_calls() -> dict[str, int]:
    """Count calls to target-taking helpers that omit their target keyword."""
    counts: dict[str, int] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        hits = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _TARGET_KWARG:
                continue
            # resolve_evaluated_resource takes base_url positionally as arg 2.
            if name == "resolve_evaluated_resource" and len(node.args) >= 2:
                continue
            if any(kw.arg == _TARGET_KWARG[name] for kw in node.keywords):
                continue
            hits += 1
        if hits:
            counts[path.relative_to(_APP_ROOT.parent).as_posix()] = hits
    return counts


def test_no_measure_engine_call_relies_on_a_defaulted_target():
    """Every call names the server it means.

    A defaulted target is the #397 mechanism itself: the call reads as innocuous and
    silently goes somewhere the caller did not choose.
    """
    actual = _count_unscoped_calls()
    unexpected = {f: n for f, n in actual.items() if EXPECTED_UNSCOPED_CALLS.get(f, 0) != n}
    assert not unexpected, (
        f"Call(s) taking their measure-engine target from a default: {unexpected}. "
        f"Pass the explicit keyword ({', '.join(f'{k}=>{v}' for k, v in _TARGET_KWARG.items())}), "
        "or add the file to EXPECTED_UNSCOPED_CALLS with a reason."
    )
```

Also correct the module docstring's overclaim, replacing the "Two things it buys" list's first bullet with:

```
1. **No new reads, and no new defaulted targets.** Both an added
   `settings.MEASURE_ENGINE_URL` access and a call that omits its target keyword
   fail this suite. The second check exists because the first one alone reported a
   clean inventory while seven unscoped calls sat in validation.py (#397 slice 3).
```

- [ ] **Step 2: Run it**

```bash
cd backend && python3 -m pytest tests/test_mcs_scoping_inventory.py -v
```
Expected: PASS if Tasks 5 and 7 made every call explicit. If it FAILS, it has found a real site those tasks missed — fix the call, do not pad `EXPECTED_UNSCOPED_CALLS`.

- [ ] **Step 3: Mutation-verify the new check**

A guard that cannot be shown to fire is not a guard.

```bash
cd backend && cp app/services/validation.py /tmp/val.bak
python3 - <<'PY'
p='app/services/validation.py'; s=open(p).read()
s = s.replace("await push_resources(primary, target_url=mcs.url, auth_headers=mcs.auth_headers)",
              "await push_resources(primary)", 1)
open(p,'w').write(s)
PY
python3 -m pytest tests/test_mcs_scoping_inventory.py -q 2>&1 | tail -3
cp /tmp/val.bak app/services/validation.py && rm -f /tmp/val.bak
python3 -m pytest tests/test_mcs_scoping_inventory.py -q 2>&1 | tail -2
```
Expected: FAIL with the omitted-target message, then PASS after restore. If it passes with the mutation in place, the detector is broken — fix it before continuing.

- [ ] **Step 4: Lint and commit**

```bash
cd backend && ruff format tests/ && ruff check tests/
git add backend/tests/test_mcs_scoping_inventory.py
git commit -m "$(cat <<'EOF'
test: catch defaulted measure-engine targets, not just env-var reads (#397)

The guard shipped in #402 counted AST attribute accesses, so it could not see a
call taking its target from a defaulted parameter — which was 7 of the 16 unscoped
sites in validation.py. It reported a clean inventory while they sat in plain sight,
and its docstring claimed to pin the bug class. Both are now corrected.

Adds a second AST pass over push_resources / evaluate_measure /
resolve_evaluated_resource calls that omit their target keyword.

Mutation-verified: removing target_url from one push makes it fail with an
actionable message.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 10: Integration test — a validation run spares bystanders

**Files:**
- Create: `backend/tests/integration/test_validation_mcs_scoping.py`

**Interfaces:**
- Consumes: the `measure_url` fixture from `tests/integration/conftest.py`; `wipe_patients_by_id` from `fhir_client`.

Mocks proved the URLs are built correctly; only a real server proves the scoped delete spares a bystander. Modelled on `tests/integration/test_scoped_wipe.py`.

- [ ] **Step 1: Write the test**

```python
"""Integration tests for validation-pipeline MCS scoping (issue #397 slice 3).

The unit tests assert which URLs the pipeline builds. Only a real FHIR server shows
that a validation run's scoped wipe actually spares another participant's patients —
the property the whole slice exists for.

Only ever wipes patients prefixed `lenny-397-`, so it cannot disturb the
session-scoped seed data other integration tests rely on.
"""

import httpx
import pytest

from app.services.fhir_client import wipe_patients_by_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RUN_PATIENT = "lenny-397-run"
_BYSTANDER = "lenny-397-bystander"


def _patient(pid: str) -> dict:
    return {"resourceType": "Patient", "id": pid, "name": [{"family": pid}]}


def _condition(pid: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": f"{pid}-cond",
        "subject": {"reference": f"Patient/{pid}"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
    }


async def _seed(measure_url: str) -> None:
    entries = [
        {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
        for r in (_patient(_RUN_PATIENT), _patient(_BYSTANDER), _condition(_RUN_PATIENT), _condition(_BYSTANDER))
    ]
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            measure_url,
            json={"resourceType": "Bundle", "type": "batch", "entry": entries},
            headers={"Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()


async def _exists(measure_url: str, rtype: str, rid: str) -> bool:
    """Direct read, not a search: per CLAUDE.md, a search can return a stale
    snapshot while a direct GET works regardless of index state."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{measure_url}/{rtype}/{rid}")
    return resp.status_code == 200


async def test_validation_scoped_wipe_spares_other_patients(measure_url):
    """The acceptance property for slice 3's wipe, against a real server."""
    await _seed(measure_url)
    assert await _exists(measure_url, "Patient", _RUN_PATIENT), "seed failed"
    assert await _exists(measure_url, "Patient", _BYSTANDER), "seed failed"

    try:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT])

        assert not await _exists(measure_url, "Patient", _RUN_PATIENT)
        assert not await _exists(measure_url, "Condition", f"{_RUN_PATIENT}-cond")
        assert await _exists(measure_url, "Patient", _BYSTANDER), (
            "a validation run deleted a patient it was not evaluating — the #392 hazard "
            "on the validation path"
        )
        assert await _exists(measure_url, "Condition", f"{_BYSTANDER}-cond")
    finally:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT, _BYSTANDER])
```

- [ ] **Step 2: Run it**

```bash
cd /Users/bill/dev/bellese/lenny-397-slice3
PATH="/Users/bill/dev/bellese/mct2/backend/.venv/bin:$PATH" ./scripts/run-integration-tests.sh tests/integration/test_validation_mcs_scoping.py
```
Expected: PASS. The CI `--ignore` list will silently skip this new file, so it must be run explicitly (CLAUDE.md pre-push step 5).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/integration/test_validation_mcs_scoping.py
git commit -m "$(cat <<'EOF'
test(integration): validation's scoped wipe spares bystander patients (#397)

Mocks prove which URLs the pipeline builds; only a real FHIR server proves the
scoped delete leaves another participant's patients intact, which is the property
slice 3 exists for.

Uses direct reads rather than searches, per CLAUDE.md's HAPI async-indexing
section, and only ever wipes lenny-397- prefixed ids so it cannot disturb the
session-scoped seed data.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017Ena9eKHA4JfNe1mHJEwQa
EOF
)"
```

---

### Task 11: Docs, version, and the full pre-push gate

**Files:**
- Modify: `docs/decisions.md` (ADR-014)
- Modify: `CHANGELOG.md`, `VERSION`, `frontend/package.json`
- Modify: `docs/superpowers/specs/2026-08-19-validation-mcs-scoping-design.md` (status line)

- [ ] **Step 1: Write ADR-014**

Append to `docs/decisions.md`, covering: the 16-vs-9 site count and why 7 were invisible; the second copy of the #392 wipe and why a naive re-point would have activated it; `McsTarget` and why not a contextvar; the `validation_runs` snapshot with `mcs_auth_type`'s role; why `is_read_only` is read live while everything else is snapshotted; the guard's corrected scope; and `bundle_loader.py:32` reclassified as not-a-defect (a boot readiness probe of Lenny's own containers).

- [ ] **Step 2: Bump version**

```bash
cd /Users/bill/dev/bellese/lenny-397-slice3
bun run ~/.claude/skills/gstack/bin/gstack-version-bump classify --base main
bun run ~/.claude/skills/gstack/bin/gstack-version-bump write --version "<next PATCH>"
```
Then set the same value in `frontend/package.json` by hand — the tool does not manage it (no `.gstack/package-json-path` pin), and repo convention bumps `VERSION`, `frontend/package.json`, and `CHANGELOG.md` together.

- [ ] **Step 3: CHANGELOG entry**

Insert a `## [X.Y.Z.W] - YYYY-MM-DD` block after the header, user-outcome-first: validation now runs against the connection you selected; a validation run no longer deletes other people's patient data; a read-only measure engine is respected; validation results record which server produced them.

- [ ] **Step 4: Run the full gate**

```bash
cd /Users/bill/dev/bellese/lenny-397-slice3/backend
export PATH="/Users/bill/dev/bellese/mct2/backend/.venv/bin:$PATH"
ruff check app/ tests/ && ruff format --check app/ tests/
~/.claude/skills/gstack/bin/gstack-evidence run --label tests -- 'cd /Users/bill/dev/bellese/lenny-397-slice3/backend && python3 -m pytest tests/ --ignore=tests/integration -q 2>&1'
cd ../frontend && CI=true npx react-scripts test --watchAll=false && CI=true npm run build
```

Then integration, INCLUDING the golden and connectathon suites — this changes the measure-evaluation pipeline, which is CLAUDE.md's stated trigger for the full run:

```bash
cd /Users/bill/dev/bellese/lenny-397-slice3
PATH="/Users/bill/dev/bellese/mct2/backend/.venv/bin:$PATH" ./scripts/run-integration-tests.sh
```
This is the 600+ patient run and takes a long time. It is the real check that validation still produces the same numbers after re-targeting. Do not substitute the `--ignore` variant.

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs/ CHANGELOG.md VERSION frontend/package.json
git commit -m "docs: ADR-014 for validation MCS scoping, bump version (#397)"
git push -u origin fix/397-validation-mcs-scoping
```

PR body must use `.github/pull_request_template.md`'s sections (CLAUDE.md: `gh pr create` does not auto-populate). Title format `v<VERSION> fix: ...`. State plainly: the 16-vs-9 finding, the second #392 wipe copy, the read-only behavior change, the guard's previously-overclaimed scope, and that #397 can finally be closed by this PR.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 `McsTarget` value object | 1 |
| §2 target resolution at 3 entry points | 5 (triage, `_reload_...`), 7 (`run_validation`) |
| §2 helpers gaining `mcs` | 3, 4 |
| §2 explicit push/evaluate targets | 5, 7 |
| §3 snapshot + migration | 6 |
| §3 generalised credential helper | 2 |
| §3 `is_read_only` read live | 7 |
| §4 the wipe | 8 |
| §5 read-only refusal | 5 (triage), 7 (`run_validation`) |
| §6 inventory guard fix | 9 |
| Testing: anti-divergence | 7 Step 1 |
| Testing: integration bystander | 10 |
| Out-of-scope: `bundle_loader:32` reclassified | 5 Step 7, 11 Step 1 |

No spec section is unimplemented.

**Placeholder scan:** No TBDs. Three places instruct the executor to verify a
value against the code rather than trusting the plan — the `validation_runs` route
path (Task 6 Step 1), the `bundle_loader` guard count (Task 5 Step 9), and the next
PATCH version (Task 11 Step 2). These are deliberate: a stale line number or a
guessed count in the plan is worse than an instruction to check.

**Type consistency:** `McsTarget(url, auth_headers, is_read_only, wipe_before_job)`
is used with those exact field names in Tasks 1, 3, 4, 5, 7, 8. The `mcs` parameter
is keyword-only (`*, mcs: McsTarget`) in every helper. `resolve_mcs_auth_headers`'s
signature in Task 2 matches its call in Task 7. `_resolve_measure_id` returns
`str | None` in Task 4 and is used as an optional in Task 7.

**Known ordering constraint:** Tasks 3-8 form one coherent change — Task 4 Step 4
updates call sites referencing an `mcs` variable that Tasks 5 and 7 introduce, so
the module will not import cleanly between them. Each task still commits its own
tested unit; the full-suite gate lands at Task 8 Step 5. This is called out in Task
4 rather than hidden.
